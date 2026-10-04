"""Slash commands: local state changes, service actions and explicit Agent prompts."""

from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path

from .db import now
from .execution import process, require
from .extensions import CATALOG
from .tools import safe_path

PROMPTS = {
    "bughunter": "检查代码中的具体缺陷。先读取代码，指出可复现的触发条件、影响和位置，不要自动修改。",
    "ultraplan": "为以下任务制定可验证的实施计划，先调查代码和依赖。此轮只规划，不修改代码。",
    "plan": "先调查上下文，再制定步骤与验收标准，此轮只规划，不修改代码。",
    "review": "审查变更和相关代码，优先报告真实缺陷与回归风险，并给出文件位置。不要自动修改。",
    "security-review": "审查输入验证、认证授权、凭据处理与危险执行路径，给出证据和修复建议。",
    "summary": "总结当前会话的目标、已完成事项、未完成事项和关键文件。",
    "explain": "读取相关代码并解释设计、执行流程和学习要点。",
    "refactor": "按用户要求重构代码，保持行为，并执行适当验证。",
    "docs": "依据真实代码编写或更新文档，描述用法、架构和限制。",
    "fix": "定位并修复问题，解释原因，并执行相关验证。",
    "perf": "先定位性能瓶颈并提供证据，再提出或执行明确要求的优化。",
    "test": "检查项目测试命令，运行与任务相关的测试并解释结果。",
    "lint": "检查项目配置，运行现有代码检查并报告结果。",
    "build": "检查项目构建配置，执行构建并报告实际结果。",
    "run": "检查项目运行文档，启动用户要求的程序并验证可用性。",
    "insights": "根据本会话的运行记录总结 Agent 的有效做法、失败模式和改进建议。",
    "thinkback": "回顾本会话，列出关键判断、证据与需要更正的结论。",
}
NAV = {
    "config": "settings",
    "setup": "settings",
    "mcp": "extensions",
    "plugin": "extensions",
    "hooks": "extensions",
    "agents": "tasks",
    "tasks": "tasks",
    "team": "tasks",
    "cron": "tasks",
    "metrics": "runs",
    "usage": "runs",
    "profile": "settings",
    "project": "settings",
}
# These are upstream placeholders with no web-equivalent engine; never call them implemented.
UNSUPPORTED = {
    "vim",
    "voice",
    "upgrade",
    "share",
    "feedback",
    "desktop",
    "ide",
    "advisor",
    "stickers",
    "release-notes",
    "keybindings",
    "privacy-settings",
    "terminal-setup",
    "listen",
    "speak",
    "changelog",
    "benchmark",
    "migrate",
    "autofix",
    "macro",
    "alias",
    "rate-limit",
    "notifications",
    "screenshot",
    "image",
}


class Commands:
    def __init__(self, ext):
        self.ext, self.db, self.store = ext, ext.db, ext.store
        self.aliases = {alias: c["name"] for c in CATALOG["commands"] for alias in c["aliases"]}

    def catalog(self):
        return [
            {
                **c,
                "upstream_implemented": c["implemented"],
                "python_status": "upstream-placeholder"
                if c["name"] in UNSUPPORTED
                else "python-adapted"
                if c["implemented"]
                else "python-extension",
                "mode": "agent-prompt" if c["name"] in PROMPTS else "web-action",
            }
            for c in CATALOG["commands"]
        ]

    async def execute(self, text, session_id, permission):
        command, _, argument = text.strip().lstrip("/").partition(" ")
        command = self.aliases.get(command, command)
        argument = argument.strip()
        cfg, ext = self.store.current, self.ext
        session = self.db.session(session_id) if session_id else None
        if session_id and not session:
            raise ValueError("会话不存在")
        can_run_during_execution = command in {"approve", "deny", "stop"}
        if ext.jobs.agent and session_id in ext.jobs.agent.active and not can_run_during_execution:
            raise ValueError("会话正在运行，请先停止再执行命令")
        if (
            session_id
            and not can_run_during_execution
            and self.db.all(
                "SELECT id FROM runs WHERE session_id=? AND status='running'", (session_id,)
            )
        ):
            raise ValueError("会话正在运行，请先停止再执行命令")

        def result(value):
            return {"kind": "result", "command": command, "result": value}

        async def tool(name, args):
            return result(await ext.execute(cfg, permission, name, args))

        def need_session():
            if not session:
                raise ValueError("此命令需要已有会话")

        # Administrative operations have real parsers and local actions, independent of UI routing.
        if command in {"plugin", "skills", "agents", "mcp", "session"} and argument:
            parts = shlex.split(argument.replace("\\", "/"))
            action, rest = parts[0], parts[1:]
            if command in {"agents", "skills"}:
                action = {"info": "show", "describe": "show"}.get(action, action)
                if action == "list" and rest and rest[0].startswith("-"):
                    raise ValueError("unknown_option: 列表过滤词不能是选项")
                if action == "show" and len(rest) > 1:
                    raise ValueError("unexpected_extra_args: show 只接受一个名称")
            target = rest[0] if rest else None
            if command == "plugin":
                if action == "registry":
                    return result(ext.plugins.registry())
                if action in {"list", "status"}:
                    return result(ext.plugins.list())
                require(permission, "danger-full-access")
                if action == "sync":
                    return result(await ext.plugins.sync())
                if action == "install" and target:
                    return result(await ext.plugins.install_source(target))
                if action == "update" and target:
                    return result(
                        await ext.plugins.update(target, rest[1] if len(rest) > 1 else None)
                    )
                if action in {"enable", "disable"} and target:
                    return result(await ext.plugins.enable(target, action == "enable"))
                if action in {"uninstall", "remove"} and target:
                    return result(await ext.plugins.uninstall(target))
                raise ValueError(
                    "用法 /plugin list|install PATH|update NAME|enable NAME|disable NAME|uninstall NAME"
                )
            if command == "skills":
                if action in {"list", "status"}:
                    return result(ext.assets.list("skills", " ".join(rest)))
                if action in {"install", "uninstall", "remove"} and target:
                    require(permission, "workspace-write")
                    return result(
                        ext.assets.install_skill(target)
                        if action == "install"
                        else ext.assets.uninstall_skill(target)
                    )
                if action == "show":
                    return result(
                        ext.assets.read("skills", target) if target else ext.assets.list("skills")
                    )
            if command == "agents":
                if action == "list":
                    return result(ext.assets.list("agents", " ".join(rest)))
                if action == "create" and target:
                    require(permission, "workspace-write")
                    return result(ext.assets.create_agent(target))
                if action == "show":
                    return result(
                        ext.assets.read("agents", target) if target else ext.assets.list("agents")
                    )
                if action == "run" and target and len(rest) > 1:
                    definition = ext.assets.read("agents", target)
                    prompt = (
                        definition["metadata"].get("prompt", definition["content"])
                        + "\n任务："
                        + " ".join(rest[1:])
                    )
                    return result(
                        ext.jobs.agent_job(
                            prompt, permission, cfg, target, definition["metadata"].get("model")
                        )
                    )
                raise ValueError("用法 /agents list|create NAME|show NAME|run NAME TASK")
            if command == "mcp":
                if action in {"list", "status"}:
                    return result(ext.mcp.list())
                if action in {"discover", "tools", "resources", "prompts"} and target:
                    return result(await ext.mcp.discover(target, permission))
                if action in {"remove", "disconnect"} and target:
                    require(permission, "workspace-write")
                    await ext.mcp.disconnect(target)
                    if action == "remove":
                        ext.jobs.remove("mcp", target)
                    return result({"ok": True})
                if action == "add":
                    require(permission, "danger-full-access")
                    return result(await ext.mcp.configure(json.loads(argument.partition(" ")[2])))
            if command == "session":
                if action in {"exists", "check"} and target:
                    return result(
                        {
                            "kind": "session_exists",
                            "session_id": target,
                            "exists": ext.sessions.exists(target),
                        }
                    )
                if action == "delete" and target:
                    require(permission, "workspace-write")
                    if rest[1:] != ["--force"]:
                        return result(
                            {
                                "kind": "session_delete",
                                "status": "confirmation_required",
                                "hint": f"/session delete {target} --force",
                            }
                        )
                    return result(ext.sessions.delete(target, session_id))
                if action == "list":
                    return result(
                        self.db.all(
                            "SELECT id,title,updated_at FROM sessions ORDER BY updated_at DESC"
                        )
                    )
                if action == "import" and target:
                    require(permission, "workspace-write")
                    loaded = ext.sessions.import_text(
                        safe_path(cfg.workspace, target).read_text(encoding="utf-8-sig")
                    )
                    return {"kind": "session", "session_id": loaded["id"]}
                if action == "export":
                    return {
                        "kind": "export",
                        "filename": "session.json",
                        "text": ext.sessions.export(target or session_id or "latest"),
                    }
                if action == "fork":
                    loaded = ext.sessions.fork(session_id or "latest", target)
                    return {"kind": "session", "session_id": loaded["id"]}
                if action in {"show", "resume", "switch"}:
                    loaded = ext.sessions.resolve(
                        target or "latest",
                        exclude_id=session_id if action in {"resume", "switch"} else None,
                    )
                    return (
                        result(loaded)
                        if action == "show"
                        else {"kind": "session", "session_id": loaded["id"]}
                    )
        if command == "compact":
            parts = argument.split()
            return result(
                ext.sessions.compact(
                    session_id or "latest",
                    int(parts[-1]) if parts and parts[-1].isdigit() else 4,
                    "trident" if "--trident" in parts else "standard",
                )
            )
        if command == "allowed-tools" and argument:
            self.store.update(
                {
                    "allowed_tools": [x.strip() for x in argument.split(",") if x.strip()]
                    if argument != "reset"
                    else []
                }
            )
            return result({"allowed_tools": self.store.current.allowed_tools})

        if command in UNSUPPORTED:
            return result(
                {
                    "status": "upstream-placeholder",
                    "message": "原项目此命令没有可执行实现；当前版本保留目录入口，尚未实现。详见迁移矩阵。",
                }
            )
        if command in PROMPTS:
            return {
                "kind": "prompt",
                "prompt": PROMPTS[command] + "\n用户补充：" + argument,
                "permission": "read-only" if command in {"plan", "ultraplan"} else permission,
            }
        if command in NAV and not argument:
            return {"kind": "navigate", "page": NAV[command]}
        if command == "help":
            return result(self.catalog())
        if command == "sandbox":
            from .sandbox import status

            return result(await asyncio.to_thread(status))
        if command in {"status", "doctor", "sandbox", "version", "providers"}:
            return result(
                {
                    "version": "0.7.0",
                    "language": "Python",
                    "provider": cfg.provider,
                    "model": cfg.model,
                    "configured": bool(cfg.api_key),
                    "workspace": cfg.workspace,
                    "permission": permission,
                    "process_isolation": "none; execution permission grants local process access",
                    "providers": ["openai-compatible", "anthropic"],
                    "tools": len(CATALOG["tools"]),
                    "mcp": len(ext.mcp.list()),
                    "plugins": len(ext.plugins.list()),
                    "background_jobs": len(ext.jobs.running),
                }
            )
        if command in {"cost", "stats", "tokens", "budget"}:
            totals = self.db.all(
                "SELECT COUNT(*) AS runs,COALESCE(SUM(input_tokens),0) AS input_tokens,COALESCE(SUM(output_tokens),0) AS output_tokens FROM runs"
                + (" WHERE session_id=?" if session_id else ""),
                (session_id,) if session_id else (),
            )[0]
            return result(
                {
                    **totals,
                    "max_turns": cfg.max_turns,
                    "max_tokens": cfg.max_tokens,
                    "tree_requests": 40,
                    "cost": None,
                    "scope": "session" if session_id else "application",
                    "messages": len(session["messages"]) if session else 0,
                    "note": "实际 Token 用量；未配置价格表，不估造费用",
                }
            )
        if command in {
            "model",
            "temperature",
            "max-tokens",
            "system-prompt",
            "language",
            "effort",
            "fast",
            "reasoning",
        }:
            field = {
                "model": "model",
                "temperature": "temperature",
                "max-tokens": "max_tokens",
                "system-prompt": "system_prompt",
                "language": "system_prompt",
                "effort": "reasoning_effort",
                "reasoning": "reasoning_effort",
                "fast": "max_turns",
            }[command]
            if argument:
                value = (
                    float(argument)
                    if command == "temperature"
                    else int(argument)
                    if field in {"max_turns", "max_tokens"}
                    else argument
                )
                if command == "language":
                    value = cfg.system_prompt + f"\n{command}: {argument}"
                if field == "reasoning_effort" and value in {"reset", "off"}:
                    value = None
                self.store.update({field: value})
            return result(
                {
                    field: getattr(self.store.current, field),
                    "note": "reasoning_effort 用于支持该参数的 OpenAI 兼容服务；fast 调整 Agent 轮数",
                }
            )
        if command in {"brief", "format", "output-style"}:
            preference = "简洁作答，只保留必要内容。" if command == "brief" else argument
            if not preference:
                raise ValueError("请在命令后填写期望的输出格式")
            self.store.update({"system_prompt": cfg.system_prompt + "\n输出要求：" + preference})
            return result({"output_preference": preference})
        if command == "permissions":
            if argument and argument not in {"read-only", "workspace-write", "danger-full-access"}:
                raise ValueError("权限只能是 read-only/workspace-write/danger-full-access")
            return {"kind": "permission", "permission": argument or permission}
        if command == "allowed-tools":
            return result(ext.tools.definitions(permission, cfg.rag_enabled))
        if command == "api-key":
            return result(
                {
                    "api_key_set": bool(cfg.api_key),
                    "message": "密钥只可在设置页重新填写，接口不会返回已存密钥",
                }
            )
        if command == "clear":
            loaded = self.db.create_session("新对话")
            ext.jobs.put("session_metadata", loaded["id"], {"workspace_root": cfg.workspace})
            return {
                "kind": "session",
                "session_id": loaded["id"],
                "previous_session_preserved": True,
            }
        if command == "reset":
            need_session()
            self.db.save_messages(session_id, [])
            return {"kind": "session", "session_id": session_id}
        if command in {"rewind", "undo"}:
            need_session()
            messages = session["messages"]
            boundary = next(
                (i for i in range(len(messages) - 1, -1, -1) if messages[i]["role"] == "user"),
                0,
            )
            messages = messages[:boundary]
            self.db.save_messages(session_id, messages)
            return {
                "kind": "session",
                "session_id": session_id,
                "note": "仅修改会话上下文；已执行的文件与进程操作不会撤销",
            }
        if command == "teleport":
            if not argument:
                raise ValueError("用法 /teleport 文件名或搜索表达式")
            return result(
                {
                    "target": argument,
                    "files": [
                        str(p.relative_to(Path(cfg.workspace)))
                        for p in ext.paths(cfg)
                        if argument.casefold() in str(p.relative_to(Path(cfg.workspace))).casefold()
                    ][:10],
                    "content": await ext.execute(
                        cfg, permission, "grep_search", {"pattern": argument, "head_limit": 20}
                    ),
                }
            )
        if command == "history":
            need_session()
            return result(
                {
                    "session_id": session_id,
                    "messages": session["messages"],
                    "count": len(session["messages"]),
                }
            )
        if command in {"session", "resume"}:
            if argument:
                loaded = ext.sessions.resolve(
                    argument, exclude_id=session_id if command == "resume" else None
                )
                return {"kind": "session", "session_id": loaded["id"]}
            return result(
                self.db.all("SELECT id,title,updated_at FROM sessions ORDER BY updated_at DESC")
            )
        if command in {"export", "copy"}:
            need_session()
            return {
                "kind": command,
                "filename": f"session-{session_id}.json",
                "text": ext.sessions.export(session_id),
            }
        if command in {"branch", "chat"}:
            new = (
                ext.sessions.fork(session_id, argument or None)
                if command == "branch" and session
                else self.db.create_session(argument or "新对话")
            )
            return {"kind": "session", "session_id": new["id"]}
        if command == "rename":
            need_session()
            if not argument:
                raise ValueError("用法 /rename 新标题")
            self.db.save_messages(session_id, session["messages"], argument)
            return result({"renamed": argument[:50]})
        if command in {"pin", "unpin", "tag", "bookmarks"}:
            need_session()
            if command == "bookmarks":
                return result(ext.jobs.records("bookmark"))
            if command == "unpin":
                ext.jobs.remove("bookmark", session_id)
            else:
                ext.jobs.put(
                    "bookmark",
                    session_id,
                    {"title": session["title"], "tag": argument, "created_at": now()},
                )
            return result(ext.jobs.records("bookmark"))
        if command == "init":
            from .cli_local import initialize

            require(permission, "workspace-write")
            if argument:
                raise ValueError("unexpected_extra_args: /init 不接受额外参数")
            return result(initialize(cfg.workspace))
        if command == "memory":
            from .prompt import discover_instructions

            if argument:
                raise ValueError("unexpected_extra_args: /memory 只查看加载的指令文件")
            return result({"files": discover_instructions(cfg.workspace, cfg.rules_import)})
        if command in {"files", "map"}:
            return await tool("glob_search", {"pattern": argument or "**/*"})
        if command == "search":
            return await tool("grep_search", {"pattern": argument})
        if command in {"diff", "log", "blame", "git", "stash", "commit", "pr", "issue"}:
            if command == "diff":
                from .cli_local import diff_value

                return (
                    await tool("GitDiff", {"path": argument})
                    if argument
                    else result(await diff_value(cfg.workspace))
                )
            if command == "log":
                return await tool("GitLog", {"count": int(argument or 10)})
            if command == "blame":
                return await tool("GitBlame", {"path": argument})
            require(permission, "danger-full-access")
            args = shlex.split(argument)
            if command == "commit":
                if not argument:
                    return {
                        "kind": "prompt",
                        "prompt": "检查 git diff --cached，给出提交消息建议，不要自动暂存或提交。",
                    }
                argv = ["git", "commit", "-m", argument]
            elif command in {"pr", "issue"}:
                argv = ["gh", command] + (args or ["list"])
            else:
                argv = (
                    ["git"]
                    + (["stash"] if command == "stash" else [])
                    + (args or ["status", "--short"])
                )
            return result(await process(argv, cfg.workspace, timeout=60))
        if command == "debug-tool-call":
            if not argument:
                need_session()
                calls = [
                    call
                    for message in session["messages"]
                    for call in message.get("tool_calls", [])
                ]
                if not calls:
                    raise ValueError("no prior tool call found in session")
                call = calls[-1]
                output = next(
                    (
                        message
                        for message in reversed(session["messages"])
                        if message.get("tool_call_id") == call["id"]
                    ),
                    None,
                )
                return result({"call": call, "output": output, "replayed": False})
            name, _, raw = argument.partition(" ")
            return await tool(name, json.loads(raw or "{}"))
        if command in {"mcp", "plugin", "agents", "skills", "hooks", "tasks", "team", "cron"}:
            if command == "skills":
                if argument:
                    return await tool("Skill", {"skill": argument})
                return result(
                    [
                        str(p.relative_to(Path(cfg.workspace)))
                        for p in ext.paths(cfg)
                        if p.name == "SKILL.md"
                    ]
                )
            if command == "mcp":
                return result(await ext.mcp.discover(argument, permission))
            return result(
                {
                    "mcp": ext.mcp.list(),
                    "plugins": ext.plugins.list(),
                    "tasks": ext.jobs.list(),
                    "teams": ext.jobs.records("team"),
                    "schedules": ext.jobs.records("cron"),
                }
            )
        if command in {"agent", "subagent", "parallel", "multi"}:
            if command in {"parallel", "multi"}:
                return await tool(
                    "TeamCreate",
                    {
                        "name": "并行任务",
                        "tasks": [{"prompt": p.strip()} for p in argument.split("||") if p.strip()],
                    },
                )
            return await tool(
                "TaskCreate", {"prompt": argument, "description": "命令创建的子 Agent"}
            )
        if command in {"symbols", "references", "definition", "hover", "diagnostics"}:
            parts = shlex.split(argument)
            return await tool(
                "LSP",
                {
                    "action": command,
                    "path": parts[0],
                    "line": int(parts[1]) if len(parts) > 1 else 0,
                    "character": int(parts[2]) if len(parts) > 2 else 0,
                },
            )
        if command == "web":
            return await tool("WebFetch", {"url": argument, "prompt": "读取网页"})
        if command in {"approve", "deny"}:
            candidates = [
                (key, q)
                for key, q in ext.questions.items()
                if q.get("session_id") == session_id and not q["future"].done()
            ]
            key = argument or (candidates[0][0] if len(candidates) == 1 else "")
            question = ext.questions.get(key)
            if not question:
                raise ValueError(
                    "用法 /approve 或 /deny 问题ID；此命令仅回答待处理问题，不扩大权限"
                )
            if question["future"].done():
                raise ValueError("问题已经回答或过期")
            approved = next(
                (
                    option
                    for option in question.get("options", [])
                    if option in {"允许本次", "允许", "同意", "Approve", "Allow"}
                ),
                "同意",
            )
            question["future"].set_result(approved if command == "approve" else "拒绝")
            return result({"answered": key})
        if command in {"config", "env", "workspace", "add-dir", "project"}:
            if command == "config":
                from .cli_local import config_value

                return result(config_value(ext, argument or None))
            if command in {"workspace", "add-dir", "project"} and argument:
                self.store.update({"workspace": argument})
            return result(self.store.public())
        if command in {"context", "tool-details"}:
            return result(
                {
                    "messages": session["messages"] if session else [],
                    "context_limit": cfg.context_chars,
                    "tools": ext.tools.definitions(permission, cfg.rag_enabled),
                }
            )
        if command == "cache":
            from .prompt_cache import PromptCache

            if argument == "clear":
                ext.mcp.cache.clear()
            return result(
                {
                    "mcp_discovery_entries": len(ext.mcp.cache),
                    "provider_prompt_cache": "服务端缓存；本机跟踪命中计数与失效原因",
                    "prompt_cache": PromptCache(self.store.data, session_id).stats()
                    if session_id
                    else None,
                }
            )
        if command == "telemetry":
            if argument in {"on", "off"}:
                self.store.update({"telemetry_enabled": argument == "on"})
            return result(
                {
                    "external_telemetry": False,
                    "jsonl_enabled": self.store.current.telemetry_enabled,
                    "storage": "local SQLite events; optional .local/traces/*.jsonl",
                }
            )
        if command in {
            "theme",
            "color",
            "brief",
            "format",
            "output-style",
            "focus",
            "unfocus",
            "templates",
            "paste",
            "screenshot",
            "image",
            "stop",
            "retry",
            "exit",
        }:
            return {"kind": "ui", "action": command, "argument": argument}
        # Plugin commands are discovered from installed manifests and are executable.
        for plugin in ext.plugins.list():
            for spec in plugin["manifest"].get("commands", []):
                if command == plugin["id"] + ":" + spec["name"]:
                    require(permission, "danger-full-access")
                    return result(
                        await ext.plugins.invoke(
                            plugin["id"], spec["command"], {"arguments": argument}
                        )
                    )
        import difflib

        names = [item["name"] for item in CATALOG["commands"]]
        nearby = difflib.get_close_matches(command, names, n=3, cutoff=0.4)
        hint = "；Did you mean " + ", ".join("/" + name for name in nearby) if nearby else ""
        raise ValueError("未知命令 /" + command + hint + "；输入 /help 查看目录")
