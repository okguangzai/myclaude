"""The upstream tool registry backed by concrete Python services."""

from __future__ import annotations

import asyncio
import copy
import fnmatch
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import sys
import weakref
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import regex
from jsonschema import Draft202012Validator

from . import lsp
from .assets import Assets
from .db import now, uid
from .execution import LEVELS, current, process, require
from .jobs import Jobs
from .mcp_client import McpManager
from .plugins import Plugins
from .policy import tool_allowed
from .sessions import Sessions
from .tools import DENIED, safe_path

CATALOG = json.loads(Path(__file__).with_name("catalog.json").read_text(encoding="utf-8"))
SPECS = {t["name"]: copy.deepcopy(t) for t in CATALOG["tools"]}
# The frozen manifest omits fields supported by runtime/TaskPacket. Preserve that
# baseline and extend only the live schema so module/file-scoped packets work.
packet_schema = SPECS["RunTaskPacket"]["input_schema"]
packet_schema["required"] = ["objective", "scope", "repo", "branch_policy", "commit_policy"]
for field in (
    "scope_path",
    "worktree",
    "model",
    "provider",
    "permission_profile",
    "recovery_policy",
):
    packet_schema["properties"][field] = {"type": "string"}
for field in ("acceptance_criteria", "reporting_targets", "verification_plan"):
    packet_schema["properties"][field] = {"type": "array", "items": {"type": "string"}}
packet_schema["properties"]["resources"] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"kind": {"type": "string"}, "value": {"type": "string"}},
        "required": ["kind", "value"],
        "additionalProperties": False,
    },
}


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


class SearchParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.href, self.title = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.href, self.title = dict(attrs).get("href"), []

    def handle_data(self, data):
        if self.href:
            self.title.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.href:
            url = self.href
            if url.startswith("/url?"):
                query = parse_qs(urlsplit(url).query)
                url = (query.get("q") or query.get("url") or [""])[0]
            parsed = urlsplit(url)
            title = " ".join(self.title).strip()
            if (
                parsed.scheme in {"https", "http"}
                and parsed.hostname
                and title
                and not parsed.hostname.endswith(("google.com", "googleusercontent.com"))
            ):
                self.links.append({"title": title[:300], "url": url})
            self.href = None


class Extensions:
    def __init__(self, db, store, client):
        self.db, self.store, self.client = db, store, client
        self.jobs = Jobs(db, store)
        self.mcp = McpManager(self.jobs, store)
        self.plugins = Plugins(self.jobs, store)
        self.assets = Assets(store, self.plugins)
        self.sessions = Sessions(db, self.jobs, store)
        self.questions = {}
        self.question_handler = None
        self.tools = None
        self.mcp_tool_names = {}
        self.discovery_task = None
        self.worker_locks = weakref.WeakValueDictionary()

    async def start(self):
        await self.plugins.startup()
        self.jobs.scheduler = asyncio.create_task(self.jobs.tick())

        async def discover():
            for server in self.mcp.list():
                if server["enabled"]:
                    result = await self.mcp.discover(server["name"], server["permission"])
                    self.jobs.put(
                        "mcp_discovery",
                        server["name"],
                        {
                            "at": now(),
                            "tool_count": len(result.get("list_tools", {}).get("tools", [])),
                            "error": result.get("list_tools", {}).get("error"),
                        },
                    )

        self.discovery_task = asyncio.create_task(discover())

    async def close(self):
        if self.discovery_task:
            self.discovery_task.cancel()
            await asyncio.gather(self.discovery_task, return_exceptions=True)
        await self.jobs.close()
        await self.mcp.close()
        await self.plugins.close()

    def definitions(self, permission):
        ctx = current.get()
        effective = "read-only" if ctx and ctx.plan else permission
        specs = [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": spec["description"],
                    "parameters": spec["input_schema"],
                },
            }
            for name, spec in SPECS.items()
            if LEVELS[effective] >= LEVELS[spec["required_permission"]]
            or (name == "ExitPlanMode" and ctx and ctx.plan)
        ]
        for server in self.mcp.list():
            if not server["enabled"] or LEVELS[effective] < LEVELS[server["permission"]]:
                continue
            for tool in self.mcp.cache.get(server["name"], []):
                name = (
                    f"mcp__{server['name']}__"
                    + re.sub(r"[^a-zA-Z0-9_-]", "_", tool["name"])[:18]
                    + "_"
                    + hashlib.sha256(tool["name"].encode()).hexdigest()[:6]
                )
                self.mcp_tool_names[name] = (server["name"], tool["name"], tool["inputSchema"])
                specs.append(
                    {
                        "type": "function",
                        "function": {
                            "name": name,
                            "description": tool.get("description", "MCP tool"),
                            "parameters": tool["inputSchema"],
                        },
                    }
                )
        servers = [
            {"name": s["name"], "permission": s["permission"]}
            for s in self.mcp.list()
            if s["enabled"]
        ]
        for spec in specs:
            if spec["function"]["name"] in {"MCP", "ListMcpResources"}:
                spec["function"]["description"] += " Configured servers: " + json.dumps(servers)
        plugin_specs = self.plugins.definitions(effective)
        specs += plugin_specs
        reserved = {spec["function"]["name"] for spec in specs} | {
            "list_files",
            "search_files",
            "retrieve_context",
        }
        by_name = {spec["function"]["name"]: spec for spec in plugin_specs}
        for alias, canonical in self.plugins.aliases().items():
            if alias not in reserved and canonical in by_name:
                spec = copy.deepcopy(by_name[canonical])
                spec["function"]["name"] = alias
                specs.append(spec)
        return specs

    def state(self, kind, value=None):
        ctx = current.get()
        key = ctx.session_id if ctx else "local"
        if value is not None:
            return self.jobs.put(kind, key, value)
        return self.jobs.record(kind, key)

    def paths(self, cfg, start="."):
        root = Path(cfg.workspace).resolve()
        base = safe_path(cfg.workspace, start)
        visited = 0
        if base.is_file():
            yield base
            return
        for directory, dirs, files in os.walk(base, followlinks=False):
            dirs[:] = [
                d
                for d in dirs
                if d.lower() not in DENIED
                and not d.startswith(".")
                and not (Path(directory) / d).is_symlink()
            ]
            for name in files:
                visited += 1
                if visited > 5000:
                    return
                try:
                    yield safe_path(str(root), str((Path(directory) / name).relative_to(root)))
                except ValueError:
                    continue

    async def execute(self, cfg, permission, name, args):
        from .hooks import run_hooks
        from .policy import authorize_rules

        if not tool_allowed(cfg, name):
            raise PermissionError("工具被 allowed_tools/denied_tools 策略拒绝：" + name)
        if name not in SPECS and name not in {"list_files", "search_files", "retrieve_context"}:
            canonical = self.plugins.aliases().get(name)
            if canonical:
                if not tool_allowed(cfg.model_copy(update={"allowed_tools": []}), canonical):
                    raise PermissionError("插件工具的规范名称被策略拒绝")
                await authorize_rules(self, cfg, name, args)
                name = canonical
        if not isinstance(args, dict):
            raise ValueError("工具参数必须为对象")
        if name.startswith("mcp__"):
            if name not in self.mcp_tool_names:
                raise ValueError("MCP 工具尚未发现，请先连接服务")
        elif name.startswith("plugin__"):
            require(permission, "danger-full-access")
        elif name in SPECS:
            if not (
                name == "ExitPlanMode"
                and current.get()
                and current.get().plan
                and LEVELS[permission] >= 1
            ):
                require(permission, SPECS[name]["required_permission"])
            Draft202012Validator(SPECS[name]["input_schema"]).validate(args)
        elif name not in {"list_files", "search_files", "retrieve_context"}:
            raise ValueError("未知工具")
        await authorize_rules(self, cfg, name, args)
        original_args = json.dumps(args, sort_keys=True)
        args = await run_hooks(self, "PreToolUse", name, args, permission)
        args = await self.plugins.hooks("PreToolUse", name, args, permission)
        if json.dumps(args, sort_keys=True) != original_args:
            await authorize_rules(self, cfg, name, args)
        if name in SPECS:
            Draft202012Validator(SPECS[name]["input_schema"]).validate(args)
            if not (
                name == "ExitPlanMode"
                and current.get()
                and current.get().plan
                and LEVELS[permission] >= 1
            ):
                require(permission, SPECS[name]["required_permission"])
        try:
            result = await self.dispatch(cfg, permission, name, args)
        except Exception as exc:
            try:
                await self.plugins.hooks(
                    "PostToolUseFailure",
                    name,
                    args,
                    permission,
                    {"error": self.store.redact(str(exc))},
                )
                await run_hooks(
                    self,
                    "PostToolUseFailure",
                    name,
                    args,
                    permission,
                    {"error": self.store.redact(str(exc))},
                )
            except Exception:
                pass
            raise
        try:
            await self.plugins.hooks("PostToolUse", name, args, permission, result)
            await run_hooks(self, "PostToolUse", name, args, permission, result)
        except Exception as exc:
            # A failed post-hook cannot undo a successful write; preserve the result explicitly.
            result = {"result": result, "post_hook_error": self.store.redact(str(exc))}
        return json.loads(self.store.redact(json.dumps(result, ensure_ascii=False)))

    async def write(self, cfg, permission, path, text):
        require(permission, "workspace-write")
        return await self.tools.execute_builtin(
            cfg, "workspace-write", "write_file", {"path": path, "content": text}
        )

    async def ask(self, question, options, timeout):
        if self.question_handler:
            return await self.question_handler(question, options)
        key = uid()
        future = asyncio.get_running_loop().create_future()
        self.questions[key] = {
            "future": future,
            "question": question,
            "options": options,
            "session_id": current.get().session_id if current.get() else None,
        }
        try:
            return await asyncio.wait_for(future, min(timeout, 600))
        finally:
            self.questions.pop(key, None)

    async def dispatch(self, cfg, permission, name, a):
        if name.startswith("mcp__"):
            server, tool, schema = self.mcp_tool_names[name]
            Draft202012Validator(schema).validate(a)
            return await self.mcp.call(server, permission, "call_tool", name=tool, arguments=a)
        if name.startswith("plugin__"):
            return await self.plugins.execute(permission, name, a)
        if name in {"list_files", "search_files", "retrieve_context", "write_file"}:
            mode = "workspace-write" if permission == "danger-full-access" else permission
            if name == "write_file":
                require(permission, "workspace-write")
            return await self.tools.execute_builtin(cfg, mode, name, a)
        if name == "read_file":
            p = safe_path(cfg.workspace, a["path"])
            if p.stat().st_size > 256000:
                raise ValueError("文件读取上限 256 KB")
            lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
            offset, limit = a.get("offset", 0), min(a.get("limit", len(lines) or 1), 5000)
            content = "".join(lines[offset : offset + limit])
            return {
                "path": a["path"],
                "content": content[:60000],
                "total_lines": len(lines),
                "truncated": offset + limit < len(lines) or len(content) > 60000,
            }
        if name == "edit_file":
            p = safe_path(cfg.workspace, a["path"])
            if p.stat().st_size > 256000:
                raise ValueError("文件编辑上限 256 KB")
            text = p.read_text(encoding="utf-8")
            old = a["old_string"]
            count = text.count(old) if old else 0
            if not count or (count != 1 and not a.get("replace_all")):
                raise ValueError(f"原文本匹配 {count} 次；需要唯一匹配或 replace_all=true")
            result = await self.write(
                cfg,
                permission,
                a["path"],
                text.replace(old, a["new_string"], -1 if a.get("replace_all") else 1),
            )
            return {**result, "replacements": count if a.get("replace_all") else 1}
        if name in {"glob_search", "grep_search"}:
            return self.search(cfg, name, a)
        if name == "NotebookEdit":
            p = safe_path(cfg.workspace, a["notebook_path"])
            if p.stat().st_size > 256000:
                raise ValueError("Notebook 编辑上限 256 KB")
            notebook = json.loads(p.read_text(encoding="utf-8"))
            cells, cell_id = notebook["cells"], a.get("cell_id")
            index = next(
                (i for i, cell in enumerate(cells) if cell.get("id", str(i)) == cell_id), None
            )
            mode = a.get("edit_mode", "replace")
            if mode != "insert" and index is None:
                raise ValueError("cell_id 未找到")
            cell = {
                "id": uid()[:8],
                "cell_type": a.get("cell_type", "code"),
                "metadata": {},
                "source": a.get("new_source", "").splitlines(keepends=True),
            }
            if cell["cell_type"] == "code":
                cell.update(execution_count=None, outputs=[])
            if mode == "delete":
                cells.pop(index)
            elif mode == "insert":
                cells.insert(index + 1 if index is not None else len(cells), cell)
            else:
                cell["id"] = cells[index].get("id", cell["id"])
                cells[index] = cell
            return await self.write(
                cfg,
                permission,
                a["notebook_path"],
                json.dumps(notebook, ensure_ascii=False, indent=2),
            )
        if name in {"bash", "PowerShell", "REPL"}:
            return await self.shell(cfg, permission, name, a)
        if name.startswith("Git"):
            return await self.git(cfg, name, a)
        if name == "Sleep":
            if a["duration_ms"] > 60000:
                raise ValueError("单次等待上限 60000 毫秒")
            await asyncio.sleep(a["duration_ms"] / 1000)
            return {"waited_ms": a["duration_ms"]}
        if name == "StructuredOutput":
            self.state("structured", {"output": a})
            return {"accepted": True, "output": a}
        if name == "TodoWrite":
            return self.state("todos", a)
        if name == "SendUserMessage":
            for path in a.get("attachments", []):
                safe_path(cfg.workspace, path)
            return self.jobs.put(
                "notification",
                uid(),
                {
                    **a,
                    "created_at": now(),
                    "session_id": current.get().session_id if current.get() else None,
                },
            )
        if name == "AskUserQuestion":
            if self.question_handler:
                return {"answer": await self.question_handler(a["question"], a.get("options", []))}
            key = uid()
            future = asyncio.get_running_loop().create_future()
            self.questions[key] = {
                "future": future,
                "question": a["question"],
                "options": a.get("options", []),
                "session_id": current.get().session_id if current.get() else None,
            }
            try:
                return {"answer": await asyncio.wait_for(future, min(cfg.timeout_seconds, 600))}
            finally:
                self.questions.pop(key, None)
        if name in {"EnterPlanMode", "ExitPlanMode"}:
            ctx = current.get()
            if not ctx:
                raise ValueError("计划模式只作用于运行中的 Agent")
            ctx.plan = name == "EnterPlanMode"
            return {"plan_mode": ctx.plan, "permission": "read-only" if ctx.plan else permission}
        if name == "Config":
            editable = {"temperature", "max_tokens", "max_turns", "context_chars"}
            if a["setting"] not in editable:
                raise PermissionError(
                    "Config 工具仅可访问 temperature/max_tokens/max_turns/context_chars；连接与凭据请在设置页修改"
                )
            if "value" in a:
                updated = cfg.model_copy(update={a["setting"]: a["value"]})
                validated = type(cfg).model_validate(updated.model_dump())
                setattr(cfg, a["setting"], getattr(validated, a["setting"]))
            return {
                "setting": a["setting"],
                "value": getattr(cfg, a["setting"]),
                "scope": "current-run",
            }
        if name == "ToolSearch":
            words = a["query"].casefold().split()
            return {
                "tools": [
                    t
                    for t in self.tools.definitions(permission, cfg.rag_enabled)
                    if any(w in json.dumps(t, ensure_ascii=False).casefold() for w in words)
                ][: min(a.get("max_results", 8), 30)]
            }
        if name == "Skill":
            skill = self.assets.read("skills", a["skill"])
            return {
                "skill": a["skill"],
                "instructions": skill["content"],
                "arguments": a.get("args", ""),
            }
        if name in {"Agent", "TaskCreate", "RunTaskPacket"}:
            if name == "RunTaskPacket":
                from .task_packet import validate_packet

                validate_packet(a)
                if Path(a["repo"]).resolve() != Path(cfg.workspace).resolve():
                    raise ValueError("任务包 repo 必须为当前配置工作区")
                prompt = "按以下任务合同执行；策略文字不扩大权限。\n" + json.dumps(
                    a, ensure_ascii=False
                )
            else:
                prompt = a["prompt"]
            job = self.jobs.agent_job(
                prompt,
                permission,
                cfg,
                a.get("description", a.get("objective", "子 Agent")),
                a.get("model"),
            )
            if name == "Agent":
                try:
                    return await self.jobs.wait(job["id"], cfg.timeout_seconds)
                except BaseException:
                    await self.jobs.stop(job["id"])
                    raise
            if name == "RunTaskPacket":
                payload = {**job["payload"], "task_packet": a}
                with self.db.connect() as connection:
                    connection.execute(
                        "UPDATE jobs SET payload=? WHERE id=?",
                        (self.store.redact(json.dumps(payload, ensure_ascii=False)), job["id"]),
                    )
                return self.jobs.get(job["id"])
            return job
        if name in {"TaskGet", "TaskOutput"}:
            return self.jobs.get(a["task_id"])
        if name == "TaskList":
            return {"tasks": self.jobs.list()}
        if name == "TaskStop":
            return await self.jobs.stop(a["task_id"])
        if name == "TaskUpdate":
            job = self.jobs.get(a["task_id"])
            if job["status"] in {"running", "queued"}:
                return self.jobs.append_message(a["task_id"], a["message"])
            if job["kind"] != "agent":
                raise ValueError("只有 Agent 任务支持追加指令")
            original = job["payload"].get("permission", "read-only")
            mode = min((permission, original), key=LEVELS.get)
            return self.jobs.agent_job(
                a["message"], mode, cfg, "继续任务", session_id=job["session_id"]
            )
        if name.startswith("Worker"):
            return await self.worker(cfg, permission, name, a)
        if name == "TeamCreate":
            if not 1 <= len(a["tasks"]) <= 4:
                raise ValueError("团队任务数量必须为 1–4")
            created = []
            try:
                for task in a["tasks"]:
                    created.append(
                        self.jobs.agent_job(
                            task["prompt"], permission, cfg, task.get("description", a["name"])
                        )["id"]
                    )
            except BaseException:
                for task_id in created:
                    await self.jobs.stop(task_id)
                raise
            return self.jobs.put(
                "team", uid(), {"name": a["name"], "tasks": created, "created_at": now()}
            )
        if name == "TeamDelete":
            team = self.jobs.record("team", a["team_id"])
            if not team:
                raise ValueError("团队不存在")
            for key in team["tasks"]:
                await self.jobs.stop(key)
            self.jobs.remove("team", a["team_id"])
            return {"deleted": a["team_id"], "history_preserved": True}
        if name == "CronCreate":
            return self.jobs.cron(a["schedule"], a["prompt"], permission, a.get("description", ""))
        if name == "CronList":
            return {"schedules": self.jobs.records("cron")}
        if name == "CronDelete":
            self.jobs.remove("cron", a["cron_id"])
            return {"deleted": a["cron_id"]}
        if name in {"MCP", "McpAuth", "ListMcpResources", "ReadMcpResource"}:
            server = a.get("server")
            if not server:
                servers = [
                    s
                    for s in self.mcp.list()
                    if s["enabled"] and LEVELS[permission] >= LEVELS[s["permission"]]
                ]
                if name == "ListMcpResources":
                    return {
                        s["name"]: await self.mcp.call(s["name"], permission, "list_resources")
                        for s in servers
                    }
                if len(servers) != 1:
                    raise ValueError("请明确指定 server")
                server = servers[0]["name"]
            if name == "McpAuth":
                await self.mcp.connect(server, permission)
                configured = next(item for item in self.mcp.list() if item["name"] == server)
                return {
                    "connected": True,
                    "credential_configured": bool(
                        configured.get("env_refs") or configured.get("header_refs")
                    ),
                    "method": "configured-environment-credentials-or-anonymous",
                    "server": server,
                }
            if name == "MCP":
                return await self.mcp.call(
                    server,
                    permission,
                    "call_tool",
                    name=a["tool"],
                    arguments=a.get("arguments", {}),
                )
            if name == "ReadMcpResource":
                from pydantic import AnyUrl, TypeAdapter

                return await self.mcp.call(
                    server,
                    permission,
                    "read_resource",
                    uri=TypeAdapter(AnyUrl).validate_python(a["uri"]),
                )
            return await self.mcp.call(server, permission, "list_resources")
        if name == "LSP":
            path = safe_path(cfg.workspace, a.get("path", "."))
            if not path.is_file() or path.stat().st_size > 256000:
                raise ValueError("请指定 256 KB 以内的源代码文件")
            result = await lsp.query(
                cfg.workspace,
                path,
                a["action"],
                a.get("line", 0),
                a.get("character", 0),
                cfg.lsp_servers,
            )
            if a.get("query") and isinstance(result, list):
                result = [r for r in result if a["query"].casefold() in json.dumps(r).casefold()]
            return {"language": lsp.LANGUAGES.get(path.suffix), "result": result}
        if name in {"WebFetch", "WebSearch", "RemoteTrigger"}:
            return await self.web(name, a)
        if name == "TestingPermission":
            return {
                "allowed": True,
                "permission": permission,
                "action": a["action"],
                "note": "仅验证调用权限，不执行 action 字符串",
            }
        raise ValueError(f"工具没有实现：{name}")

    def search(self, cfg, name, a):
        root = Path(cfg.workspace).resolve()
        pattern = a["pattern"]
        if len(pattern) > 1000:
            raise ValueError("模式长度上限 1000")
        results = []
        match_count = 0
        limit = min(a.get("head_limit", 100), 300)
        offset = min(a.get("offset", 0), 5000)
        flags = (regex.I if a.get("-i") else 0) | (regex.S | regex.M if a.get("multiline") else 0)
        compiled = regex.compile(pattern, flags) if name == "grep_search" else None
        visited = 0
        for path in self.paths(cfg, a.get("path", ".")):
            visited += 1
            relative = path.relative_to(root).as_posix()
            if name == "glob_search":
                if fnmatch.fnmatch(relative, pattern) or path.match(pattern):
                    results.append(relative)
            else:
                if a.get("glob") and not (
                    fnmatch.fnmatch(relative, a["glob"]) or path.match(a["glob"])
                ):
                    continue
                extensions = {"python": "py", "rust": "rs", "javascript": "js", "typescript": "ts"}
                if a.get("type") and path.suffix != "." + extensions.get(a["type"], a["type"]):
                    continue
                try:
                    if path.stat().st_size > 256000:
                        continue
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    continue
                lines = text.splitlines()
                hits = list(compiled.finditer(text, timeout=0.03))[:300]
                match_count += len(hits)
                mode = a.get("output_mode", "content")
                if mode == "files_with_matches" and hits:
                    results.append(relative)
                elif mode == "count" and hits:
                    results.append({"path": relative, "count": len(hits)})
                elif mode == "content":
                    for hit in hits:
                        number = text.count("\n", 0, hit.start())
                        before = min(a.get("-B", a.get("-C", a.get("context", 0))), 10)
                        after = min(a.get("-A", a.get("-C", a.get("context", 0))), 10)
                        results.append(
                            {
                                "path": relative,
                                "line": number + 1,
                                "text": "\n".join(
                                    lines[max(0, number - before) : number + after + 1]
                                )[:2000],
                            }
                        )
                elif mode not in {"files_with_matches", "count"}:
                    raise ValueError("output_mode 必须是 content/files_with_matches/count")
            if len(results) >= limit + offset:
                break
        return {
            "matches": results[offset : offset + limit],
            "numMatches": match_count if name == "grep_search" else len(results),
            "truncated": visited >= 5000 or len(results) >= limit + offset,
        }

    async def shell(self, cfg, permission, name, a):
        if name == "REPL":
            language = a["language"].lower()
            if language in {"python", "py"}:
                argv = [sys.executable, "-u", "-c", a["code"]]
            elif language in {"javascript", "js", "node"}:
                argv = [shutil.which("node") or "node", "-e", a["code"]]
            else:
                raise ValueError("REPL 支持 Python 和 JavaScript，每次调用独立进程")
            timeout = a.get("timeout_ms", 30000) / 1000
        else:
            executable = (
                shutil.which("pwsh") or shutil.which("powershell")
                if name == "PowerShell"
                else shutil.which("bash")
            )
            if name == "bash" and os.name == "nt":
                git = shutil.which("git")
                git_bash = Path(git).parent.parent / "bin/bash.exe" if git else None
                if git_bash and git_bash.is_file():
                    executable = str(git_bash)
            if not executable:
                raise ValueError(f"未安装 {name} 执行器")
            argv = (
                [executable, "-NoProfile", "-NonInteractive", "-Command", a["command"]]
                if name == "PowerShell"
                else [executable, "--noprofile", "--norc", "-c", a["command"]]
            )
            timeout = min(a.get("timeout", 30000) / 1000, 900)

        from .sandbox import wrap

        sandbox_request = dict(a)
        if cfg.sandbox_config.get("enabled", True):
            for key, value in cfg.sandbox_config.items():
                if key == "enabled":
                    if value:
                        sandbox_request["namespaceRestrictions"] = True
                elif value:
                    sandbox_request["isolateNetwork" if key == "networkIsolation" else key] = value
        argv, sandbox_env, isolation = wrap(argv, cfg.workspace, sandbox_request)

        async def work():
            from .bash_validation import classify_command, validate_command

            validation = (
                validate_command(a["command"], permission, cfg.workspace)
                if name == "bash"
                else None
            )
            if validation and validation["decision"] == "Block":
                raise PermissionError(validation["reason"])
            result = await process(argv, cfg.workspace, timeout=timeout, env=sandbox_env)
            return {
                **result,
                "isolation": isolation,
                **(
                    {"command_intent": classify_command(a["command"]), "validation": validation}
                    if name == "bash"
                    else {}
                ),
                "status": "completed" if result["exit_code"] == 0 else "failed",
            }

        if a.get("run_in_background"):
            return self.jobs.submit("process", {"description": a.get("description", name)}, work)
        return await work()

    async def git(self, cfg, name, a):
        argv = ["git", "--no-pager", "-c", "core.fsmonitor=false", "-c", "core.pager=cat"]

        def revision(value):
            if value.startswith("-") or not re.fullmatch(r"[a-zA-Z0-9_./~^@{}:+-]{1,180}", value):
                raise ValueError("Git revision 格式不合法")
            return value

        if name == "GitStatus":
            argv += ["status", "--short" if a.get("short", True) else "--long"]
        elif name == "GitDiff":
            argv += ["diff", "--no-ext-diff", "--no-textconv"]
            if a.get("staged"):
                argv += ["--cached"]
            argv += [revision(a[k]) for k in ("commit", "commit2") if a.get(k)]
        elif name == "GitLog":
            argv += ["log", "--no-ext-diff", f"-n{min(a.get('count', 10), 100)}"]
            if a.get("oneline", True):
                argv += ["--oneline"]
            argv += [f"--{k}={a[k]}" for k in ("author", "since", "until") if a.get(k)]
        elif name == "GitShow":
            argv += ["show", "--no-ext-diff", "--no-textconv"]
            fmt = a.get("format", "stat" if a.get("stat") else "patch")
            argv += ["--stat"] if fmt == "stat" else ["--no-patch"] if fmt == "metadata" else []
            argv += [revision(a["commit"])]
        else:
            argv += ["blame", "--no-textconv"]
            if a.get("start_line"):
                argv += ["-L", f"{a['start_line']},{a.get('end_line', a['start_line'])}"]
        if a.get("path"):
            safe_path(cfg.workspace, a["path"])
            argv += ["--", a["path"]]
        return await process(argv, cfg.workspace, timeout=20)

    async def worker(self, cfg, permission, name, a):
        if name == "WorkerCreate":
            return await self._worker(cfg, permission, name, a)
        async with self.worker_lock(a["worker_id"]):
            return await self._worker(cfg, permission, name, a)

    def worker_lock(self, key):
        return self.worker_locks.setdefault(key, asyncio.Lock())

    async def _worker(self, cfg, permission, name, a):
        from . import worker as control

        if name == "WorkerCreate":
            root = Path(a["cwd"]).resolve()
            if root != Path(cfg.workspace).resolve():
                raise ValueError("Worker cwd 必须是当前工作区")
            key = uid()
            item = control.create(
                key,
                str(root),
                [*cfg.trusted_roots, *a.get("trusted_roots", [])],
                a.get("auto_recover_prompt_misdelivery", True),
            )
            from .trust import policy

            item["trust_policy"] = policy(
                str(root),
                [*cfg.trusted_roots, *cfg.trust_allowlist, *a.get("trusted_roots", [])],
                cfg.trust_denied_roots,
            )
            item["trust_auto_resolve"] = item["trust_policy"] == "auto_trust"
            item.update(
                permission=permission,
                session_id=self.db.create_session("Worker")["id"],
                job_id=None,
            )
            return self.jobs.put("worker", key, item)
        item = self.jobs.record("worker", a["worker_id"])
        if not item:
            raise ValueError("Worker 不存在")
        key = item.pop("id")
        # Upgrade previous Python records without presenting their status as upstream parity.
        if "worker_id" not in item:
            item = {**item, **control.create(key, item["cwd"])}
        job = self.jobs.get(item["job_id"]) if item.get("job_id") else None
        if job and job["status"] not in {"queued", "running"} and item["status"] == "running":
            control.completion(
                item,
                "stop" if job["status"] == "completed" else "error",
                (job.get("result") or {}).get("usage", {}).get("output_tokens", 0),
            )
        if name == "WorkerResolveTrust":
            control.resolve_trust(item)
        elif name == "WorkerObserve":
            control.observe(item, a["screen_text"])
        elif name == "WorkerSendPrompt":
            if job and job["status"] in {"queued", "running"}:
                raise ValueError("Worker 的实际任务仍在执行")
            control.send_prompt(item, a.get("prompt"), a.get("task_receipt"))
            try:
                warnings = await control.preflight(cfg.workspace, item["last_prompt"])
                for warning in warnings:
                    control.event(
                        item,
                        "startup_preflight_warning",
                        detail=warning["message"],
                        payload={"type": "startup_preflight_warning", **warning},
                    )
                task = self.jobs.agent_job(
                    item["last_prompt"],
                    min((permission, item["permission"]), key=LEVELS.get),
                    cfg,
                    "Worker",
                    session_id=item["session_id"],
                )
            except (Exception, asyncio.CancelledError):
                item["prompt_in_flight"] = False
                control.failure(item, "protocol", "worker task could not be started")
                control.event(item, "failed", "failed", "worker dispatch failed")
                self.jobs.put("worker", key, item)
                raise
            item["job_id"] = task["id"]
        elif name in {"WorkerTerminate", "WorkerRestart"}:
            if job:
                await self.jobs.stop(job["id"])
            item["job_id"] = None
            if name == "WorkerRestart":
                control.restart(item)
            else:
                control.terminate(item)
        elif name == "WorkerObserveCompletion":
            if job and job["status"] in {"queued", "running"}:
                raise ValueError("实际任务尚未结束，不能伪造完成状态")
            control.completion(item, a["finish_reason"], a["tokens_output"])
        saved = self.jobs.put("worker", key, item)
        return control.ready_snapshot(item) if name == "WorkerAwaitReady" else saved

    async def web(self, name, a):
        if name == "WebSearch":
            # Public search adapter. No invented search results when the provider blocks automation.
            query = a["query"]
            if a.get("allowed_domains"):
                query += " (" + " OR ".join("site:" + d for d in a["allowed_domains"]) + ")"
            query += "".join(" -site:" + d for d in a.get("blocked_domains", []))
            response = await self.client.get(
                "https://www.google.com/search",
                params={"q": query},
                headers={"User-Agent": "Mozilla/5.0"},
            )
            response.raise_for_status()
            parser = TextParser()
            parser.feed(response.text[:1_000_000])
            text = "\n".join(parser.parts)[:30000]
            if "unusual traffic" in text.lower() or "captcha" in text.lower():
                raise ValueError("搜索服务要求人机验证；请配置可用的搜索 MCP")
            links = SearchParser()
            links.feed(response.text[:1_000_000])

            def matches(url, domains):
                host = (urlsplit(url).hostname or "").lower()
                return any(
                    host == domain.lower().strip(".")
                    or host.endswith("." + domain.lower().strip("."))
                    for domain in domains
                )

            results, seen = [], set()
            for item in links.links:
                if (
                    item["url"] in seen
                    or (a.get("allowed_domains") and not matches(item["url"], a["allowed_domains"]))
                    or matches(item["url"], a.get("blocked_domains", []))
                ):
                    continue
                seen.add(item["url"])
                results.append(item)
            if not results:
                raise ValueError(
                    "未解析到符合域名过滤条件的搜索结果；服务可能限流或页面结构变化，请使用搜索 MCP"
                )
            return {
                "provider": "google-html",
                "query": query,
                "url": str(response.url),
                "results": results[:8],
            }
        url = a["url"]
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("只接受 HTTP(S) 地址，不允许 URL 内嵌凭据")
        addresses = await asyncio.get_running_loop().getaddrinfo(
            parsed.hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
        if any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses):
            raise ValueError("Web 工具只访问公网；内网服务请通过显式配置的 MCP 连接")
        method = a.get("method", "GET") if name == "RemoteTrigger" else "GET"
        # Pin the validated destination IP so a second DNS lookup cannot rebind to a private host.
        pinned = httpx.URL(url).copy_with(host=addresses[0][4][0])
        headers = {
            k: v
            for k, v in a.get("headers", {}).items()
            if k.lower() not in {"host", "connection", "transfer-encoding", "content-length"}
        }
        headers["Host"] = parsed.netloc
        async with self.client.stream(
            method,
            pinned,
            headers=headers,
            content=a.get("body"),
            follow_redirects=False,
            extensions={"sni_hostname": parsed.hostname},
        ) as response:
            if response.is_redirect:
                return {
                    "status": response.status_code,
                    "redirect": response.headers.get("location"),
                    "note": "请检查后显式请求重定向地址",
                }
            response.raise_for_status()
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > 1_000_000:
                    raise ValueError("HTTP 响应超过 1 MB")
            text = content.decode("utf-8", errors="replace")
            if "html" in response.headers.get("content-type", ""):
                parser = TextParser()
                parser.feed(text)
                text = "\n".join(parser.parts)
            return {
                "url": url,
                "status": response.status_code,
                "content": text[:40000],
                "truncated": len(text) > 40000,
                "prompt": a.get("prompt"),
            }
