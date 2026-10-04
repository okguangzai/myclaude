import asyncio
import json
from pathlib import Path

import httpx
import pytest
from app.agent import Agent, trim_history
from app.config import ConfigStore
from app.db import Database
from app.main import create_app
from app.providers import Provider, ProviderError
from app.rag import RagService, namespace
from app.tools import Tools, safe_path
from fastapi.testclient import TestClient


@pytest.fixture
def setup(tmp_path):
    (tmp_path / "workspace").mkdir()
    store = ConfigStore(tmp_path / "data", tmp_path)
    store.update({"api_key": "test-secret-not-real", "workspace": str(tmp_path / "workspace")})
    db = Database(tmp_path / "data/test.sqlite")
    return store, db


def test_config_masks_and_preserves_secrets(setup):
    store, _ = setup
    assert "api_key" not in store.public()
    assert store.public()["api_key_set"]
    store.update({"api_key": "", "model": "new-model"})
    assert store.current.api_key == "test-secret-not-real"
    store.update({"clear_secrets": ["api_key"]})
    assert not store.current.api_key
    assert ConfigStore(store.data, store.root).current.model == "new-model"


@pytest.mark.parametrize(
    "path",
    [
        "../escape.txt",
        "nested/../../escape",
        ".env",
        ".local/settings.json",
        ".git/config",
        "secret.pem",
    ],
)
def test_file_boundary_and_secrets(setup, path):
    store, _ = setup
    with pytest.raises(ValueError):
        safe_path(store.current.workspace, path)


async def test_write_permission_and_nested_paths(setup):
    store, _ = setup
    tools = Tools(None)
    args = {"path": "nested/deep/file.txt", "content": "你好 Agent"}
    with pytest.raises(ValueError, match="授权"):
        await tools.execute(store.current, "read-only", "write_file", args)
    await tools.execute(store.current, "workspace-write", "write_file", args)
    result = await tools.execute(store.current, "read-only", "read_file", {"path": args["path"]})
    assert result["content"] == "你好 Agent"


def test_symlink_escape(setup, tmp_path):
    store, _ = setup
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (Path(store.current.workspace) / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Windows symlink creation privilege unavailable")
    with pytest.raises(ValueError, match="工作区"):
        safe_path(store.current.workspace, "link/secret.txt")


def test_compaction_keeps_tool_pairs():
    messages = [
        {"role": "user", "content": "old" * 5000},
        {"role": "assistant", "tool_calls": [{"id": "call1"}]},
        {"role": "tool", "tool_call_id": "call1", "content": "result"},
        {"role": "user", "content": "new"},
    ]
    result, trimmed = trim_history(messages, 1000)
    assert trimmed and result == messages[-1:]


async def test_openai_fragmented_tool_stream(setup):
    store, _ = setup
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        frames = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"pa',
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {
                                        "name": None,
                                        "arguments": 'th":"hello.txt"}',
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
            {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 5}},
        ]
        return httpx.Response(
            200,
            text="".join("data: " + json.dumps(f) + "\n\n" for f in frames) + "data: [DONE]\n\n",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = [e async for e in Provider(client).stream(store.current, [], [], "test")]
    final = events[-1]
    assert final["message"]["tool_calls"][0]["function"]["arguments"] == '{"path":"hello.txt"}'
    assert final["usage"]["input_tokens"] == 20
    assert captured[0]["model"] == "qwen-flash"
    assert captured[0]["max_tokens"] <= 32768


async def test_provider_error_does_not_leak_body(setup):
    store, _ = setup
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(401, text="test-secret-not-real"))
    ) as client:
        with pytest.raises(ProviderError) as caught:
            _ = [e async for e in Provider(client).stream(store.current, [], [], "")]
    assert "test-secret" not in str(caught.value)


class FakeProvider:
    def __init__(self, repeat=False):
        self.calls, self.repeat = [], repeat

    async def stream(self, cfg, messages, tools, system):
        self.calls.append(messages)
        if len(self.calls) == 1 or self.repeat:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path":"hello.txt"}',
                        },
                    }
                ],
            }
        else:
            assert messages[-1]["role"] == "tool"
            assert "fixture-marker" in messages[-1]["content"]
            yield {"type": "delta", "text": "读取成功"}
            message = {"role": "assistant", "content": "读取成功"}
        yield {
            "type": "complete",
            "message": message,
            "usage": {"input_tokens": 10, "output_tokens": 2},
            "stop": "stop",
        }


async def test_agent_real_tools_and_persistence(setup):
    store, db = setup
    (Path(store.current.workspace) / "hello.txt").write_text("fixture-marker")
    provider = FakeProvider()
    agent = Agent(db, provider, Tools(None), store)
    session = db.create_session()
    events = [
        e
        async for e in agent.run(
            db.session(session["id"]), "read hello", "read-only", store.current
        )
    ]
    assert events[-1]["status"] == "completed"
    assert events[-1]["turns"] == 2
    assert next(e for e in events if e["type"] == "tool_result")["is_error"] is False
    assert db.session(session["id"])["messages"][-1]["content"] == "读取成功"


async def test_turn_limit_is_not_success(setup):
    store, db = setup
    store.current.max_turns = 1
    agent = Agent(db, FakeProvider(repeat=True), Tools(None), store)
    session = db.create_session()
    events = [
        e async for e in agent.run(db.session(session["id"]), "read", "read-only", store.current)
    ]
    assert events[-1]["status"] == "limit"
    assert db.all("SELECT status FROM runs")[0]["status"] == "limit"


async def test_cancelled_run_is_persisted(setup):
    store, db = setup
    started = asyncio.Event()

    class SlowProvider:
        async def stream(self, *args):
            started.set()
            await asyncio.sleep(60)
            yield {}

    session = db.create_session()
    agent = Agent(db, SlowProvider(), Tools(None), store)

    async def consume():
        return [
            e
            async for e in agent.run(db.session(session["id"]), "wait", "read-only", store.current)
        ]

    task = asyncio.create_task(consume())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert db.all("SELECT status FROM runs")[0]["status"] == "cancelled"
    assert "运行未完成" in db.session(session["id"])["messages"][-1]["content"]


def test_http_secret_mask_origin_validation(tmp_path):
    (tmp_path / "workspace").mkdir()
    app = create_app(tmp_path / "data", tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/health").json()["ok"]
        assert client.put("/api/settings", json={"api_key": "hidden-key"}).status_code == 200
        assert "hidden-key" not in client.get("/api/settings").text
        assert (
            client.put(
                "/api/settings", json={"max_turns": 999, "api_key": "another-hidden"}
            ).status_code
            == 422
        )
        assert (
            "another-hidden"
            not in client.put(
                "/api/settings", json={"max_turns": 999, "api_key": "another-hidden"}
            ).text
        )
        assert (
            client.post(
                "/api/sessions", headers={"Origin": "https://untrusted.example"}
            ).status_code
            == 403
        )
        assert (
            client.post("/api/chat", json={"session_id": "missing", "message": "hello"}).status_code
            == 404
        )


async def test_rag_roundtrip_and_visible_fallback(setup):
    store, db = setup
    saved = []

    class FakeTunnel:
        async def ensure(self, cfg):
            pass

    def handler(request):
        path = request.url.path
        if path.endswith("/embeddings"):
            inputs = json.loads(request.content)["input"]
            return httpx.Response(
                200,
                json={"data": [{"index": i, "embedding": [1.0, 0.5]} for i in range(len(inputs))]},
            )
        if request.method == "GET":
            return httpx.Response(404)
        if path.endswith("/points/query"):
            raise httpx.ConnectError("offline")
        if path.endswith("/points"):
            saved.extend(json.loads(request.content)["points"])
        return httpx.Response(200, json={"result": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rag = RagService(db, Provider(client), client, FakeTunnel())
        result = await rag.ingest(store.current, "public.txt", "Agent has tools and context.")
        assert result["backend"] == "qdrant"
        assert saved[0]["payload"]["namespace"] == namespace(store.current)
        query = await rag.query(store.current, "tools")
        assert query["backend"] == "sqlite-fallback" and query["warning"]
        assert query["hits"][0]["name"] == "public.txt"
        cfg = store.current.model_copy(update={"embedding_model": "different-model"})
        assert (await rag.query(cfg, "tools"))["backend"] == "empty"


async def test_anthropic_stream_translation(setup):
    store, _ = setup
    cfg = store.current.model_copy(update={"provider": "anthropic", "model": "claude-test"})

    def handler(request):
        body = json.loads(request.content)
        assert body["messages"][1]["content"][0]["type"] == "tool_use"
        frames = [
            {"type": "message_start", "message": {"usage": {"input_tokens": 9}}},
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "ok"},
            },
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 1},
            },
            {"type": "message_stop"},
        ]
        return httpx.Response(200, text="".join("data: " + json.dumps(e) + "\n\n" for e in frames))

    messages = [
        {"role": "user", "content": "read"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "hello"},
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = [e async for e in Provider(client).stream(cfg, messages, [], "system")]
    assert events[-1]["message"]["content"] == "ok"


async def test_truncated_stream_is_failure(setup):
    store, _ = setup
    frame = {"choices": [{"delta": {"content": "partial"}}]}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, text="data: " + json.dumps(frame) + "\n\n")
        )
    ) as client:
        with pytest.raises(ProviderError, match="中断"):
            _ = [e async for e in Provider(client).stream(store.current, [], [], "")]


async def test_ingest_dimension_mismatch_does_not_publish(setup):
    store, db = setup

    class FakeTunnel:
        async def ensure(self, cfg):
            pass

    def handler(request):
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 0]}]})
        return httpx.Response(
            200, json={"result": {"config": {"params": {"vectors": {"size": 3}}}}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rag = RagService(db, Provider(client), client, FakeTunnel())
        with pytest.raises(ValueError, match="维度"):
            await rag.ingest(store.current, "sample.txt", "public document")
    assert not db.all("SELECT * FROM documents")


def test_chat_http_sse_and_session_resume(tmp_path):
    (tmp_path / "workspace").mkdir()
    (tmp_path / "workspace/hello.txt").write_text("fixture-marker")
    app = create_app(tmp_path / "data", tmp_path)
    with TestClient(app) as client:
        app.state.agent.provider = FakeProvider()
        session = client.post("/api/sessions").json()
        response = client.post(
            "/api/chat", json={"session_id": session["id"], "message": "read hello"}
        )
        events = [
            json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
        ]
        assert events[-1]["status"] == "completed"
        restored = client.get("/api/sessions/" + session["id"]).json()
        assert restored["messages"][-1]["content"] == "读取成功"
        assert not app.state.agent.active
