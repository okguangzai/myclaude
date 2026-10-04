"""Launch migrated standalone processes on temporary ports, without cloud credentials."""

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


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    results = {}
    with tempfile.TemporaryDirectory(prefix="myclaude-services-") as temporary:
        workspace = Path(temporary) / "workspace"
        workspace.mkdir()
        (workspace / "example.py").write_text(
            "def agent_loop():\n    return 'tool results'\n", encoding="utf-8"
        )
        db = Path(temporary) / "rag.sqlite"
        mock_port, rag_port = port(), port()
        env = {
            **os.environ,
            "PYTHONPATH": str(ROOT / "backend"),
            "PYTHONUTF8": "1",
            "CLAW_RAG_MOCK_PROVIDERS": "1",
            "CLAW_RAG_HOST": "127.0.0.1",
            "CLAW_RAG_PORT": str(rag_port),
        }
        ingested = subprocess.run(
            [
                sys.executable,
                "-m",
                "app.rag_service",
                "ingest",
                "--workspace",
                str(workspace),
                "--db",
                str(db),
            ],
            env=env,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if ingested.returncode:
            raise RuntimeError("RAG ingest process failed")
        results["ingest"] = json.loads(ingested.stdout)
        children = []
        try:
            for args in (
                ["app.mock_service", "--bind", f"127.0.0.1:{mock_port}"],
                ["app.rag_service", "serve", "--db", str(db)],
            ):
                children.append(
                    subprocess.Popen(
                        [sys.executable, "-m", *args],
                        cwd=ROOT,
                        env=env,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    )
                )
            with httpx.Client(timeout=3, trust_env=False) as client:
                for value in (mock_port, rag_port):
                    for _ in range(40):
                        try:
                            client.get(f"http://127.0.0.1:{value}/health")
                            break
                        except httpx.TransportError:
                            if any(p.poll() is not None for p in children):
                                raise RuntimeError("Standalone server exited during startup")
                            time.sleep(0.1)
                    else:
                        raise RuntimeError("Standalone server startup timeout")
                mock = client.post(
                    f"http://127.0.0.1:{mock_port}/v1/messages",
                    json={
                        "model": "fixture",
                        "messages": [{"role": "user", "content": "PARITY_SCENARIO:streaming_text"}],
                    },
                ).json()
                assert mock["content"][0]["text"].startswith("Mock streaming")
                results["mock_messages"] = "ok"
                assert client.get(f"http://127.0.0.1:{rag_port}/health").text == "ok"
                results["rag_stats"] = client.get(f"http://127.0.0.1:{rag_port}/v1/stats").json()
                query = client.post(
                    f"http://127.0.0.1:{rag_port}/v1/query",
                    json={"query": "agent loop", "top_k": 2},
                ).json()
                assert query["hits"]
                results["rag_query"] = {"hits": len(query["hits"]), "phase": query["phase"]}
        finally:
            for child in children:
                child.terminate()
            for child in children:
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
    results["cloud_requests"] = 0
    report = ROOT / ".local/standalone-services-v04.json"
    report.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
