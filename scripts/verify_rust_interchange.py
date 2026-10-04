"""Verify Python-exported histories can be resumed by the frozen Rust executable.

Uses temporary workspaces and a scrubbed environment. Never makes model requests.
"""

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import ConfigStore  # noqa: E402
from app.db import Database  # noqa: E402
from app.execution import clean_env  # noqa: E402
from app.extensions import CATALOG, Extensions  # noqa: E402


async def verify(executable):
    results = []
    with tempfile.TemporaryDirectory(prefix="myclaude-rust-parity-") as directory:
        root = Path(directory)
        workspace, home = root / "workspace", root / "home"
        workspace.mkdir()
        home.mkdir()
        store = ConfigStore(root / "data", root)
        db = Database(store.data / "app.sqlite")
        async with httpx.AsyncClient() as client:
            ext = Extensions(db, store, client)
            try:
                session = db.create_session("cross-runtime-verification")
                messages = [
                    {"role": "user", "content": "Read the fixture; do not modify files."},
                    {
                        "role": "assistant",
                        "content": None,
                        "thinking": [
                            {
                                "type": "thinking",
                                "thinking": "test provider block",
                                "signature": "test-signature",
                            }
                        ],
                        "tool_calls": [
                            {
                                "id": "fixture-call",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path":"fixture.txt"}',
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "fixture-call",
                        "name": "read_file",
                        "content": "fixture content",
                        "is_error": False,
                    },
                    {
                        "role": "assistant",
                        "content": "Fixture inspected.",
                        "usage": {
                            "input_tokens": 20,
                            "output_tokens": 4,
                            "cache_read_input_tokens": 3,
                            "cache_creation_input_tokens": 0,
                        },
                    },
                    {"role": "user", "content": "Next preserve the context."},
                    {"role": "assistant", "content": "Context preserved."},
                ]
                db.save_messages(session["id"], messages)
                env = {
                    **clean_env(),
                    "HOME": str(home),
                    "USERPROFILE": str(home),
                    "CLAW_CONFIG_HOME": str(home / ".claw"),
                }
                for compact in (False, True):
                    if compact:
                        ext.sessions.compact(session["id"], 2)
                    for format in ("json", "jsonl"):
                        target = workspace / f"fixture-{'compact' if compact else 'full'}.{format}"
                        target.write_text(
                            ext.sessions.export(session["id"], format), encoding="utf-8"
                        )
                        completed = subprocess.run(
                            [
                                str(executable),
                                "--resume",
                                str(target),
                                "/status",
                                "--output-format",
                                "json",
                            ],
                            cwd=workspace,
                            env=env,
                            capture_output=True,
                            text=True,
                            encoding="utf-8",
                            timeout=30,
                        )
                        if completed.returncode:
                            raise AssertionError(
                                f"Rust rejected {target.name}: {completed.stderr[:1000]}"
                            )
                        json.loads(completed.stdout)
                        results.append(
                            {
                                "format": format,
                                "compacted": compact,
                                "exit_code": completed.returncode,
                                "valid_json_response": True,
                            }
                        )
            finally:
                await ext.close()
    return {
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "source_revision": CATALOG["revision"],
        "rust_executable": str(executable),
        "model_requests": 0,
        "checks": results,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rust", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = asyncio.run(verify(args.rust.resolve(strict=True)))
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
