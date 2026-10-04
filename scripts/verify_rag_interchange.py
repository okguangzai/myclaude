"""Verify both directions of SQLite exchange with the frozen Rust RAG binary."""

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rust", required=True, type=Path)
    parser.add_argument("--report", type=Path, default=ROOT / ".local/rag-interchange.json")
    args = parser.parse_args()
    implementations = {
        "python": [sys.executable, "-m", "app.rag_service"],
        "rust": [str(args.rust.resolve(strict=True))],
    }
    results = {}
    with tempfile.TemporaryDirectory(prefix="rag-interchange-") as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir()
        (workspace / "example.py").write_text("def agent_loop():\n    return 'tools'\n")
        env = {
            **os.environ,
            "PYTHONPATH": str(ROOT / "backend"),
            "PYTHONUTF8": "1",
            "CLAW_RAG_MOCK_PROVIDERS": "1",
            "CLAW_RAG_HOST": "127.0.0.1",
        }
        for writer, reader in [("python", "rust"), ("rust", "python")]:
            db = root / f"{writer}.sqlite"
            subprocess.run(
                [
                    *implementations[writer],
                    "ingest",
                    "--workspace",
                    str(workspace),
                    "--db",
                    str(db),
                ],
                env=env,
                cwd=root,
                check=True,
                capture_output=True,
                timeout=30,
            )
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            env["CLAW_RAG_PORT"] = str(port)
            child = subprocess.Popen(
                [*implementations[reader], "serve", "--db", str(db)],
                env=env,
                cwd=root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            try:
                with httpx.Client(
                    base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=3
                ) as client:
                    for _ in range(60):
                        try:
                            client.get("/health").raise_for_status()
                            break
                        except httpx.TransportError:
                            if child.poll() is not None:
                                raise RuntimeError(f"{reader} exited before ready") from None
                            time.sleep(0.1)
                    else:
                        raise TimeoutError(reader)
                    stats = client.get("/v1/stats").json()
                    response = client.post("/v1/query", json={"query": "agent loop", "top_k": 1})
                    response.raise_for_status()
                    query = response.json()
                    assert stats["chunks"] == 1
                    assert len(query["hits"]) == 1
                    assert query["hits"][0]["path"].endswith("example.py")
                    assert query["hits"][0]["score"] > 0
                    results[f"{writer}_to_{reader}"] = {
                        "chunks": 1,
                        "hits": 1,
                        "phase": query["phase"],
                    }
            finally:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
    results["cloud_requests"] = 0
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
