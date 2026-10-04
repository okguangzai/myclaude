"""Capture local CLI behavior from frozen Rust, using isolated repositories and no models."""

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def prepare(root, scenario):
    root.mkdir(parents=True, exist_ok=True)
    if scenario in {"partial", "existing", "ignore_rules"}:
        (root / ".claw").mkdir()
    if scenario == "existing":
        (root / "CLAUDE.md").write_text("user guidance\n", encoding="utf-8")
        (root / ".claw/settings.json").write_text('{"model":"custom-model"}', encoding="utf-8")
    if scenario == "ignore_rules":
        (root / ".gitignore").write_text(
            ".claw/settings.local.json\n.claw/sessions/\n.clawhip/\n", encoding="utf-8"
        )
    if scenario == "models":
        (root / ".claw").mkdir()
        (root / ".claw/settings.json").write_text('{"model":"sonnet"}', encoding="utf-8")
    if scenario == "definitions":
        folder = root / ".claw/agents"
        folder.mkdir(parents=True)
        for name in ["alpha", "beta"]:
            (folder / (name + ".toml")).write_text(
                f'name="{name}"\ndescription="test {name}"\nmodel="sonnet"\nmodel_reasoning_effort="medium"\n',
                encoding="utf-8",
            )
        (folder / "invalid.md").write_text("Agent without metadata", encoding="utf-8")
        for name in ["alpha", "beta"]:
            folder = root / ".claw/skills" / name
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: test\n---\nRead first.\n", encoding="utf-8"
            )
    if scenario in {"clean", "staged", "unstaged", "both"}:

        def git(*args):
            subprocess.run(
                [
                    "git",
                    "-c",
                    "core.hooksPath=",
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.test",
                    *args,
                ],
                cwd=root,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )

        git("init", "-q")
        (root / "one.txt").write_text("one\n", encoding="utf-8")
        git("add", "one.txt")
        git("commit", "-qm", "initial")
        if scenario in {"staged", "both"}:
            (root / "one.txt").write_text("staged\n", encoding="utf-8")
            git("add", "one.txt")
        if scenario in {"unstaged", "both"}:
            (root / "one.txt").write_text("unstaged\n", encoding="utf-8")


def project(value, kind):
    if kind == "init":
        return {
            k: value[k]
            for k in (
                "kind",
                "action",
                "status",
                "already_initialized",
                "created",
                "updated",
                "skipped",
                "partial",
                "deferred",
                "artifacts",
            )
        }
    if kind == "models":
        return {
            k: value[k]
            for k in (
                "kind",
                "action",
                "status",
                "configured_model",
                "resolved_configured_model",
                "local_only",
                "requires_credentials",
                "requires_provider_request",
            )
        }
    if kind == "diff":
        return {
            "kind": value["kind"],
            "action": value["action"],
            "status": value["status"],
            "result": value["result"],
            "changed_file_count": value.get("changed_file_count"),
            "has_staged": bool(value.get("staged")),
            "has_unstaged": bool(value.get("unstaged")),
        }
    return {
        "kind": value["kind"],
        "action": value["action"],
        "status": value["status"],
        "names": sorted(item["name"] for item in value.get(kind, [])),
        "error_kind": value.get("error_kind"),
        "invalid_count": value.get("invalid_count", 0),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=ROOT.parent / "claude")
    args = parser.parse_args()
    executable = args.source / "rust/target/debug" / ("claw.exe" if os.name == "nt" else "claw")
    baseline = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=args.source, text=True
    ).strip()
    cases = []
    with tempfile.TemporaryDirectory(prefix="myclaude-local-parity-") as directory:
        temp = Path(directory)
        env = {
            k: v
            for k, v in os.environ.items()
            if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT"}
        }
        env["CLAW_CONFIG_HOME"] = str(temp / "config")
        commands = [
            (scenario, ["init"], 2) for scenario in ("empty", "partial", "existing", "ignore_rules")
        ]
        commands += [("models", ["models"], 1)]
        commands += [
            (scenario, ["diff"], 1)
            for scenario in ("no_git", "clean", "staged", "unstaged", "both")
        ]
        commands += [
            ("definitions", [kind, *tail], 1)
            for kind in ("agents", "skills")
            for tail in (
                ["list"],
                ["list", "ALP"],
                ["show", "alpha"],
                ["info", "beta"],
                ["show", "missing"],
            )
        ]
        for index, (scenario, arguments, repetitions) in enumerate(commands):
            root = temp / str(index)
            prepare(root, scenario)
            expected = []
            for _ in range(repetitions):
                run = subprocess.run(
                    [str(executable), "--output-format", "json", *arguments],
                    cwd=root,
                    env=env,
                    capture_output=True,
                    encoding="utf-8",
                    timeout=20,
                )
                value = json.loads(run.stdout)
                expected.append(
                    {
                        "exit_code": run.returncode,
                        "value": project(
                            value, "models" if arguments[0] == "model" else arguments[0]
                        ),
                    }
                )
            cases.append({"scenario": scenario, "arguments": arguments, "expected": expected})
    target = ROOT / "backend/tests/fixtures/cli_local_upstream.json"
    target.write_text(
        json.dumps({"baseline": baseline, "cases": cases}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "baseline": baseline,
                "cases": len(cases),
                "invocations": sum(len(c["expected"]) for c in cases),
            }
        )
    )


if __name__ == "__main__":
    main()
