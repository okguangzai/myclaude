"""Session interchange and deterministic compaction from the Rust runtime contracts."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

PREAMBLE = "This session is being continued from a previous conversation that ran out of context. The summary below covers the earlier portion of the conversation.\n\n"


def compress_summary(text, max_chars=1200, max_lines=24, max_line_chars=160):
    if max_chars <= 0 or max_lines <= 0:
        return ""
    lines, seen = [], set()
    for raw in text.splitlines():
        line = " ".join(raw.split())
        if max_line_chars and len(line) > max_line_chars:
            line = line[: max_line_chars - 1] + "…"
        if line and line.lower() not in seen:
            seen.add(line.lower())
            lines.append(line)
    prefixes = (
        "- Scope:",
        "- Current work:",
        "- Pending work:",
        "- Key files referenced:",
        "- Tools mentioned:",
        "- Recent user requests:",
        "- Previously compacted context:",
        "- Newly compacted context:",
    )

    def priority(line):
        if line in {"Summary:", "Conversation summary:"} or line.startswith(prefixes):
            return 0
        return 1 if line.endswith(":") else 2 if line.startswith("- ") else 3

    selected = []
    for index in sorted(range(len(lines)), key=lambda i: (priority(lines[i]), i)):
        candidate = sorted([*selected, index])
        if len(candidate) <= max_lines and len("\n".join(lines[i] for i in candidate)) <= max_chars:
            selected = candidate
    result = [lines[i] for i in selected]
    if not result and lines:
        result = [lines[0] if len(lines[0]) <= max_chars else lines[0][: max_chars - 1] + "…"]
    omitted = len(lines) - len(result)
    notice = f"- … {omitted} additional line(s) omitted."
    if omitted and len(result) < max_lines and len("\n".join([*result, notice])) <= max_chars:
        result.append(notice)
    return "\n".join(result)


def compact_messages(messages, preserve=4, max_chars=1200):
    if preserve < 0:
        raise ValueError("preserve 不能为负数")
    boundary = max(0, len(messages) - preserve)
    while 0 < boundary < len(messages) and messages[boundary]["role"] == "tool":
        boundary -= 1
    if not boundary:
        return messages, {"removed_message_count": 0, "summary": ""}
    removed, tail = messages[:boundary], messages[boundary:]
    count = Counter(m["role"] for m in removed)
    tools = sorted({c["function"]["name"] for m in removed for c in m.get("tool_calls", [])})
    texts = [(m["role"], m.get("content") or "") for m in removed]
    lines = [
        "Conversation summary:",
        f"- Scope: {len(removed)} earlier messages compacted (user={count['user']}, assistant={count['assistant']}, tool={count['tool']}).",
    ]
    previous = [
        t.removeprefix(PREAMBLE) for r, t in texts if r == "system" and t.startswith(PREAMBLE)
    ]
    lines.extend(previous)
    if tools:
        lines.append("- Tools mentioned: " + ", ".join(tools) + ".")
    users = [t[:160] for r, t in texts if r == "user"][-3:]
    lines += ["- Recent user requests: " + " | ".join(users)] if users else []
    pending = [
        t[:160]
        for _, t in texts
        if any(w in t.lower() for w in ("todo", "next", "pending", "follow up", "remaining"))
    ][-3:]
    if pending:
        lines.append("- Pending work: " + " | ".join(pending))
    files = sorted(
        set(
            re.findall(
                r"[\w./-]+\.(?:rs|ts|tsx|js|json|md|py|vue)\b",
                json.dumps(removed, ensure_ascii=False),
            )
        )
    )[:8]
    if files:
        lines.append("- Key files referenced: " + ", ".join(files))
    if texts:
        lines.append("- Current work: " + texts[-1][1][:200])
    lines += ["- Key timeline:"] + [
        f"- {role}: {text[:160]}" for role, text in texts if role != "system"
    ]
    summary = compress_summary("\n".join(lines), max_chars=max_chars)
    return [{"role": "system", "content": PREAMBLE + summary}, *tail], {
        "removed_message_count": len(removed),
        "summary": summary,
    }


def from_blocks(message):
    role = message["role"].lower()
    text, calls, results, thinking = [], [], [], []
    for block in message.get("blocks", []):
        kind = block["type"]
        if kind == "text":
            text.append(block["text"])
        elif kind == "thinking":
            thinking.append(block)
        elif kind == "tool_use":
            raw = block["input"]
            calls.append(
                {
                    "id": block["id"],
                    "type": "function",
                    "function": {
                        "name": block["name"],
                        "arguments": raw
                        if isinstance(raw, str)
                        else json.dumps(raw, ensure_ascii=False),
                    },
                }
            )
        elif kind == "tool_result":
            results.append(
                {
                    "role": "tool",
                    "tool_call_id": block["tool_use_id"],
                    "name": block.get("tool_name", ""),
                    "content": block["output"],
                    "is_error": block.get("is_error", False),
                }
            )
        else:
            raise ValueError("未知会话 block 类型：" + kind)
    converted = []
    if text or calls or thinking:
        item = {"role": role, "content": "\n".join(text) or None}
        if calls:
            item["tool_calls"] = calls
        if thinking:
            item["thinking"] = thinking
        if message.get("usage"):
            item["usage"] = message["usage"]
        converted.append(item)
    return converted + results


def to_blocks(message):
    blocks = list(message.get("thinking", []))
    if message["role"] == "tool":
        blocks.append(
            {
                "type": "tool_result",
                "tool_use_id": message["tool_call_id"],
                "tool_name": message.get("name", ""),
                "output": message.get("content") or "",
                "is_error": message.get("is_error", False),
            }
        )
    elif message.get("content"):
        blocks.append({"type": "text", "text": message["content"]})
    for call in message.get("tool_calls", []):
        blocks.append(
            {
                "type": "tool_use",
                "id": call["id"],
                "name": call["function"]["name"],
                "input": call["function"]["arguments"],
            }
        )
    result = {"role": message["role"], "blocks": blocks}
    if message.get("usage") is not None:
        result["usage"] = message["usage"]
    return result


def render_markdown(root, path):
    """Upstream CLI transcript format: metadata, text, tool summaries and usage."""
    lines = [
        "# Conversation Export",
        "",
        f"- **Session**: `{root['session_id']}`",
        f"- **File**: `{path}`",
        f"- **Messages**: {len(root['messages'])}",
    ]
    if root.get("workspace_root"):
        lines.append(f"- **Workspace**: `{root['workspace_root']}`")
    if root.get("fork"):
        fork = root["fork"]
        lines.append(
            f"- **Forked from**: `{fork['parent_session_id']}` (branch `{fork.get('branch_name') or '(unnamed)'}`)"
        )
    if root.get("compaction"):
        compaction = root["compaction"]
        lines.append(
            f"- **Compactions**: {compaction.get('count', 0)} (last removed {compaction.get('removed_message_count', 0)} messages)"
        )
    lines.extend(["", "---", ""])
    for index, message in enumerate(root["messages"], 1):
        lines.extend([f"## {index}. {message['role'].title()}", ""])
        for block in message.get("blocks", []):
            kind = block["type"]
            if kind == "text":
                if text := block["text"].rstrip():
                    lines.extend([text, ""])
            elif kind in {"tool_use", "tool_result"}:
                key = block.get("id", block.get("tool_use_id", ""))
                short = key if len(key) <= 12 else key[:12] + "…"
                if kind == "tool_use":
                    label = f"**Tool call** `{block['name']}` _(id `{short}`)_"
                    payload = block["input"]
                else:
                    status = "error" if block.get("is_error") else "ok"
                    label = (
                        f"**Tool result** `{block.get('tool_name', '')}` _(id `{short}`, {status})_"
                    )
                    payload = block["output"]
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except ValueError:
                        payload = " ".join(payload.split())
                summary = (
                    json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    if not isinstance(payload, str)
                    else payload
                )
                if len(summary) > 280:
                    summary = summary[:279] + "…"
                lines.append(label)
                if summary:
                    lines.append("> " + summary)
                lines.append("")
        if usage := message.get("usage"):
            lines.extend(
                [
                    f"_tokens: in={usage.get('input_tokens', 0)} out={usage.get('output_tokens', 0)} cache_create={usage.get('cache_creation_input_tokens', 0)} cache_read={usage.get('cache_read_input_tokens', 0)}_",
                    "",
                ]
            )
    return "\n".join(lines)


class Sessions:
    def __init__(self, db, jobs, store):
        self.db, self.jobs, self.store = db, jobs, store

    def resolve(self, reference="latest", exclude_id=None):
        if reference in {"latest", "last", "recent"}:
            rows = self.db.all(
                "SELECT id FROM sessions WHERE messages != '[]' ORDER BY updated_at DESC"
            )
            workspace = Path(self.store.current.workspace).resolve()
            rows = [
                row
                for row in rows
                if row["id"] != exclude_id
                and Path(
                    (self.jobs.record("session_metadata", row["id"]) or {}).get(
                        "workspace_root", str(workspace)
                    )
                ).resolve()
                == workspace
            ]
            if not rows:
                raise ValueError("当前工作区没有可恢复的其他非空会话")
            reference = rows[0]["id"]
        session = self.db.session(reference)
        if not session:
            raise ValueError("会话不存在：" + reference)
        return session

    def exists(self, reference):
        try:
            self.resolve(reference)
            return True
        except ValueError:
            return False

    def delete(self, reference, active_id=None):
        session = self.resolve(reference)
        key = session["id"]
        if key == active_id:
            raise ValueError("不能删除当前活动会话；请先切换会话")
        # Keep a recoverable export before removing the managed record.
        backup = self.store.data / "deleted-sessions" / (key + ".json")
        backup.parent.mkdir(parents=True, exist_ok=True)
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM runs WHERE session_id=? AND status='running'", (key,)
            ).fetchone():
                raise ValueError("会话正在运行，不能删除")
            if connection.execute(
                "SELECT 1 FROM jobs WHERE session_id=? AND status IN ('running','queued')", (key,)
            ).fetchone():
                raise ValueError("会话关联任务尚未结束，不能删除")
            backup.write_text(self.export(key), encoding="utf-8")
            connection.execute("DELETE FROM sessions WHERE id=?", (key,))
        return {
            "kind": "session_delete",
            "action": "delete",
            "status": "ok",
            "session_id": key,
            "deleted": True,
            "backup": str(backup),
            "run_history_preserved": True,
        }

    def import_text(self, text, allow_workspace_mismatch=False):
        if len(text) > 10_000_000:
            raise ValueError("会话超过 10 MB")
        try:
            root = json.loads(text)
        except ValueError:
            records = [json.loads(line) for line in text.splitlines() if line.strip()]
            meta = next((r for r in records if r.get("type") == "session_meta"), {})
            root = {
                **meta,
                "messages": [r["message"] for r in records if r.get("type") == "message"],
            }
            compactions = [r for r in records if r.get("type") == "compaction"]
            if compactions:
                root["compaction"] = {k: v for k, v in compactions[-1].items() if k != "type"}
            root["prompt_history"] = [r for r in records if r.get("type") == "prompt_history"]
            if any(
                r.get("type") not in {"session_meta", "message", "compaction", "prompt_history"}
                for r in records
            ):
                raise ValueError("会话 JSONL 包含未知记录类型")
        if not isinstance(root, dict) or not isinstance(root.get("messages"), list):
            raise ValueError("会话必须包含 messages 数组")
        workspace = root.get("workspace_root")
        if (
            workspace
            and Path(workspace).resolve() != Path(self.store.current.workspace).resolve()
            and not allow_workspace_mismatch
        ):
            raise ValueError("会话属于其他工作区；请切换工作区或显式允许导入副本")
        messages = []
        for message in root["messages"]:
            if message.get("role") not in {"system", "user", "assistant", "tool"}:
                raise ValueError("会话 role 不合法")
            messages.extend(from_blocks(message) if "blocks" in message else [message])
        # Reject dangling/duplicate tool results; providers reject these and they can misattribute outputs.
        pending = set()
        for message in messages:
            if message["role"] == "tool":
                call = message.get("tool_call_id")
                if call not in pending:
                    raise ValueError("会话包含没有匹配调用的工具结果")
                pending.remove(call)
            else:
                if pending:
                    raise ValueError("会话缺少工具结果")
                pending.update(c["id"] for c in message.get("tool_calls", []))
        if pending:
            raise ValueError("会话末尾存在未完成的工具调用")
        session = self.db.create_session(root.get("title", "导入会话"))
        self.db.save_messages(session["id"], messages)
        self.jobs.put(
            "session_metadata",
            session["id"],
            {
                **{k: v for k, v in root.items() if k != "messages"},
                "source_workspace_root": workspace,
                "workspace_root": self.store.current.workspace,
            },
        )
        return self.db.session(session["id"])

    def export(self, reference="latest", format="json"):
        session = self.resolve(reference)
        meta = self.jobs.record("session_metadata", session["id"]) or {}
        root = {
            **meta,
            "version": 1,
            "session_id": session["id"],
            "title": session["title"],
            "created_at_ms": int(datetime.fromisoformat(session["created_at"]).timestamp() * 1000),
            "updated_at_ms": int(datetime.fromisoformat(session["updated_at"]).timestamp() * 1000),
            "workspace_root": meta.get("workspace_root", self.store.current.workspace),
            "model": meta.get("model", self.store.current.model),
            "messages": [to_blocks(m) for m in session["messages"]],
        }
        root.pop("id", None)
        if format == "markdown":
            return self.store.redact(render_markdown(root, str(self.db.path)))
        if format == "jsonl":
            records = [
                {
                    "type": "session_meta",
                    **{
                        k: v
                        for k, v in root.items()
                        if k not in {"messages", "compaction", "prompt_history", "type"}
                    },
                },
                *([{"type": "compaction", **root["compaction"]}] if root.get("compaction") else []),
                *[{"type": "prompt_history", **entry} for entry in root.get("prompt_history", [])],
                *[{"type": "message", "message": m} for m in root["messages"]],
            ]
            return self.store.redact(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n"
            )
        return self.store.redact(json.dumps(root, ensure_ascii=False, indent=2))

    def fork(self, reference, name=None):
        parent = self.resolve(reference)
        child = self.db.create_session(name or parent["title"] + " · 分支")
        self.db.save_messages(child["id"], parent["messages"])
        metadata = self.jobs.record("session_metadata", parent["id"]) or {}
        metadata.pop("id", None)
        self.jobs.put(
            "session_metadata",
            child["id"],
            {
                **metadata,
                "fork": {"parent_session_id": parent["id"], "branch_name": name},
                "workspace_root": metadata.get("workspace_root", self.store.current.workspace),
            },
        )
        return self.db.session(child["id"])

    def compact(self, reference, preserve=4, mode="standard"):
        session = self.resolve(reference)
        if self.db.all(
            "SELECT id FROM runs WHERE session_id=? AND status='running'", (session["id"],)
        ):
            raise ValueError("会话运行中不能压缩")
        source = session["messages"]
        trident_stats = None
        if mode not in {"standard", "trident"}:
            raise ValueError("unknown compaction mode")
        if mode == "trident":
            from .trident import compact

            boundary = max(0, len(source) - preserve)
            while 0 < boundary < len(source) and source[boundary]["role"] == "tool":
                boundary -= 1
            head, trident_stats = compact(source[:boundary])
            source = head + source[boundary:]
        messages, metadata = compact_messages(source, preserve)
        if trident_stats:
            metadata["trident"] = trident_stats
            metadata["removed_message_count"] += (
                trident_stats["original_message_count"] - trident_stats["final_message_count"]
            )
        if metadata["removed_message_count"]:
            old = self.jobs.record("session_metadata", session["id"]) or {}
            old.pop("id", None)
            metadata["count"] = old.get("compaction", {}).get("count", 0) + 1
            self.jobs.put("session_metadata", session["id"], {**old, "compaction": metadata})
            self.db.save_messages(session["id"], messages)
        return {"session_id": session["id"], **metadata}
