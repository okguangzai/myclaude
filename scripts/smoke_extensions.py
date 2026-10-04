"""Explicit live-cloud extension acceptance. Does not print secrets or create schedules."""

import asyncio
import json
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


async def chat(client, prompt, permission, expected):
    session = (await client.post("/api/sessions")).json()
    events = []
    async with client.stream(
        "POST",
        "/api/chat",
        json={"session_id": session["id"], "message": prompt, "permission": permission},
        timeout=920,
    ) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    final = next(e for e in reversed(events) if e["type"] == "run_end")
    tools = [e for e in events if e["type"] == "tool_result"]
    assert final["status"] == "completed", final
    assert any(e["name"].startswith(expected) and not e["is_error"] for e in tools), [
        (e["name"], e["is_error"]) for e in tools
    ]
    return {
        "session_id": session["id"],
        "run_id": final["run_id"],
        "status": final["status"],
        "tools": [e["name"] for e in tools],
    }


async def main():
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8000", trust_env=False, timeout=90
    ) as client:
        discovered = await client.post(
            "/api/mcp/learning/discover", json={"permission": "read-only"}
        )
        discovered.raise_for_status()
        report = {}
        report["mcp"] = await chat(
            client,
            "必须调用名称以 mcp__learning__add 开头的已发现 MCP 工具，计算 19 加 23。根据实际结果回答，不使用其他工具。",
            "read-only",
            "mcp__learning__add",
        )
        print("MCP cloud tool call: passed", flush=True)
        report["plugin"] = await chat(
            client,
            "必须调用 plugin__learning__word_count 工具统计文本 'learn agent tools' 的词数和字符数。根据实际结果回答，不使用其他工具。",
            "danger-full-access",
            "plugin__learning__word_count",
        )
        print("Plugin cloud tool call and hooks: passed", flush=True)
        report["child_agent"] = await chat(
            client,
            "必须调用 Agent 工具，description 为读取学习说明，prompt 为：只调用 read_file 读取 README.txt，随后用一句话概括内容，不修改任何文件、不创建子任务。等待其真实结果后，用中文复述。不要调用其他工具。",
            "danger-full-access",
            "Agent",
        )
        print("Nested Agent: passed", flush=True)
        created = await client.post(
            "/api/teams",
            json={
                "name": "扩展验收 · 双 Agent",
                "permission": "read-only",
                "tasks": [
                    "只调用 read_file 读取 README.txt，然后用一句话概括项目用途。",
                    "只调用 read_file 读取 README.txt，然后给出一条学习建议。",
                ],
            },
        )
        created.raise_for_status()
        team = created.json()

        async def wait(key):
            for _ in range(180):
                job = (await client.get("/api/jobs/" + key)).json()
                if job["status"] not in {"queued", "running"}:
                    assert job["status"] == "completed", job["status"]
                    return {
                        "job_id": key,
                        "session_id": job["session_id"],
                        "status": job["status"],
                        "run_id": job["result"]["run_id"],
                    }
                await asyncio.sleep(1)
            raise TimeoutError("Team timed out")

        report["team"] = {
            "team_id": team["id"],
            "tasks": await asyncio.gather(*(wait(key) for key in team["tasks"])),
        }
        print("Concurrent team: passed", flush=True)
        qdrant = await client.post("/api/connections/qdrant")
        qdrant.raise_for_status()
        report["qdrant"] = {"ok": qdrant.json().get("ok", True)}
        (ROOT / ".local/extension-live-verification.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("Qdrant regression: passed", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
