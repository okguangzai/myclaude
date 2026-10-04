"""Local CLI help, workspace scaffolding and read-only inspection contracts."""

import json
from pathlib import Path

from .git_context import git
from .model_routing import ALIASES, resolve
from .tools import safe_path

HELP = {
    "status": ("", "显示当前运行时和连接配置状态"),
    "doctor": ("", "检查配置与本机运行依赖"),
    "config": (
        "[SECTION|show|inspect [PATH]|import [PATH]]",
        "显示脱敏配置；import 显式导入配置层",
    ),
    "settings": ("", "显示完整脱敏设置，与 config settings 等价"),
    "models": ("[list|status]", "查看模型及别名，不修改当前模型"),
    "session": (
        "[list|show ID|exists ID|switch ID|fork NAME|delete ID --force|import PATH|export ID]",
        "管理持久化会话；删除前保留恢复副本",
    ),
    "login": ("[status]", "使用已配置 OAuth，在交互终端完成授权回调；也可使用 Web 设置页"),
    "logout": ("", "删除当前 OAuth 令牌，保留其它配置"),
    "resume": ("[ID|latest] [PROMPT]", "读取已有会话；提供提示时继续执行"),
    "export": (
        "[PATH] [--session ID|latest] [--output PATH] [--format markdown|json|jsonl]",
        "导出会话，默认 Markdown",
    ),
    "import": ("PATH", "将 JSON/JSONL 历史导入为新会话"),
    "compact": ("[PRESERVE] [--session ID|latest]", "压缩选中会话，建议先创建分支"),
    "agents": (
        "[list [FILTER]|show NAME|create NAME|run NAME TASK]",
        "查看或创建 Agent 定义，也可执行后台 Agent",
    ),
    "skills": (
        "[list [FILTER]|show NAME|install PATH|uninstall NAME|NAME [ARGS]]",
        "管理并调用技能",
    ),
    "plugins": (
        "[list|registry|install PATH|update NAME [PATH]|enable NAME|disable NAME|uninstall NAME|sync]",
        "管理插件安装与生命周期",
    ),
    "mcp": (
        "[list|add JSON|discover NAME|tools NAME|resources NAME|prompts NAME|disconnect NAME|remove NAME]",
        "管理 MCP 服务与发现",
    ),
    "sandbox": ("", "显示当前平台隔离能力与明确的不支持项"),
    "state": ("", "查看会话、任务、MCP 与插件状态"),
    "init": ("[--cwd PATH]", "补齐工作区配置、忽略规则与 CLAUDE.md；保留已有文件"),
    "setup": ("", "显示 Web 设置入口；Python 版通过设置页配置模型"),
    "diff": ("", "查看暂存和未暂存变更，不执行外部 diff/textconv"),
    "version": ("", "显示 Python 运行时版本"),
    "bootstrap-plan": ("", "查看启动模块说明，不加载模型"),
    "dump-manifests": ("[--manifests-dir PATH]", "导出冻结的工具与命令清单"),
    "system-prompt": ("[--cwd PATH] [--date YYYY-MM-DD]", "预览系统提示与上下文来源，不调用模型"),
    "acp": ("[serve]", "显示原版 ACP 占位状态，退出码 2"),
    "prompt": ("TEXT [--stdin]", "执行提示词；-p -- TEXT 可传入以连字符开头的字面文本"),
}
HELP_ALIASES = {
    "model": "models",
    "sessions": "session",
    "plugin": "plugins",
    "marketplace": "plugins",
    "agent": "agents",
    "skill": "skills",
}


def help_topic(words, args):
    requested = args.help or bool(
        words
        and (
            words[0] == "help"
            or len(words) == 2
            and words[-1] == "help"
            and words[0] in {*HELP, *HELP_ALIASES}
            and words[0] != "prompt"
        )
    )
    if not requested:
        return None
    topic = (
        (words[1] if len(words) > 1 else "")
        if words and words[0] == "help"
        else (
            words[0]
            if words
            else "resume"
            if args.session
            else "acp"
            if args.acp
            else "dump-manifests"
            if args.dump_manifests
            else "system-prompt"
            if args.print_system_prompt
            else ""
        )
    )
    return HELP_ALIASES.get(topic, topic)


def help_value(topic):
    if topic not in HELP:
        raise ValueError(
            "unknown_help_topic: 未知帮助主题 " + topic + "\nUsage: myclaude help COMMAND"
        )
    operands, purpose = HELP[topic]
    usage = ("myclaude " + topic + " " + operands).strip()
    options = [
        {"name": "--help", "aliases": ["-h"], "description": "显示此命令帮助"},
        {
            "name": "--output-format",
            "values": ["text", "json", "stream-json"],
            "default": "text",
            "description": "输出格式",
        },
        {
            "name": "--cwd",
            "aliases": ["-C", "--directory"],
            "value": "PATH",
            "description": "选择工作区",
        },
        {"name": "--data-dir", "value": "PATH", "description": "Python 持久化数据目录"},
    ]
    if topic == "export":
        options += [
            {"name": "--session", "default": "latest", "value": "ID|latest"},
            {"name": "--output", "aliases": ["-o"], "value": "PATH"},
            {"name": "--format", "values": ["markdown", "json", "jsonl"], "default": "markdown"},
        ]
    return {
        "kind": "help",
        "action": "help",
        "status": "ok",
        "topic": topic,
        "command": topic,
        "usage": usage,
        "purpose": purpose,
        "options": options,
        "formats": ["text", "json", "stream-json"],
        "local_only": True,
        "requires_credentials": False,
        "requires_provider_request": False,
        "requires_session_resume": False,
        "mutates_workspace": False,
        "message": f"{topic.title()}\n  Usage: {usage}\n  {purpose}\n  帮助只读取内置定义，不加载配置、会话或模型。",
    }


def models_value(store):
    cfg = store.current
    return {
        "kind": "models",
        "action": "list",
        "status": "ok",
        "default_model": ALIASES["opus"],
        "configured_model": cfg.model,
        "resolved_configured_model": resolve(cfg.model, cfg.model_aliases),
        "aliases": [
            {"name": key, "model": resolve(key, cfg.model_aliases)}
            for key in dict.fromkeys([*ALIASES, *cfg.model_aliases])
        ],
        "local_only": True,
        "requires_credentials": False,
        "requires_provider_request": False,
        "message": "使用 --model MODEL 指定本次模型，或在 Web 设置中保存默认模型。",
        "result": {"model": cfg.model, "provider": cfg.provider, "base_url": cfg.base_url},
    }


def config_value(ext, section=None):
    from .config_layers import inspect

    store, cfg = ext.store, ext.store.current
    public, layers = store.public(), inspect(store)
    sections = {
        "settings": public,
        "env": {key: "[REDACTED]" for key in cfg.runtime_environment},
        "model": cfg.model,
        "hooks": cfg.hooks,
        "plugins": {key: value for key, value in public.items() if key.startswith("plugin_")},
        "mcp": public.get("imported_mcp", []),
        "sandbox": cfg.sandbox_config,
        "permissions": {"defaultMode": cfg.permission_mode, "rules": cfg.permission_rules},
        "skills": ext.assets.list("skills"),
        "agents": ext.assets.list("agents"),
    }
    section = section or "settings"
    if section not in sections and section not in public:
        raise ValueError(
            "unsupported_config_section: 未知或保密的配置字段 "
            + section
            + "\nUsage: myclaude config [settings|env|model|hooks|plugins|mcp|sandbox|permissions|skills|agents]"
        )
    value = sections.get(section, public.get(section))
    report = {
        "kind": "config",
        "action": "show",
        "status": "ok",
        "section": section,
        "cwd": cfg.workspace,
        "files": layers["files"],
        "loaded_files": sum(f["status"] == "loaded" for f in layers["files"]),
        "merged": public,
        "merged_keys": len(public),
        "merged_key_count": len(public),
        "section_value": value,
        "result": value,
        "supported_sections": list(sections),
    }
    return json.loads(store.redact(json.dumps(report, ensure_ascii=False)))


def definitions_value(ext, kind, rest):
    """Read-only list/show reports retain Python result and add upstream envelope fields."""
    action = rest[0] if rest else "list"
    action = {"info": "show", "describe": "show", "status": "list"}.get(action, action)
    if action not in {"list", "show"}:
        return None
    query = " ".join(rest[1:])
    inventory = ext.assets.inventory(kind)
    items = inventory["items"]
    if query:
        items = [
            item
            for item in items
            if (
                str(item["metadata"].get("name", item["name"])).lower() == query.lower()
                if action == "show"
                else query.lower() in str(item["metadata"].get("name", item["name"])).lower()
            )
        ]
    if action == "show" and query and not items:
        return {
            "kind": kind,
            "action": action,
            "status": "error",
            "error_kind": "agent_not_found" if kind == "agents" else "skill_not_found",
            "message": "未找到定义：" + query,
            "hint": f"Usage: myclaude {kind} list",
        }
    entries = [
        {
            **item,
            "description": item["metadata"].get("description", ""),
            "model": item["metadata"].get("model"),
            "name": item["metadata"].get("name", item["name"]),
            "invocation_name": item["name"],
        }
        for item in items
    ]
    return {
        "kind": kind,
        "action": action,
        "status": "degraded" if inventory["invalid"] else "ok",
        kind: entries,
        "count": len(entries),
        "invalid_" + kind: inventory["invalid"],
        "invalid_count": len(inventory["invalid"]),
        "result": items,
    }


async def diff_value(cwd):
    result = {"kind": "diff", "action": "diff", "working_directory": str(cwd)}
    try:
        inside = await git(cwd, "rev-parse", "--is-inside-work-tree")
    except (ValueError, OSError):
        inside = "false"
    if inside != "true":
        return {
            **result,
            "status": "error",
            "result": "no_git_repo",
            "error_kind": "no_git_repo",
            "message": "当前目录不是 Git 工作区",
            "hint": "进入已有仓库，或先运行 git init。",
        }
    base = ["-c", "core.pager=cat", "diff", "--no-ext-diff", "--no-textconv", "--no-color"]
    staged, unstaged = await git(cwd, *base, "--cached"), await git(cwd, *base)
    names = set()
    for extra in [[], ["--cached"]]:
        names.update(filter(None, (await git(cwd, *base, *extra, "--name-only", "-z")).split("\0")))
    return {
        **result,
        "status": "ok",
        "result": "changes" if staged or unstaged else "clean",
        "changed_file_count": len(names),
        "staged": staged,
        "unstaged": unstaged,
    }


def initialize(cwd):
    root = Path(cwd).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("init 需要工作区目录")
    paths = {
        name: safe_path(str(root), name)
        for name in (".claw", ".claw/settings.json", ".claw.json", ".gitignore", "CLAUDE.md")
    }
    # Validate all target types before starting; existing user files are never overwritten.
    for name, path in paths.items():
        if path.exists() and (path.is_file() if name == ".claw" else not path.is_file()):
            raise ValueError("初始化目标类型冲突：" + name)
    existed = paths[".claw"].exists()
    paths[".claw"].mkdir(exist_ok=True)
    artifacts = []

    def write_missing(name, text):
        try:
            with paths[name].open("x", encoding="utf-8", newline="\n") as file:
                file.write(text)
            return "created"
        except FileExistsError:
            return "skipped"

    settings = json.dumps({"permissions": {"defaultMode": "acceptEdits"}}, indent=2) + "\n"
    setting_status = write_missing(".claw/settings.json", settings)
    artifacts.extend(
        [
            {
                "name": ".claw/",
                "status": "partial"
                if existed and setting_status == "created"
                else "skipped"
                if existed
                else "created",
            },
            {"name": ".claw/settings.json", "status": setting_status},
            {
                "name": ".claw/sessions/",
                "status": "skipped"
                if safe_path(str(root), ".claw/sessions").is_dir()
                else "deferred",
            },
            {"name": ".claw.json", "status": write_missing(".claw.json", settings)},
        ]
    )
    ignore = paths[".gitignore"]
    previous = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
    entries = [
        "# Claw Code local artifacts",
        ".claw/settings.local.json",
        ".claw/sessions/",
        ".clawhip/",
    ]
    missing = [
        entry for entry in entries if entry not in [line.strip() for line in previous.splitlines()]
    ]
    ignore_status = "skipped"
    if missing:
        ignore_status = "updated" if ignore.exists() else "created"
        with ignore.open("a", encoding="utf-8", newline="\n") as file:
            file.write(
                ("\n" if previous and not previous.endswith("\n") else "")
                + ("\n" if previous else "")
                + "\n".join(missing)
                + "\n"
            )
    artifacts.append({"name": ".gitignore", "status": ignore_status})
    commands = []
    if (root / "pyproject.toml").exists() or (root / "requirements.txt").exists():
        commands.append("- Python: `python -m pytest`（确认项目实际测试配置）")
    if (root / "package.json").exists():
        commands.append(
            "- Node/Vue/React: 先检查 `package.json` 的 scripts，再选择安装、构建、测试命令"
        )
    if (root / "Cargo.toml").exists():
        commands.append("- Rust: `cargo test`、`cargo fmt --check`")
    guidance = (
        "# Project guidance\n\n## Workflow\n\n先阅读 README 和现有约定；最小化修改并验证。不要提交凭据。\n\n## Validation\n\n"
        + "\n".join(commands or ["- 按项目 README 运行构建和测试。"])
        + "\n"
    )
    artifacts.append({"name": "CLAUDE.md", "status": write_missing("CLAUDE.md", guidance)})
    buckets = {
        status: [a["name"] for a in artifacts if a["status"] == status]
        for status in ("created", "updated", "skipped", "partial", "deferred")
    }
    already = not any(buckets[key] for key in ("created", "updated", "partial"))
    return {
        "kind": "init",
        "action": "init",
        "status": "ok",
        "project_path": str(root),
        "already_initialized": already,
        "artifacts": artifacts,
        **buckets,
        "hint": "Workspace already initialised."
        if already
        else "检查 CLAUDE.md 并补充项目专属约定。",
        "next_step": "Review and tailor the generated guidance",
        "message": "Init\n" + "\n".join(f"  {a['name']}: {a['status']}" for a in artifacts),
    }
