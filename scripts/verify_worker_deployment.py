"""Authenticated production Worker smoke; creates a session and calls the configured model."""

import argparse
import json
import time
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", type=Path, default=Path(".local/deploy/access.json"))
    parser.add_argument("--output", type=Path, default=Path(".local/worker-server-v06.json"))
    args = parser.parse_args()
    access = json.loads(args.credentials.read_text(encoding="utf-8"))
    with httpx.Client(
        base_url=access["url"],
        auth=(access["username"], access["password"]),
        trust_env=False,
        timeout=60,
    ) as client:

        def tool(name, **arguments):
            result = client.post(
                "api/tools/execute",
                json={"name": name, "arguments": arguments, "permission": "danger-full-access"},
            )
            result.raise_for_status()
            return result.json()

        settings = client.get("api/settings")
        settings.raise_for_status()
        item = tool("WorkerCreate", cwd=settings.json()["workspace"])
        key = item["worker_id"]
        started = None
        try:
            tool("WorkerObserve", worker_id=key, screen_text="Ready for input")
            assert tool("WorkerAwaitReady", worker_id=key)["ready"]
            started = tool(
                "WorkerSendPrompt",
                worker_id=key,
                prompt="请只调用 read_file 读取 README.txt，不修改任何文件，然后只回答 MYCLAUDE_V06_WORKER_OK。",
            )
            deadline = time.monotonic() + 180
            while True:
                response = client.get("api/jobs/" + started["job_id"])
                response.raise_for_status()
                job = response.json()
                if job["status"] not in {"queued", "running"}:
                    break
                if time.monotonic() > deadline:
                    raise TimeoutError("Worker verification exceeded 180 seconds")
                time.sleep(1)
            assert job["status"] == "completed", job["status"]
            assert "MYCLAUDE_V06_WORKER_OK" in job.get("output", "")
            history = client.get("api/sessions/" + started["session_id"])
            history.raise_for_status()
            messages = history.json()["messages"]
            calls = [
                call
                for message in messages
                for call in message.get("tool_calls", [])
                if call.get("function", {}).get("name") == "read_file"
            ]
            assert calls and any(
                message.get("role") == "tool"
                and message.get("tool_call_id") in {call["id"] for call in calls}
                for message in messages
            )
            finished = tool("WorkerGet", worker_id=key)
            assert finished["status"] == "finished"
            observed = json.loads(tool("read_file", path=".claw/worker-state.json")["content"])
            assert observed["worker_id"] == key and observed["status"] == "finished"
            restarted = tool("WorkerRestart", worker_id=key)
            assert restarted["status"] == "spawning" and restarted["job_id"] is None
            report = {
                "worker_id": key,
                "job_id": started["job_id"],
                "session_id": started["session_id"],
                "status": job["status"],
                "marker": True,
                "read_file_call": True,
                "state_file": True,
                "restart": True,
                "event_kinds": [event["kind"] for event in finished["events"]],
            }
        finally:
            tool("WorkerTerminate", worker_id=key)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report))


if __name__ == "__main__":
    main()
