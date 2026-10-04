"""Capture offline argument-error contracts from the original claw executable."""

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES = [
    ["--unknown-option"],
    ["--model"],
    ["--permission-mode"],
    ["--permission-mode", "invalid"],
    ["--reasoning-effort"],
    ["--reasoning-effort", "HIGH"],
    ["--base-commit"],
    ["--base-commit", "HEAD"],
    ["--allowedTools"],
    ["--allowed-tools="],
    ["--allowedTools", "status"],
    ["-p"],
    ["-p", ""],
    ["-p", "  "],
    ["-p", "--model"],
    ["-p", "--"],
    ["export", "--output"],
    ["export", "a.md", "b.md"],
    ["dump-manifests", "--manifests-dir"],
    ["dump-manifests", "--manifests-dir="],
    ["config", "show", "extra"],
    ["config", "env", "extra"],
    ["agents", "show", "name", "extra"],
    ["skills", "show", "name", "extra"],
    ["system-prompt", "--unknown-option"],
    ["agents", "list", "--unknown-option"],
    ["skills", "list", "--unknown-option"],
]


def project(result):
    value = json.loads(result.stdout)
    return {
        "exit_code": result.returncode,
        "status": value["status"],
        "error_kind": value["error_kind"],
        "has_hint": bool(value.get("hint")),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=ROOT.parent / "claude")
    args = parser.parse_args()
    executable = args.source / "rust/target/debug" / ("claw.exe" if os.name == "nt" else "claw")
    baseline = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=args.source, text=True
    ).strip()
    with tempfile.TemporaryDirectory(prefix="myclaude-cli-parity-") as directory:
        temp = Path(directory)
        # Use a minimal environment and isolated config; these cases must never invoke a model.
        env = {
            k: v
            for k, v in os.environ.items()
            if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT"}
        }
        env.update(CLAW_CONFIG_HOME=str(temp / "config"), CLAW_OUTPUT_FORMAT="json")
        fixtures = []
        for arguments in CASES:
            result = subprocess.run(
                [str(executable), "--output-format", "json", *arguments],
                cwd=temp,
                env=env,
                capture_output=True,
                encoding="utf-8",
                timeout=20,
            )
            if result.returncode == 0:
                raise AssertionError(f"Not an error case: {arguments}")
            fixtures.append({"arguments": arguments, "expected": project(result)})
        path = ROOT / "backend/tests/fixtures/cli_errors_upstream.json"
        path.write_text(
            json.dumps({"baseline": baseline, "cases": fixtures}, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps({"baseline": baseline, "cases": len(fixtures)}))


if __name__ == "__main__":
    main()
