from __future__ import annotations

import asyncio
import json
from pathlib import Path

from .db import Database
from .execution import RunContext, current
from .tools import Tools


def trim_history(messages: list, limit: int) -> tuple[list, bool]:
    # Drop whole user turns so no assistant tool call can lose its tool result.
    result = list(messages)
    trimmed = False
    while len(json.dumps(result, ensure_ascii=False)) > limit:
        boundary = next((i for i, m in enumerate(result) if i > 0 and m["role"] == "user"), None)
        if boundary is None:
            raise ValueError("本轮上下文超过限制，请缩小任务或开启新会话")
        result = result[boundary:]
        trimmed = True
    return result, trimmed


class Agent:
    def __init__(self, db: Database, provider, tools: Tools, store):
        self.db, self.provider, self.tools, self.store = db, provider, tools, store
        self.active = set()

    async def run(self, session: dict, prompt: str, permission: str, cfg):
        session_id = session["id"]
        run_id = self.db.start_run(session_id, cfg.model, permission)
        inherited = current.get()
        run_context = RunContext(
            session_id=session_id,
            run_id=run_id,
            permission=permission,
            depth=inherited.depth if inherited else 0,
            budget=inherited.budget if inherited else {"children": 0, "turns": 0},
        )
        context_token = current.set(run_context)
        from .telemetry import JsonlSink, SessionTracer, active

        tracer = (
            SessionTracer(
                session_id,
                JsonlSink(self.store.data / "traces" / (run_id + ".jsonl"), self.store.redact),
            )
            if cfg.telemetry_enabled
            else None
        )
        trace_token = active.set(tracer)
        history = session["messages"] + [{"role": "user", "content": prompt}]
        baseline = list(history)
        usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        status, error, turns = "cancelled", None, 0

        def event(kind, **data):
            safe = json.loads(self.store.redact(json.dumps(data, ensure_ascii=False)))
            self.db.event(run_id, kind, safe)
            if tracer:
                tracer.record(kind, safe)
            return {"type": kind, **safe}

        try:
            if self.tools.extensions:
                jobs = self.tools.extensions.jobs
                meta = jobs.record("session_metadata", session_id) or {}
                meta.pop("id", None)
                if (
                    meta.get("workspace_root")
                    and Path(meta["workspace_root"]).resolve() != Path(cfg.workspace).resolve()
                ):
                    raise ValueError("会话绑定了其他工作区，请切换回该工作区或创建新会话")
                jobs.put(
                    "session_metadata",
                    session_id,
                    {**meta, "workspace_root": cfg.workspace, "model": cfg.model},
                )
            yield event(
                "run_start",
                run_id=run_id,
                session_id=session_id,
                model=cfg.model,
                permission=permission,
            )
            async with asyncio.timeout(cfg.timeout_seconds):
                from .prompt import assemble

                assembled = await assemble(cfg, permission)
                system = assembled["prompt"]
                yield event("context_loaded", **assembled["context"])
                base_state = assembled["context"]["base"]
                if base_state["state"] in {"diverged", "not_a_git_repo"}:
                    yield event("stale_base", **base_state)
                for turns in range(1, cfg.max_turns + 1):
                    if run_context.budget["turns"] >= 40:
                        raise ValueError("任务树已达到 40 次模型请求预算")
                    run_context.budget["turns"] += 1
                    context = history
                    previous_tokens = next(
                        (
                            m.get("usage", {}).get("input_tokens", 0)
                            + m.get("usage", {}).get("cache_read_input_tokens", 0)
                            for m in reversed(history)
                            if m.get("usage")
                        ),
                        0,
                    )
                    if len(history) > 4 and (
                        len(json.dumps(history, ensure_ascii=False)) > cfg.context_chars
                        or cfg.auto_compact_tokens
                        and previous_tokens >= cfg.auto_compact_tokens
                    ):
                        from .sessions import compact_messages

                        context, compaction = compact_messages(history, preserve=4)
                        if compaction["removed_message_count"]:
                            yield event(
                                "auto_compact", **compaction, persistent_history_preserved=True
                            )
                    context, trimmed = trim_history(context, cfg.context_chars)
                    if trimmed:
                        yield event(
                            "context_trimmed",
                            message="已从模型上下文移除较早的完整对话轮次；原始会话仍保留",
                        )
                    yield event("turn_start", turn=turns)
                    result = None
                    async for item in self.provider.stream(
                        cfg, context, self.tools.definitions(permission, cfg.rag_enabled), system
                    ):
                        if item["type"] == "delta":
                            yield {"type": "delta", "text": item["text"], "turn": turns}
                        elif item["type"] == "provider_fallback":
                            yield event(
                                "provider_fallback",
                                **{k: v for k, v in item.items() if k != "type"},
                            )
                        else:
                            result = item
                    if result is None:
                        raise ValueError("模型未返回完整响应")
                    from .prompt_cache import PromptCache

                    cache_state = PromptCache(self.store.data, session_id).record(
                        {
                            "model": result.get("model", cfg.model),
                            "system": system,
                            "tools": self.tools.definitions(permission, cfg.rag_enabled),
                            "messages": context,
                        },
                        result["usage"],
                    )
                    if cache_state["cache_break"]:
                        yield event("cache_break", **cache_state["cache_break"])
                    for key in usage:
                        usage[key] += result["usage"].get(key, 0)
                    message = result["message"]
                    message["usage"] = result["usage"]
                    history.append(message)
                    yield event(
                        "assistant",
                        message=message,
                        turn=turns,
                        usage=result["usage"],
                        model=result.get("model", cfg.model),
                        provider=result.get("provider", cfg.provider),
                    )
                    calls = message.get("tool_calls", [])
                    if not calls:
                        status = (
                            "limit" if result["stop"] in {"length", "max_tokens"} else "completed"
                        )
                        if status == "limit":
                            error = "模型达到单轮输出上限，回答可能未完成"
                        break
                    if len(calls) > 12:
                        raise ValueError("单轮工具调用超过 12 次限制")
                    for call in calls:
                        name = call["function"]["name"]
                        yield event(
                            "tool_start",
                            name=name,
                            arguments=call["function"]["arguments"],
                            call_id=call["id"],
                            turn=turns,
                        )
                        try:
                            args = json.loads(call["function"]["arguments"] or "{}")
                            output = await self.tools.execute(cfg, permission, name, args)
                            is_error = False
                        except Exception as exc:
                            output, is_error = (
                                {"error": self.store.redact(str(exc))[:1200]},
                                True,
                            )
                        text = json.dumps(output, ensure_ascii=False)
                        history.append(
                            {
                                "role": "tool",
                                "tool_call_id": call["id"],
                                "name": name,
                                "is_error": is_error,
                                "content": text,
                            }
                        )
                        yield event(
                            "tool_result",
                            name=name,
                            call_id=call["id"],
                            output=output,
                            is_error=is_error,
                            turn=turns,
                        )
                else:
                    status, error = "limit", "达到 Agent 最大轮数，任务未确认完成"
            # Only save structurally complete turns; interrupted calls stay in the trace.
            self.db.save_messages(session_id, history, prompt if not session["messages"] else None)
        except asyncio.CancelledError:
            error = "用户停止或客户端断开连接"
            raise
        except TimeoutError:
            status, error = "timeout", "超过本次运行的总时间限制"
            yield event("error", message=error)
        except Exception as exc:
            status, error = (
                "failed",
                self.store.redact(str(exc))[:1500] or type(exc).__name__,
            )
            yield event("error", message=error)
        finally:
            if status in {"cancelled", "timeout", "failed"}:
                if self.tools.extensions:
                    for child in self.tools.extensions.jobs.active_children(run_id):
                        await self.tools.extensions.jobs.stop(child["id"])
                self.db.save_messages(
                    session_id,
                    baseline
                    + [
                        {
                            "role": "assistant",
                            "content": f"[运行未完成：{error or status}]",
                        }
                    ],
                    prompt if not session["messages"] else None,
                )
            self.db.finish_run(run_id, status, turns, usage, error)
            self.db.event(
                run_id,
                "run_end",
                {"status": status, "usage": usage, "turns": turns, "error": error},
            )
            if tracer:
                tracer.record(
                    "run_end", {"status": status, "usage": usage, "turns": turns, "error": error}
                )
            self.active.discard(session_id)
            current.reset(context_token)
            active.reset(trace_token)
        yield {
            "type": "run_end",
            "run_id": run_id,
            "status": status,
            "usage": usage,
            "turns": turns,
            "error": error,
        }
