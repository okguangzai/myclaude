"""Explicitly invoked live smoke test; makes small real LLM/embedding calls."""

import json
from pathlib import Path

import httpx

root = Path(__file__).resolve().parents[1]
client = httpx.Client(base_url="http://127.0.0.1:8000", timeout=180)


def call(method, path, **kwargs):
    response = client.request(method, path, **kwargs)
    response.raise_for_status()
    return response.json()


def run(prompt, permission="read-only"):
    session = call("POST", "/api/sessions")
    events = []
    with client.stream(
        "POST",
        "/api/chat",
        json={"session_id": session["id"], "message": prompt, "permission": permission},
    ) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    assert events[-1]["status"] == "completed", events[-1]
    return events


health = call("POST", "/api/connections/qdrant")
assert health["ok"]
text = "MyClaude 的 Agent 循环位于 backend/app/agent.py。模型流式提出工具调用，Python 先检查只读或工作区写入权限，再执行工具并把结果返回模型，直到完成、达到轮数上限、超时或取消。Qdrant 用于向量相似度检索，SQLite 保存会话、运行记录、文档和向量副本。向量模型是 text-embedding-v4，对话模型是 qwen-flash。知识检索工具名为 retrieve_context。"
existing = call("GET", "/api/documents")
uploaded = next((d for d in existing if d["name"] == "MyClaude架构示例.txt"), None)
if not uploaded:
    uploaded = call(
        "POST",
        "/api/documents",
        files={"file": ("MyClaude架构示例.txt", text.encode("utf-8"), "text/plain")},
    )
assert uploaded["backend"] == "qdrant" and uploaded["dimension"] == 1024
retrieved = call(
    "POST",
    "/api/retrieve",
    json={"query": "Agent 循环在哪里实现？Qdrant 的作用是什么？", "top_k": 3},
)
assert retrieved["backend"] == "qdrant" and retrieved["hits"]
events = run("请使用 read_file 读取 README.txt，用一句中文概括其中描述的 Agent 循环。")
assert any(
    e["type"] == "tool_result" and e["name"] == "read_file" and not e["is_error"] for e in events
)
rag_events = run(
    "请调用 retrieve_context 检索 MyClaude 的 Agent 循环实现位置和 Qdrant 的作用，然后引用文档名称回答。"
)
assert any(
    e["type"] == "tool_result"
    and e["name"] == "retrieve_context"
    and e["output"].get("backend") == "qdrant"
    for e in rag_events
)
write_events = run(
    "请使用 write_file 创建 learning-notes/agent-demo.txt，内容严格为 MYCLAUDE_WRITE_OK，然后用 read_file 读回来确认。",
    "workspace-write",
)
assert (root / "workspace/learning-notes/agent-demo.txt").read_text(
    encoding="utf-8"
) == "MYCLAUDE_WRITE_OK"
summary = {
    "qdrant": health,
    "document": uploaded,
    "retrieval_backend": retrieved["backend"],
    "runs": [
        {
            "kind": kind,
            "run_id": es[-1]["run_id"],
            "status": es[-1]["status"],
            "turns": es[-1]["turns"],
            "tools": [e["name"] for e in es if e["type"] == "tool_result"],
        }
        for kind, es in [("read", events), ("rag", rag_events), ("write", write_events)]
    ],
}
(root / ".local/live-verification.json").write_text(
    json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(json.dumps(summary, ensure_ascii=True, indent=2))
