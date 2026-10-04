"""Frozen upstream bootstrap discovery and honest ACP status fast paths."""

from pathlib import Path
from types import SimpleNamespace

from .assets import Assets
from .extensions import CATALOG

PHASES = (
    ("CliEntry", "CLI Entry", "Command-line argument parsing and global flag resolution"),
    (
        "FastPathVersion",
        "Fast-Path Version",
        "Short-circuit version/help requests before full startup",
    ),
    ("StartupProfiler", "Startup Profiler", "Instrument startup timing for diagnostics"),
    (
        "SystemPromptFastPath",
        "System Prompt Fast-Path",
        "Serve system-prompt requests without provider init",
    ),
    ("ChromeMcpFastPath", "Chrome MCP Fast-Path", "Serve Chrome MCP requests without full runtime"),
    (
        "DaemonWorkerFastPath",
        "Daemon Worker Fast-Path",
        "Handle daemon worker requests without full init",
    ),
    (
        "BridgeFastPath",
        "Bridge Fast-Path",
        "Bridge/sibling process communication without full init",
    ),
    ("DaemonFastPath", "Daemon Fast-Path", "Daemon lifecycle management without full runtime"),
    (
        "BackgroundSessionFastPath",
        "Background Session Fast-Path",
        "Resume/list background sessions without full init",
    ),
    ("TemplateFastPath", "Template Fast-Path", "Template rendering without full runtime"),
    (
        "EnvironmentRunnerFastPath",
        "Environment Runner Fast-Path",
        "Environment/runner dispatch without full init",
    ),
    ("MainRuntime", "Main Runtime", "Full interactive REPL or one-shot prompt execution"),
)


def plan():
    return {
        "kind": "bootstrap-plan",
        "action": "show",
        "status": "ok",
        "total_phases": len(PHASES),
        "phases": [
            {"id": key, "label": label, "description": description, "order": index}
            for index, (key, label, description) in enumerate(PHASES)
        ],
    }


def manifests(cwd, data):
    cwd = Path(cwd).resolve()
    if not cwd.is_dir():
        raise ValueError("missing_manifests: manifest discovery directory does not exist")
    assets = Assets(SimpleNamespace(data=Path(data), current=SimpleNamespace(workspace=str(cwd))))
    agents, skills = assets.list("agents"), assets.list("skills")
    return {
        "kind": "dump-manifests",
        "action": "dump",
        "status": "ok",
        "source": "python-resolver",
        "workspace": str(cwd),
        "commands": len(CATALOG["commands"]),
        "tools": len(CATALOG["tools"]),
        "agents": len(agents),
        "skills": len(skills),
        "bootstrap_phases": len(PHASES),
        "command_manifests": CATALOG["commands"],
        "tool_manifests": CATALOG["tools"],
        "agent_manifests": agents,
        "skill_manifests": skills,
        "bootstrap_manifest": [key for key, _, _ in PHASES],
    }


def acp_status():
    return {
        "schema_version": "1.0",
        "kind": "acp",
        "action": "status",
        "status": "not_implemented",
        "supported": False,
        "message": "原项目 ACP/Zed 本身仅提供状态；没有编辑器 daemon 或 JSON-RPC endpoint。",
        "launch_command": None,
        "protocol": {
            "name": "ACP/Zed",
            "json_rpc": False,
            "daemon": False,
            "endpoint": None,
            "serve_starts_daemon": False,
        },
        "contracts": {
            "blocking_gates": [
                "task_packet_schema",
                "session_control_schema",
                "event_report_schema",
            ],
            "stable_status_surface": "myclaude acp [serve] --output-format json",
            "unsupported_invocation_kind": "unsupported_acp_invocation",
        },
        "aliases": ["acp", "--acp", "-acp"],
    }
