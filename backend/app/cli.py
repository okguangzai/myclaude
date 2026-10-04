"""Standalone Python CLI: no HTTP server required, one runtime shared with Vue."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

from .agent import Agent
from .commands import Commands
from .config import DATA, ROOT, ConfigStore
from .db import Database
from .extensions import Extensions
from .oauth import OAuth
from .providers import Provider
from .rag import RagService
from .tools import Tools
from .tunnel import Tunnel

VERSION = "0.4.0"
LOCAL = {
    "status",
    "doctor",
    "config",
    "models",
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


def parser():
    p = argparse.ArgumentParser(prog="myclaude", add_help=False)
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
    p.add_argument("--cwd", "-C", "--directory")
    p.add_argument("--data-dir", type=Path, default=DATA)
    p.add_argument("--model")
    p.add_argument(
        "--reasoning",
        "--reasoning-effort",
        choices=["none", "minimal", "low", "medium", "high", "xhigh"],
    )
    p.add_argument("--base-commit")
    p.add_argument("--allowed-tools", "--allowedTools")
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
    p.add_argument("--output", type=Path)
    p.add_argument("--format", choices=["json", "jsonl"], default="json")
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
        changes["allowed_tools"] = [s.strip() for s in args.allowed_tools.split(",") if s.strip()]
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
            if command in LOCAL and not args.prompt:
                if command in {"sessions", "session"} and not rest:
                    value = db.all(
                        "SELECT id,title,updated_at FROM sessions ORDER BY updated_at DESC"
                    )
                elif command == "export":
                    value = ext.sessions.export(
                        session_id or (rest[0] if rest else "latest"), args.format
                    )
                    if args.output:
                        args.output.write_text(value, encoding="utf-8")
                        value = {"output": str(args.output.resolve())}
                    else:
                        print(value)
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
                    elif rest and rest[0] == "env":
                        value = {
                            "env": {
                                key: "[REDACTED]"
                                for key in getattr(store, "runtime_environment", {})
                            }
                        }
                    else:
                        value = store.public()
                    if rest and rest[0] not in {"inspect", "import", "env"}:
                        if rest[0] not in value:
                            raise ValueError("未知或保密的配置字段")
                        value = {rest[0]: value[rest[0]]}
                elif command == "models":
                    value = {
                        "model": store.current.model,
                        "provider": store.current.provider,
                        "base_url": store.current.base_url,
                    }
                    if rest and rest[0] not in {"list", "status"}:
                        store.update({"model": rest[-1]})
                        value["model"] = store.current.model
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
    if ext.db.all("SELECT id FROM runs WHERE session_id=? AND status='running'", (session_id,)):
        raise ValueError("该会话已有正在执行的任务")
    if prompt.startswith("/"):
        result = await Commands(ext).execute(prompt, session_id, args.permission)
        if result["kind"] != "prompt":
            if result["kind"] == "session":
                args.active_session_id = result["session_id"]
            elif result["kind"] == "permission":
                args.permission = result["permission"]
            emit(result, args)
            return 0
        prompt, permission = result["prompt"], result.get("permission", args.permission)
    else:
        permission = args.permission
    await ext.plugins.startup()
    for server in ext.mcp.list():
        if server["enabled"]:
            await ext.mcp.discover(server["name"], server["permission"])
    final, text = None, ""
    async for event in agent.run(
        ext.db.session(session_id), prompt, permission, ext.store.current.model_copy(deep=True)
    ):
        if args.output_format == "stream-json":
            emit(event, args)
        elif event["type"] == "delta":
            text += event["text"]
            if args.output_format == "text":
                print(event["text"], end="", flush=True)
        if event["type"] == "run_end":
            final = event
    if args.output_format == "json":
        emit(
            {
                "kind": "prompt",
                "session_id": session_id,
                "result": text,
                **(final or {"status": "failed"}),
            },
            args,
        )
    elif args.output_format == "text":
        print()
        if final and final.get("error"):
            print(final["error"], file=sys.stderr)
    return 0 if final and final["status"] == "completed" else 1


def main(argv=None):
    p = parser()
    args = p.parse_intermixed_args(argv)
    if args.output_format not in {"text", "json", "stream-json"}:
        p.error("CLAW_OUTPUT_FORMAT must be text, json or stream-json")
    if args.words and args.words[0] == "system-prompt":
        args.print_system_prompt = True
        args.words = args.words[1:]
    if args.compact and args.words and args.words[0] in LOCAL:
        p.error("--compact is only supported with prompt mode")
    if args.words and args.words[0] == "prompt":
        args.words = args.words[1:]
    if args.json:
        args.output_format = "stream-json"
    if args.version or args.words == ["version"]:
        emit({"kind": "version", "status": "ok", "version": VERSION, "runtime": "python"}, args)
        return 0
    if args.help or (args.words and args.words[0] == "help"):
        message = "Usage: " + p.format_help().removeprefix("usage: ")
        emit(
            {"kind": "help", "status": "ok", "message": message, "commands": sorted(LOCAL)}
            if args.output_format != "text"
            else message,
            args,
        )
        return 0
    try:
        from .bootstrap import acp_status, manifests, plan

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

        emit(
            {
                "kind": "error",
                "status": "error",
                "message": str(error)
                if isinstance(error, ValueError) and not isinstance(error, ValidationError)
                else type(error).__name__,
            },
            args,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
