"""Standalone Python CLI: no HTTP server required, one runtime shared with Vue."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

import httpx

from .agent import Agent
from .commands import Commands
from .config import DATA, ROOT, ConfigStore
from .db import Database
from .extensions import CATALOG, Extensions
from .oauth import OAuth
from .providers import Provider
from .rag import RagService
from .tools import Tools
from .tunnel import Tunnel

VERSION = "0.8.0"
LOCAL = {
    "status",
    "doctor",
    "config",
    "models",
    "model",
    "settings",
    "marketplace",
    "login",
    "logout",
    "session",
    "sessions",
    "resume",
    "export",
    "import",
    "compact",
    "agents",
    "skills",
    "plugins",
    "plugin",
    "mcp",
    "sandbox",
    "state",
    "init",
    "setup",
    "diff",
    "version",
    "bootstrap-plan",
    "dump-manifests",
    "acp",
}


def error_payload(message, kind="runtime_error", hint=None):
    if ": " in message and message.split(": ", 1)[0] in {
        "unexpected_extra_args",
        "unknown_option",
        "missing_flag_value",
        "invalid_flag_value",
        "missing_prompt",
        "missing_argument",
        "invalid_permission_mode",
        "unknown_help_topic",
        "unsupported_models_action",
        "unsupported_config_section",
        "interactive_only",
    }:
        kind, message = message.split(": ", 1)
    if "\nUsage:" in message:
        message, usage = message.split("\nUsage:", 1)
        hint = "Usage:" + usage
    return {
        "type": "error",
        "kind": kind,
        "status": "error",
        "error_kind": kind,
        "error": message,
        "message": message,
        "action": "abort",
        "hint": hint,
        "exit_code": 1,
    }


class CliParser(argparse.ArgumentParser):
    def error(self, message):
        arguments = getattr(self, "raw_arguments", [])
        output = os.environ.get("CLAW_OUTPUT_FORMAT", "text").lower()
        for index, value in enumerate(arguments):
            if value.startswith("--output-format="):
                output = value.split("=", 1)[1].lower()
            elif value == "--output-format" and index + 1 < len(arguments):
                output = arguments[index + 1].lower()
            elif value == "--json":
                output = "json"
        kind = (
            "missing_flag_value"
            if "expected" in message and "argument" in message
            else "unknown_option"
            if "unrecognized arguments" in message
            else "invalid_flag_value"
        )
        if "invalid choice" in message and any(
            value.split("=", 1)[0] == "--permission-mode" for value in arguments
        ):
            kind = "invalid_permission_mode"
        payload = error_payload(message, kind, self.format_usage().strip())
        if output in {"json", "stream-json"}:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            self.print_usage(sys.stderr)
            print(message, file=sys.stderr)
        raise SystemExit(1)


def parser():
    p = CliParser(prog="myclaude", add_help=False, allow_abbrev=False)
    p.add_argument("words", nargs="*")
    p.add_argument("--help", "-h", action="store_true")
    p.add_argument("--version", "-v", "-V", action="store_true")
    p.add_argument("--prompt", "-p")
    p.add_argument("--session", "--resume", nargs="?", const="latest")
    p.add_argument(
        "--permission",
        "--permission-mode",
        choices=["read-only", "workspace-write", "danger-full-access"],
        default=None,
    )
    p.add_argument(
        "--output-format",
        type=str.lower,
        choices=["text", "json", "stream-json"],
        default=os.environ.get("CLAW_OUTPUT_FORMAT", "text").lower(),
    )
    p.add_argument("--json", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--cwd", "-C", "--directory")
    p.add_argument("--allow-broad-cwd", action="store_true")
    p.add_argument("--data-dir", type=Path, default=DATA)
    p.add_argument("--model")
    p.add_argument(
        "--reasoning",
        "--reasoning-effort",
        choices=["none", "minimal", "low", "medium", "high", "xhigh"],
    )
    p.add_argument("--base-commit")
    p.add_argument("--allowed-tools", "--allowedTools", action="append")
    p.add_argument("--print", action="store_true")
    p.add_argument("--stdin", "--prompt-stdin", action="store_true")
    p.add_argument(
        "--compact", action="store_true", help="Print only assistant text in prompt mode"
    )
    p.add_argument("--dangerously-skip-permissions", "--skip-permissions", action="store_true")
    p.add_argument("--dump-manifests", action="store_true")
    p.add_argument("--manifests-dir", type=Path)
    p.add_argument("--acp", "-acp", action="store_true")
    p.add_argument("--print-system-prompt", action="store_true")
    p.add_argument("--date")
    p.add_argument("--output", "-o", type=Path)
    p.add_argument("--format", choices=["markdown", "json", "jsonl"], default="markdown")
    return p


def emit(value, args):
    if isinstance(value, str) and args.output_format == "text":
        print(value)
    else:
        print(
            json.dumps(
                value, ensure_ascii=False, indent=None if args.output_format == "stream-json" else 2
            )
        )


async def run(args):
    store = ConfigStore(args.data_dir, ROOT)
    changes = {}
    if args.cwd:
        changes["workspace"] = str(Path(args.cwd).resolve(strict=True))
    store.current = type(store.current).model_validate({**store.current.model_dump(), **changes})
    from .config_layers import activate

    activate(store)
    args.permission = args.permission or store.current.permission_mode
    if args.dangerously_skip_permissions:
        args.permission = "danger-full-access"
    if args.model:
        changes["model"] = args.model
    if args.reasoning:
        changes["reasoning_effort"] = args.reasoning
    if args.allowed_tools is not None:
        changes["allowed_tools"] = list(
            dict.fromkeys(
                s.strip() for value in args.allowed_tools for s in value.split(",") if s.strip()
            )
        )
    store.current = type(store.current).model_validate({**store.current.model_dump(), **changes})
    db = Database(args.data_dir / "app.sqlite", recover=False)
    async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=12), trust_env=False) as client:
        oauth = OAuth(store, client)
        provider, tunnel = Provider(client, oauth, store), Tunnel()
        ext = Extensions(db, store, client)
        ext.oauth = oauth

        async def answer_question(question, options):
            if not sys.stdin.isatty():
                raise ValueError("非交互 CLI 无法回答问题；请通过交互终端或网页运行")
            print(question + ("\n" + " / ".join(options) if options else ""), file=sys.stderr)
            return await asyncio.to_thread(input, "回答 › ")

        ext.question_handler = answer_question
        rag = RagService(db, provider, client, tunnel)
        agent = Agent(db, provider, Tools(rag, ext), store)
        ext.jobs.agent = agent
        try:
            if args.print_system_prompt:
                from .prompt import assemble

                assembled = await assemble(store.current, args.permission, args.date)
                emit(
                    {
                        "kind": "system_prompt",
                        "status": "ok",
                        **assembled,
                        "workspace": store.current.workspace,
                    },
                    args,
                )
                return 0
            if args.base_commit:
                from .git_context import check_base

                state = await check_base(store.current.workspace, args.base_commit)
                if state["state"] in {"diverged", "not_a_git_repo"}:
                    print(json.dumps({"warning": "stale_base", **state}), file=sys.stderr)
            session_id = None
            if args.session:
                reference = args.session
                loaded = (
                    ext.sessions.import_text(Path(reference).read_text(encoding="utf-8-sig"))
                    if Path(reference).is_file()
                    else ext.sessions.resolve(reference)
                )
                session_id = loaded["id"]
            words = args.words
            if args.session and not words and not args.prompt:
                emit(
                    {"kind": "resume", "status": "ok", "result": ext.sessions.resolve(session_id)},
                    args,
                )
                return 0
            command, rest = (words[0], words[1:]) if words else (None, [])
            command = {"model": "models", "marketplace": "plugins", "sessions": "session"}.get(
                command, command
            )
            if command == "settings":
                command, rest = "config", ["settings"]
            if command in {"agents", "skills"} and not args.prompt:
                from .cli_local import definitions_value

                report = definitions_value(ext, command, rest)
                if report is not None:
                    emit(report, args)
                    return 1 if report["status"] == "error" else 0
            if command in LOCAL and not args.prompt:
                if command == "login":
                    if rest == ["status"]:
                        value = oauth.status()
                    else:
                        if not sys.stdin.isatty():
                            raise ValueError(
                                "login 需要交互终端粘贴授权回调；也可使用 Web 设置页 OAuth 登录"
                            )
                        authorization = oauth.begin()
                        print(authorization["authorize_url"], file=sys.stderr)
                        callback = await asyncio.to_thread(input, "完成授权后粘贴完整回调 URL › ")
                        value = await oauth.complete(callback.strip())
                elif command == "logout":
                    value = await oauth.logout()
                elif command in {"sessions", "session"} and not rest:
                    value = db.all(
                        "SELECT id,title,updated_at FROM sessions ORDER BY updated_at DESC"
                    )
                elif command == "export":
                    if len(rest) > 1 or (rest and args.output):
                        raise ValueError(
                            "unexpected_extra_args: export [PATH] [--session SESSION] [--output PATH]"
                        )
                    target = args.output or (Path(rest[0]) if rest else None)
                    selected = ext.sessions.resolve(session_id or "latest")
                    value = ext.sessions.export(selected["id"], args.format)
                    if target:
                        target.write_text(value, encoding="utf-8")
                    if not target and args.output_format == "text":
                        print(value)
                        return 0
                    emit(
                        {
                            "kind": "export",
                            "action": "export",
                            "status": "ok",
                            "session_id": selected["id"],
                            "messages": len(selected["messages"]),
                            "file": str(target.resolve()) if target else str(db.path),
                            **(
                                {"markdown" if args.format == "markdown" else "content": value}
                                if not target
                                else {}
                            ),
                        },
                        args,
                    )
                    return 0
                elif command == "import":
                    if not rest:
                        raise ValueError("Usage: myclaude import PATH")
                    value = ext.sessions.import_text(Path(rest[0]).read_text(encoding="utf-8-sig"))
                elif command == "resume":
                    session_id = ext.sessions.resolve(rest[0] if rest else "latest")["id"]
                    if len(rest) > 1:
                        return await interact(ext, agent, session_id, " ".join(rest[1:]), args)
                    value = ext.sessions.resolve(session_id)
                elif command == "config":
                    if rest and rest[0] in {"inspect", "import"}:
                        from .config_layers import inspect

                        value = inspect(
                            store, rest[1] if len(rest) > 1 else None, rest[0] == "import"
                        )
                    else:
                        from .cli_local import config_value

                        emit(
                            config_value(ext, rest[0] if rest and rest[0] != "show" else None), args
                        )
                        return 0
                elif command == "models":
                    from .cli_local import models_value

                    emit(models_value(store), args)
                    return 0
                elif command == "diff":
                    from .cli_local import diff_value

                    emit(await diff_value(store.current.workspace), args)
                    return 0
                elif command == "state":
                    from .remote import ProxyBootstrap

                    value = {
                        "remote": ProxyBootstrap.from_env().public(),
                        "sessions": db.all(
                            "SELECT id,title,updated_at FROM sessions ORDER BY updated_at DESC"
                        ),
                        "jobs": ext.jobs.list(),
                        "mcp": ext.mcp.list(),
                        "plugins": ext.plugins.list(),
                    }
                else:
                    actual = "plugin" if command == "plugins" else command
                    if actual in {"agents", "skills", "plugin", "mcp"} and not rest:
                        rest = ["list"]
                    argument = " ".join(
                        json.dumps(x.replace("\\", "/"), ensure_ascii=False) for x in rest
                    )
                    if actual == "compact":
                        argument = " ".join(rest)
                    if (
                        actual in {"session", "sessions"}
                        and rest
                        and rest[0] == "delete"
                        and args.force
                    ):
                        argument += " --force"
                    value = await Commands(ext).execute(
                        "/" + actual + (" " + argument if argument else ""),
                        session_id,
                        args.permission,
                    )
                emit({"kind": command, "status": "ok", "result": value}, args)
                return 0
            prompt = args.prompt or " ".join(words)
            if args.stdin:
                piped = sys.stdin.read().strip()
                prompt = "\n".join(part for part in (prompt, piped) if part)
            if prompt:
                return await interact(ext, agent, session_id, prompt, args)
            if not sys.stdin.isatty():
                prompt = sys.stdin.read().strip()
                if prompt:
                    return await interact(ext, agent, session_id, prompt, args)
                raise ValueError("请提供 -p PROMPT，或使用交互终端")
            session_id = session_id or db.create_session("CLI 会话")["id"]
            print("MyClaude Python · /help 查看命令，/exit 退出", file=sys.stderr)
            while True:
                try:
                    prompt = await asyncio.to_thread(input, "› ")
                except EOFError:
                    return 0
                if prompt.strip() in {"/exit", "/quit"}:
                    return 0
                if prompt.strip():
                    await interact(ext, agent, session_id, prompt, args)
                    session_id = getattr(args, "active_session_id", session_id)
        finally:
            await ext.close()
            await tunnel.close()


async def interact(ext, agent, session_id, prompt, args):
    session_id = session_id or ext.db.create_session("CLI 会话")["id"]
    if not prompt.startswith("/") and ext.db.all(
        "SELECT id FROM runs WHERE session_id=? AND status='running'", (session_id,)
    ):
        raise ValueError("该会话已有正在执行的任务")
    if prompt.startswith("/"):
        result = await Commands(ext).execute(prompt, session_id, args.permission)
        if result["kind"] != "prompt":
            if result["kind"] == "session":
                args.active_session_id = result["session_id"]
            elif result["kind"] == "permission":
                args.permission = result["permission"]
            elif result["kind"] == "attachment":
                args.attachment_ids = [
                    *getattr(args, "attachment_ids", []),
                    *[r["id"] for r in result["items"]],
                ][:4]
            emit(result, args)
            return 0
        prompt, permission = result["prompt"], result.get("permission", args.permission)
    else:
        permission = args.permission
    await ext.plugins.startup()
    for server in ext.mcp.list():
        if server["enabled"]:
            await ext.mcp.discover(server["name"], server["permission"])
    final, text, last_assistant = None, "", ""
    tool_uses, tool_results, cache_events = [], [], []
    compaction = None
    async for event in agent.run(
        ext.db.session(session_id),
        prompt,
        permission,
        ext.store.current.model_copy(deep=True),
        getattr(args, "attachment_ids", []),
    ):
        args.attachment_ids = []
        if args.output_format == "stream-json":
            emit(event, args)
        elif event["type"] == "delta":
            text += event["text"]
            if args.output_format == "text" and not args.compact:
                print(event["text"], end="", flush=True)
        if event["type"] == "assistant":
            last_assistant = event["message"].get("content") or ""
        elif event["type"] == "tool_start":
            tool_uses.append(
                {"id": event["call_id"], "name": event["name"], "input": event["arguments"]}
            )
        elif event["type"] == "tool_result":
            tool_results.append(
                {
                    "tool_use_id": event["call_id"],
                    "tool_name": event["name"],
                    "output": json.dumps(event["output"], ensure_ascii=False),
                    "is_error": event["is_error"],
                }
            )
        elif event["type"] == "cache_break":
            cache_events.append(
                {
                    k: event[k]
                    for k in (
                        "unexpected",
                        "reason",
                        "previous_cache_read_input_tokens",
                        "current_cache_read_input_tokens",
                        "token_drop",
                    )
                }
            )
        elif event["type"] == "auto_compact":
            compaction = {
                "removed_messages": event["removed_message_count"],
                "notice": "Earlier context compacted",
            }
        if event["type"] == "run_end":
            final = event
    if args.output_format == "json":
        emit(
            {
                "kind": "prompt",
                "session_id": session_id,
                "result": text,
                "message": last_assistant,
                "model": ext.store.current.model,
                **(
                    {"compact": True}
                    if args.compact
                    else {
                        "iterations": (final or {}).get("turns", 0),
                        "tool_uses": tool_uses,
                        "tool_results": tool_results,
                        "prompt_cache_events": cache_events,
                        "auto_compaction": compaction,
                    }
                ),
                **(final or {"status": "failed"}),
            },
            args,
        )
    elif args.output_format == "text":
        print(last_assistant if args.compact else "")
        if final and final.get("error"):
            print(final["error"], file=sys.stderr)
    return 0 if final and final["status"] == "completed" else 1


def prepare_arguments(p):
    """Preserve literal -p input and fail locally before loading credentials/config."""
    raw = p.raw_arguments
    output, index = [], 0
    value_flags = {
        option
        for action in p._actions
        if action.nargs not in (0, "*")
        for option in action.option_strings
    }
    while index < len(raw):
        value = raw[index]
        if value == "--":
            output.extend(raw[index:])
            break
        flag, equal, inline = value.partition("=")
        if flag in {"-p", "--prompt"}:
            prompt = inline if equal else raw[index + 1] if index + 1 < len(raw) else None
            consumed = 1 if equal else 2
            if prompt == "--" and not equal:
                prompt = raw[index + 2] if index + 2 < len(raw) else None
                consumed = 3
            elif prompt and prompt.startswith("-") and not equal:
                prompt = None
            if not prompt or not prompt.strip():
                p.error(
                    "missing_prompt: prompt text is required.\nUsage: myclaude -p <text> or -p -- <literal>"
                )
            output.append("--prompt=" + prompt)
            index += consumed
            continue
        next_value = inline if equal else raw[index + 1] if index + 1 < len(raw) else None
        if flag in {"--allowed-tools", "--allowedTools"} and (
            not next_value
            or not next_value.strip()
            or next_value.startswith("-")
            or (not equal and next_value in LOCAL)
        ):
            p.error(
                "missing_argument: allowed tools are required.\nUsage: --allowedTools read_file,search_files"
            )
        if flag in {"--manifests-dir", "--output"} and equal and not inline:
            p.error(
                "missing_flag_value: path is required.\nUsage: myclaude dump-manifests --manifests-dir PATH; myclaude export --output PATH"
            )
        if (
            flag == "--base-commit"
            and next_value is not None
            and not re.fullmatch(r"[0-9a-fA-F]{7,64}", next_value)
        ):
            p.error(
                "invalid_flag_value: base commit must be a 7–64 digit hex SHA.\nUsage: --base-commit <git-sha>"
            )
        output.append(value)
        index += 1
        if (
            flag in value_flags
            and not equal
            and next_value is not None
            and not next_value.startswith("-")
        ):
            output.append(next_value)
            index += 1
    return output


def validate_operands(p, args):
    words = args.words
    if not words or args.prompt:
        return
    command, rest = words[0], words[1:]
    if command in {item["name"] for item in CATALOG["commands"]} - LOCAL - {"system-prompt"}:
        p.error(
            "interactive_only: 此命令应通过 /"
            + command
            + ' 执行。\nUsage: myclaude -p "/'
            + command
            + '"'
        )
    if args.force and not (command in {"session", "sessions"} and rest and rest[0] == "delete"):
        p.error("invalid_flag_value: --force 仅用于 session delete")
    usage = None
    if command in {"model", "models"}:
        if len(rest) > 1:
            usage = command + " [help|list|status]"
        elif rest and rest[0] not in {"list", "status"}:
            p.error(
                "unsupported_models_action: 未知模型查询操作。\nUsage: myclaude models [help|list|status]"
            )
    elif (
        command
        in {"settings", "init", "setup", "diff", "state", "version", "bootstrap-plan", "logout"}
        and rest
    ):
        usage = command
    elif command == "login" and rest not in ([], ["status"]):
        usage = "login [status]"
    elif command == "export" and (len(rest) > 1 or rest and args.output):
        usage = "export [PATH] [--session SESSION] [--output PATH]"
    elif (
        command == "config" and rest and len(rest) > (2 if rest[0] in {"inspect", "import"} else 1)
    ):
        usage = "config show|env|FIELD|inspect [PATH]|import [PATH]"
    elif (
        command in {"agents", "skills"}
        and rest
        and rest[0] in {"show", "info", "describe"}
        and len(rest) > 2
    ):
        usage = command + " show NAME"
    if usage:
        p.error("unexpected_extra_args: extra positional arguments.\nUsage: myclaude " + usage)


def main(argv=None):
    p = parser()
    p.raw_arguments = list(sys.argv[1:] if argv is None else argv)
    args = p.parse_intermixed_args(prepare_arguments(p))
    if args.output_format not in {"text", "json", "stream-json"}:
        p.error("CLAW_OUTPUT_FORMAT must be text, json or stream-json")
    if args.json:
        args.output_format = "stream-json"
    from .cli_local import help_topic, help_value

    topic = help_topic(args.words, args)
    if topic is not None:
        if topic:
            try:
                value = help_value(topic)
            except ValueError as error:
                p.error(str(error))
            emit(value if args.output_format != "text" else value["message"], args)
        else:
            message = "Usage: " + p.format_help().removeprefix("usage: ")
            emit(
                {"kind": "help", "status": "ok", "message": message, "commands": sorted(LOCAL)}
                if args.output_format != "text"
                else message,
                args,
            )
        return 0
    validate_operands(p, args)
    if args.cwd and not args.allow_broad_cwd:
        candidate = Path(args.cwd).expanduser().resolve()
        if candidate == Path(candidate.anchor) or candidate == Path.home().resolve():
            p.error(
                "invalid_flag_value: 工作区过于宽泛；选择项目目录，或显式添加 --allow-broad-cwd"
            )
    if args.words and args.words[0] == "system-prompt":
        args.print_system_prompt = True
        args.words = args.words[1:]
    if args.compact and args.words and args.words[0] in LOCAL:
        p.error("--compact is only supported with prompt mode")
    if args.words and args.words[0] == "prompt":
        args.words = args.words[1:]
    if args.version or args.words == ["version"]:
        emit({"kind": "version", "status": "ok", "version": VERSION, "runtime": "python"}, args)
        return 0
    try:
        from .bootstrap import acp_status, manifests, plan

        if args.words == ["init"] and not args.prompt:
            from .cli_local import initialize

            value = initialize(args.cwd or Path.cwd())
            emit(value if args.output_format != "text" else value["message"], args)
            return 0

        if args.words == ["bootstrap-plan"]:
            emit(plan(), args)
            return 0
        if args.acp or args.words and args.words[0] == "acp":
            operands = args.words if args.acp else args.words[1:]
            if operands not in ([], ["serve"]):
                emit(
                    {"kind": "unsupported_acp_invocation", "status": "error", "supported": False},
                    args,
                )
                return 2
            emit(acp_status(), args)
            return 2
        if args.dump_manifests or args.words == ["dump-manifests"]:
            emit(
                manifests(args.manifests_dir or args.cwd or ROOT / "workspace", args.data_dir), args
            )
            return 0
        return asyncio.run(run(args))
    except (Exception, KeyboardInterrupt) as error:
        from pydantic import ValidationError

        message = (
            str(error)
            if isinstance(error, ValueError) and not isinstance(error, ValidationError)
            else type(error).__name__
        )
        emit(error_payload(message), args)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
