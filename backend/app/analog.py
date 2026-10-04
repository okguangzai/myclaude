"""Lean Agent entry point: narrow tools, TOML presets, resumable files and sequential agents."""

import argparse
import asyncio
import fnmatch
import json
import os
import sys
import tomllib
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .agent import Agent
from .config import DATA, ROOT, ConfigStore
from .db import Database
from .extensions import Extensions
from .providers import Provider
from .sessions import from_blocks
from .tools import DENIED, Tools, definition, safe_path

PRESETS = {
    "audit": "Prioritize security and correctness. Cite file paths and evidence; investigate read-only.",
    "explain": "Teach clearly, define terms and ground claims in repository content.",
    "implement": "Make focused edits; read before writing and explain the changes.",
    "none": "",
}


def infer_preset(prompt):
    text = prompt.lower()
    for kind, cues in (
        (
            "audit",
            (
                "audit",
                "security",
                "review",
                "vuln",
                "threat",
                "pentest",
                "аудит",
                "безопас",
                "审查",
                "安全",
            ),
        ),
        (
            "implement",
            (
                "implement",
                "add",
                "build",
                "create",
                "change",
                "update",
                "refactor",
                "optimize",
                "fix",
                "bug",
                "feature",
                "исправ",
                "добав",
                "实现",
                "修改",
                "修复",
            ),
        ),
        ("explain", ("explain", "why", "how", "what is", "объясни", "почему", "解释", "为什么")),
    ):
        if any(cue in text for cue in cues):
            return kind
    return "none"


class Options(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str | None = None
    stream: bool = False
    output_format: Literal["rich", "json"] = "rich"
    permission: str | None = None
    preset: Literal["auto", "none", "audit", "explain", "implement"] = "auto"
    language: Literal["en", "ru"] = "en"
    no_runtime_enforcer: bool = False
    accept_danger_non_interactive: bool = False
    max_read_bytes: int = Field(default=262144, ge=1, le=10_000_000)
    max_turns: int = Field(default=24, ge=1, le=30)
    max_list_entries: int = Field(default=500, ge=1, le=10000)
    grep_max_lines: int = Field(default=200, ge=1, le=5000)
    glob_max_paths: int = Field(default=2000, ge=1, le=10000)
    glob_max_depth: int = Field(default=32, ge=1, le=100)
    session: str | None = None
    profile: str | None = None
    rag_base_url: str | None = None
    rag_timeout_secs: int = Field(default=30, ge=1, le=300)
    rag_top_k_max: int = Field(default=32, ge=1, le=256)


def resolve(args):
    root = Path(args.workspace).resolve(strict=True)
    config_path = Path(args.config) if args.config else root / ".claw-analog.toml"
    if (args.config or args.strict) and not config_path.is_file():
        raise ValueError("Analog 配置文件不存在")
    raw = tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    provenance = {key: str(config_path) for key in raw}
    for key in Options.model_fields:
        value = getattr(args, key, None)
        if value is not None:
            raw[key], provenance[key] = value, "CLI"
    if args.no_stream:
        raw["stream"], provenance["stream"] = False, "CLI"
    if args.lang:
        raw["language"], provenance["language"] = args.lang, "CLI"
    options = Options.model_validate(raw)
    if options.preset == "auto":
        options.preset = infer_preset(" ".join(args.words))
    if options.preset not in PRESETS:
        raise ValueError("unknown preset")
    permission = options.permission or (
        "workspace-write" if options.preset == "implement" else "read-only"
    )
    options.permission = {
        "readonly": "read-only",
        "write": "workspace-write",
        "allow": "danger-full-access",
        "danger": "danger-full-access",
    }.get(permission.replace("_", "-"), permission)
    if options.permission not in {"read-only", "workspace-write", "danger-full-access", "prompt"}:
        raise ValueError("unknown permission")
    options.rag_base_url = (
        options.rag_base_url or os.environ.get("RAG_BASE_URL", "")
    ).strip().rstrip("/") or None
    return root, options, provenance


class NarrowTools(Tools):
    def __init__(self, ext, options, client):
        super().__init__(None, ext)
        self.options, self.client = options, client

    def definitions(self, permission, rag):
        string = {"type": "string"}
        specs = [
            definition("read_file", "Read UTF-8 file in workspace", {"path": string}, ["path"]),
            definition("list_dir", "List directory entries", {"path": string}, []),
            definition(
                "glob_workspace",
                "Find workspace paths by glob",
                {"pattern": string, "path": string},
                ["pattern"],
            ),
            definition(
                "grep_workspace",
                "Search literal text in workspace files",
                {"pattern": string, "glob": string, "path": string},
                ["pattern"],
            ),
            definition(
                "grep_search",
                "Search literal text",
                {"pattern": string, "path": string, "output_mode": string},
                ["pattern"],
            ),
        ]
        path_list = {"type": "array", "items": string}
        specs.extend(
            [
                definition(
                    "git_diff",
                    "Read workspace Git diff",
                    {
                        "cached": {"type": "boolean"},
                        "rev_range": string,
                        "context_lines": {"type": "integer"},
                        "paths": path_list,
                    },
                    [],
                ),
                definition(
                    "git_log",
                    "Read workspace Git history",
                    {"rev_range": string, "max_count": {"type": "integer"}, "paths": path_list},
                    [],
                ),
            ]
        )
        if self.options.permission != "read-only":
            specs.append(
                definition(
                    "write_file",
                    "Write a UTF-8 file",
                    {"path": string, "content": string},
                    ["path", "content"],
                )
            )
        if self.options.rag_base_url:
            specs.append(
                definition(
                    "retrieve_context",
                    "Query workspace RAG",
                    {"query": string, "top_k": {"type": "integer"}},
                    ["query"],
                )
            )
        if not self.options.no_runtime_enforcer:
            from .policy import tool_allowed

            specs = [
                spec
                for spec in specs
                if tool_allowed(self.extensions.store.current, spec["function"]["name"])
            ]
        return specs

    def paths(self, cfg, relative="."):
        root = safe_path(cfg.workspace, relative)
        count = 0
        if root.is_file():
            yield root
            return
        for directory, dirs, files in os.walk(root, followlinks=False):
            depth = len(Path(directory).relative_to(root).parts)
            dirs[:] = (
                []
                if depth >= self.options.glob_max_depth
                else sorted(
                    d
                    for d in dirs
                    if d.lower() not in DENIED and not (Path(directory) / d).is_symlink()
                )
            )
            for name in sorted(files):
                path = Path(directory) / name
                try:
                    safe_path(cfg.workspace, path.relative_to(cfg.workspace).as_posix())
                except ValueError:
                    continue
                yield path
                count += 1
                if count >= self.options.glob_max_paths:
                    return

    async def execute(self, cfg, permission, name, args):
        allowed = {s["function"]["name"] for s in self.definitions(permission, False)}
        if name not in allowed:
            raise PermissionError("tool is outside lean Agent scope")
        if not self.options.no_runtime_enforcer:
            from .policy import authorize_rules

            await authorize_rules(self.extensions, cfg, name, args)
        if name in {"git_diff", "git_log"}:
            import re

            from .execution import process

            revision = args.get("rev_range", "")
            if revision and (
                revision.startswith("-")
                or not re.fullmatch(r"[A-Za-z0-9_./~^@{}-]{1,256}", revision)
            ):
                raise ValueError("unsafe Git revision")
            paths = args.get("paths", [])
            for path in paths:
                safe_path(cfg.workspace, path)
            command = ["git", "--no-pager", "-c", "core.fsmonitor=false", "-c", "core.hooksPath="]
            if name == "git_diff":
                command += [
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    f"-U{max(0, min(int(args.get('context_lines', 3)), 100))}",
                ]
                if args.get("cached"):
                    command.append("--cached")
            else:
                command += [
                    "log",
                    "--no-decorate",
                    f"--max-count={max(1, min(int(args.get('max_count', 20)), 100))}",
                    "--format=%h %s",
                ]
            if revision:
                command.append(revision)
            command += ["--", *paths]
            result = await process(
                command, cfg.workspace, timeout=15, limit=self.options.max_read_bytes
            )
            if result["exit_code"]:
                raise ValueError("Git query failed: " + result["stderr"][:500])
            return result
        if name == "retrieve_context":
            response = await self.client.post(
                self.options.rag_base_url + "/v1/query",
                json={
                    "query": str(args["query"])[:12000],
                    "top_k": max(1, min(int(args.get("top_k", 8)), self.options.rag_top_k_max)),
                },
                timeout=self.options.rag_timeout_secs,
            )
            if response.is_error:
                raise ValueError(f"RAG HTTP {response.status_code}")
            return response.json()
        if name == "read_file":
            path = safe_path(cfg.workspace, args["path"])
            with path.open("rb") as source:
                raw = source.read(self.options.max_read_bytes + 1)
            return {
                "file": {
                    "path": args["path"],
                    "content": raw[: self.options.max_read_bytes].decode("utf-8", errors="replace"),
                },
                "truncated": len(raw) > self.options.max_read_bytes,
            }
        if name == "list_dir":
            root = safe_path(cfg.workspace, args.get("path", "."))
            entries = []
            for path in sorted(root.iterdir()):
                try:
                    safe_path(cfg.workspace, path.relative_to(cfg.workspace).as_posix())
                except ValueError:
                    continue
                entries.append({"name": path.name, "is_dir": path.is_dir()})
                if len(entries) >= self.options.max_list_entries:
                    break
            return {"entries": entries}
        if name == "write_file":
            if self.options.permission == "prompt":
                if not sys.stdin.isatty():
                    raise PermissionError("non-interactive prompt mode cannot approve writes")
                answer = await asyncio.to_thread(input, f"Write {args['path']}? [y/N] ")
                if answer.strip().lower() not in {"y", "yes"}:
                    raise PermissionError("write denied")
            return await self.execute_builtin(cfg, "workspace-write", name, args)
        matches = []
        for path in self.paths(cfg, args.get("path", ".")):
            relative = path.relative_to(cfg.workspace).as_posix()
            pattern = args.get("glob", "*") if name != "glob_workspace" else args["pattern"]
            if not (
                fnmatch.fnmatchcase(relative, pattern)
                or pattern.startswith("**/")
                and fnmatch.fnmatchcase(relative, pattern[3:])
            ):
                continue
            if name == "glob_workspace":
                matches.append(relative)
                continue
            if path.stat().st_size > self.options.max_read_bytes:
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (UnicodeError, OSError):
                continue
            for index, line in enumerate(lines, 1):
                if args["pattern"] in line:
                    matches.append({"path": relative, "line": index, "text": line[:2000]})
                    if len(matches) >= self.options.grep_max_lines:
                        return {"matches": matches, "numMatches": len(matches), "truncated": True}
        return (
            {"paths": matches}
            if name == "glob_workspace"
            else {"matches": matches, "numMatches": len(matches)}
        )


def read_session(path, workspace):
    if not path or not path.is_file():
        return []
    root = json.loads(path.read_text(encoding="utf-8"))
    if root.get("version") != 1:
        raise ValueError("unsupported session version")
    if root.get("workspace") and Path(root["workspace"]).resolve() != workspace:
        raise ValueError("session belongs to another workspace")
    result = []
    for message in root["messages"]:
        if isinstance(message.get("content"), str) or "tool_calls" in message:
            result.append(message)
            continue
        blocks = []
        for block in message.get("content", []):
            if block["type"] == "tool_result":
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block["tool_use_id"],
                        "output": block.get("content", "")
                        if isinstance(block.get("content"), str)
                        else "\n".join(b.get("text", "") for b in block.get("content", [])),
                        "is_error": block.get("is_error", False),
                    }
                )
            else:
                blocks.append(block)
        result.extend(from_blocks({"role": message["role"], "blocks": blocks}))
    return result


def save_session(path, workspace, cfg, preset, messages, redact):
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {
        "version": 1,
        "workspace": str(workspace),
        "model": cfg.model,
        "preset": preset,
        "messages": Provider.anthropic_messages(messages),
    }
    temporary = path.with_suffix(".tmp_session_write")
    temporary.write_text(redact(json.dumps(value, ensure_ascii=False, indent=2)), encoding="utf-8")
    temporary.replace(path)


async def run(args):
    root, options, provenance = resolve(args)
    store = ConfigStore(args.data_dir, ROOT)
    cfg = store.current.model_copy(
        update={
            "workspace": str(root),
            "model": options.model or store.current.model,
            "max_turns": options.max_turns,
            "rag_enabled": False,
        }
    )
    words = args.words
    profile = (
        Path(options.profile).expanduser()
        if options.profile
        else Path.home() / ".claw-analog/profile.toml"
    )
    if options.profile and not profile.is_file():
        raise ValueError("profile does not exist")
    if profile.is_file():
        if profile.stat().st_size > 2048:
            raise ValueError("profile exceeds 2048 bytes")
        profile_data = tomllib.loads(profile.read_text(encoding="utf-8"))
        if not isinstance(profile_data.get("line", ""), str):
            raise ValueError("profile line must be text")
    if words[:2] == ["config", "validate"] or words[:1] == ["doctor"]:
        checks = {}
        if words[:1] == ["doctor"]:
            if not args.skip_build:
                for file in Path(__file__).parent.glob("*.py"):
                    compile(
                        file.read_text(encoding="utf-8-sig"),
                        str(file),
                        "exec",
                        optimize=2 if args.release_build else 0,
                    )
                checks["python_compile"] = "ok"
            if args.tcp_ping:
                from urllib.parse import urlsplit

                endpoint = urlsplit(cfg.base_url)
                try:
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection(
                            endpoint.hostname,
                            endpoint.port or (443 if endpoint.scheme == "https" else 80),
                        ),
                        5,
                    )
                    writer.close()
                    await writer.wait_closed()
                    checks["tcp"] = "ok"
                except (OSError, TimeoutError):
                    checks["tcp"] = "unreachable"
        print(
            json.dumps(
                {
                    "status": "ok",
                    "runtime": "python",
                    "configured": bool(cfg.api_key),
                    "options": options.model_dump(),
                    "provenance": provenance,
                    "checks": checks,
                },
                ensure_ascii=False,
            )
        )
        return 0
    if (
        options.permission == "danger-full-access"
        and not sys.stdin.isatty()
        and not options.accept_danger_non_interactive
    ):
        raise ValueError("non-interactive danger mode requires --accept-danger-non-interactive")
    db = Database(args.data_dir / "app.sqlite", recover=False)
    async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
        ext = Extensions(db, store, client)

        async def answer(question, choices):
            if not sys.stdin.isatty():
                return "拒绝"
            value = await asyncio.to_thread(input, question + " [y/N] ")
            return "允许本次" if value.strip().lower() in {"y", "yes"} else "拒绝"

        ext.question_handler = answer
        narrow = NarrowTools(ext, options, client)
        if args.print_tools:
            print(
                json.dumps(
                    {
                        "tools": narrow.definitions(options.permission, False),
                        "permission": options.permission,
                    },
                    ensure_ascii=False,
                )
            )
            await ext.close()
            return 0

        def local_path(value):
            path = Path(value).expanduser() if value else None
            return path if path is None or path.is_absolute() else root / path

        session_path, export_path = (
            local_path(args.base_session or options.session),
            local_path(args.save_session),
        )
        session = db.create_session("Lean Agent")
        db.save_messages(session["id"], read_session(session_path, root))
        profile = local_path(options.profile) or Path.home() / ".claw-analog/profile.toml"
        hint = ""
        if profile.is_file():
            if profile.stat().st_size > 2048:
                raise ValueError("profile exceeds 2048 bytes")
            hint = str(tomllib.loads(profile.read_text(encoding="utf-8")).get("line", "")).strip()[
                :512
            ]
        from .oauth import OAuth

        agent = Agent(db, Provider(client, OAuth(store, client), store), narrow, store)
        ext.jobs.agent = agent
        prompt = (
            args.prompt
            or " ".join(words[1:] if words[:1] == ["agents"] else words)
            or await asyncio.to_thread(sys.stdin.read)
        )
        if not prompt.strip():
            raise ValueError("empty prompt")
        base_messages = list(db.session(session["id"])["messages"])
        specs = args.agent or [options.preset]
        success = True
        try:
            for spec in specs:
                fields = (
                    dict(p.split("=", 1) for p in spec.split(","))
                    if "=" in spec
                    else {"preset": "implement" if spec == "fix" else spec}
                )
                preset = fields.get("preset", "none")
                if args.split_sessions:
                    session = db.create_session("Lean Agent " + fields.get("name", preset))
                    db.save_messages(session["id"], base_messages)
                agent_session_path = session_path
                if args.split_sessions and session_path:
                    import re

                    label = fields.get("name", preset)
                    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", label):
                        raise ValueError("agent name must be a simple identifier")
                    agent_session_path = Path(str(session_path) + ".agent-" + label + ".json")
                if preset not in PRESETS:
                    raise ValueError("unknown agent preset")
                permission = (
                    fields.get(
                        "permission", "workspace-write" if preset == "implement" else "read-only"
                    )
                    if args.agent
                    else options.permission
                )
                if permission not in {
                    "read-only",
                    "workspace-write",
                    "prompt",
                    "danger-full-access",
                }:
                    raise ValueError("unknown agent permission")
                if (
                    permission == "danger-full-access"
                    and not sys.stdin.isatty()
                    and not options.accept_danger_non_interactive
                ):
                    raise ValueError("non-interactive danger mode requires acknowledgement")
                options.permission = permission
                run_cfg = cfg.model_copy(
                    update={
                        "model": fields.get("model", cfg.model),
                        "system_prompt": cfg.system_prompt
                        + "\n"
                        + PRESETS[preset]
                        + "\n"
                        + hint
                        + (
                            "\nReply in Russian."
                            if options.language in {"ru", "russian"}
                            else "\nReply in English."
                        ),
                    }
                )
                text = ""
                snapshot = db.session(session["id"])["messages"] + [
                    {"role": "user", "content": fields.get("prompt", prompt)}
                ]
                pending = set()
                async for event in agent.run(
                    db.session(session["id"]),
                    fields.get("prompt", prompt),
                    "workspace-write" if permission == "prompt" else permission,
                    run_cfg,
                ):
                    if options.output_format == "json":
                        print(
                            json.dumps(
                                {
                                    "schema": "claw-analog-ndjson",
                                    "format_version": 1,
                                    "agent": fields.get("name", preset),
                                    **event,
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                    elif event["type"] == "delta":
                        text += event["text"]
                        if options.stream:
                            print(event["text"], end="", flush=True)
                    if event["type"] == "assistant":
                        snapshot.append(event["message"])
                        pending = {c["id"] for c in event["message"].get("tool_calls", [])}
                    elif event["type"] == "tool_result":
                        snapshot.append(
                            {
                                "role": "tool",
                                "tool_call_id": event["call_id"],
                                "name": event["name"],
                                "content": json.dumps(event["output"], ensure_ascii=False),
                                "is_error": event["is_error"],
                            }
                        )
                        pending.discard(event["call_id"])
                    if event["type"] in {"assistant", "tool_result", "run_end"} and not pending:
                        for target in {p for p in (agent_session_path, export_path) if p}:
                            save_session(target, root, run_cfg, preset, snapshot, store.redact)
                    if event["type"] == "run_end":
                        success &= event["status"] == "completed"
                if options.output_format != "json":
                    print("" if options.stream else text)
        finally:
            await ext.close()
        return 0 if success else 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="myclaude-analog")
    parser.add_argument("words", nargs="*")
    parser.add_argument("--workspace", "-w", default=".")
    parser.add_argument("--config")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--tcp-ping", action="store_true")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--release-build", action="store_true")
    parser.add_argument("--data-dir", type=Path, default=DATA)
    parser.add_argument("--print-tools", action="store_true")
    parser.add_argument("--no-stream", action="store_true")
    parser.add_argument("--save-session")
    parser.add_argument("--lang", choices=["en", "ru"])
    parser.add_argument("--agent", action="append")
    parser.add_argument("--prompt", "-p")
    parser.add_argument("--split-sessions", action="store_true")
    parser.add_argument("--base-session")
    for key in Options.model_fields:
        flag = "--" + key.replace("_", "-")
        if key in {"stream", "no_runtime_enforcer", "accept_danger_non_interactive"}:
            parser.add_argument(flag, action="store_true", default=None)
        elif key in {
            "max_read_bytes",
            "max_turns",
            "max_list_entries",
            "grep_max_lines",
            "glob_max_paths",
            "glob_max_depth",
            "rag_timeout_secs",
            "rag_top_k_max",
        }:
            parser.add_argument(flag, type=int)
        else:
            parser.add_argument(flag)
    try:
        args = parser.parse_intermixed_args(argv)
        if args.words[:1] == ["complete"]:
            from .completions import generate

            print(
                generate(parser, args.words[1] if len(args.words) == 2 else "", "myclaude-analog")
            )
            return 0
        return asyncio.run(run(args))
    except Exception as error:
        print(json.dumps({"status": "error", "error": type(error).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
