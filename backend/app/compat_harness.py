"""Read-only extraction of the same archived TypeScript manifests as compat-harness."""

import argparse
import json
import os
import re
from pathlib import Path


def resolve_root(workspace):
    primary = Path(workspace).resolve().parent
    candidates = [primary]
    if os.environ.get("CLAUDE_CODE_UPSTREAM"):
        candidates.append(Path(os.environ["CLAUDE_CODE_UPSTREAM"]))
    for ancestor in [primary, *list(primary.parents)[:3]]:
        candidates.extend([ancestor / "claw-code", ancestor / "clawd-code"])
    candidates.extend([primary / "reference-source/claw-code", primary / "vendor/claw-code"])
    return next((p for p in candidates if (p / "src/commands.ts").is_file()), primary)


def identifier(line):
    match = re.search(r"[a-zA-Z0-9_-]+", line)
    return match[0] if match else None


def imports(line):
    text = line.removeprefix("import ").split(" from ")[0].strip()
    if text.startswith("{"):
        return [p.strip().split()[0] for p in text.strip("{}").split(",") if p.strip()]
    return [text.split(",")[0].strip()] if text else []


def extract(source, kind):
    entries, internal = [], False

    def add(name, origin):
        item = {"name": name, "source": origin}
        if name and item not in entries:
            entries.append(item)

    for raw in source.splitlines():
        line = raw.strip()
        if kind == "commands" and line.startswith("export const INTERNAL_ONLY_COMMANDS = ["):
            internal = True
            continue
        if internal:
            if line.startswith("]"):
                internal = False
            else:
                add(identifier(line), "InternalOnly")
            continue
        if line.startswith("import ") and (kind == "commands" or "./tools/" in line):
            for name in imports(line):
                if kind == "commands" or name.endswith("Tool"):
                    add(name, "Builtin" if kind == "commands" else "Base")
        if "feature('" in line and (
            "./commands/" in line if kind == "commands" else "Tool" in line
        ):
            name = identifier(line.split("=")[0])
            if kind == "commands" or name and name.endswith(("Tool", "Tools")):
                add(name, "FeatureGated" if kind == "commands" else "Conditional")
    return entries


def extract_bootstrap(source):
    mapping = [
        ("--version", "FastPathVersion"),
        ("startupProfiler", "StartupProfiler"),
        ("--dump-system-prompt", "SystemPromptFastPath"),
        ("--claude-in-chrome-mcp", "ChromeMcpFastPath"),
        ("--daemon-worker", "DaemonWorkerFastPath"),
        ("remote-control", "BridgeFastPath"),
        ("args[0] === 'daemon'", "DaemonFastPath"),
        ("args[0] === 'ps'", "BackgroundSessionFastPath"),
        ("args.includes('--bg')", "BackgroundSessionFastPath"),
        ("args[0] === 'new' || args[0] === 'list' || args[0] === 'reply'", "TemplateFastPath"),
        ("environment-runner", "EnvironmentRunnerFastPath"),
    ]
    return list(
        dict.fromkeys(
            ["CliEntry", *[phase for cue, phase in mapping if cue in source], "MainRuntime"]
        )
    )


def manifest(root):
    root = Path(root)
    return {
        "commands": extract((root / "src/commands.ts").read_text(encoding="utf-8"), "commands"),
        "tools": extract((root / "src/tools.ts").read_text(encoding="utf-8"), "tools"),
        "bootstrap": extract_bootstrap(
            (root / "src/entrypoints/cli.tsx").read_text(encoding="utf-8")
        ),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    print(json.dumps(manifest(args.root), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
