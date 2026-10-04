import json

import httpx
import pytest
from app import analog
from app.config import ConfigStore
from app.mock_service import SCENARIOS, create_app, response_for, sse_events
from app.providers import Provider


@pytest.mark.asyncio
async def test_mock_chunked_multi_tool_stream_and_error_result_roundtrip(tmp_path):
    (tmp_path / "workspace").mkdir()
    store = ConfigStore(tmp_path / "data", tmp_path)
    cfg = store.current.model_copy(
        update={
            "provider": "anthropic",
            "base_url": "http://mock.test",
            "api_key": "mock",
            "model": "test",
            "provider_retries": 0,
        }
    )
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://mock.test"
    ) as client:
        provider = Provider(client)
        history = [{"role": "user", "content": "PARITY_SCENARIO:multi_tool_turn_roundtrip"}]
        events = [e async for e in provider.stream(cfg, history, [], "test")]
        calls = events[-1]["message"]["tool_calls"]
        assert len(calls) == 2
        assert json.loads(calls[1]["function"]["arguments"])["pattern"] == "parity"
        history += [
            events[-1]["message"],
            {
                "role": "tool",
                "tool_call_id": calls[0]["id"],
                "content": '{"file":{"content":"fixture"}}',
            },
            {"role": "tool", "tool_call_id": calls[1]["id"], "content": '{"numMatches":2}'},
        ]
        events = [e async for e in provider.stream(cfg, history, [], "test")]
        assert "fixture / 2 occurrences" in events[-1]["message"]["content"]
        encoded = Provider.anthropic_messages(
            [{"role": "tool", "tool_call_id": "test", "content": "denied", "is_error": True}]
        )
        assert encoded[0]["content"][0]["is_error"] is True
        assert all("x-api-key" not in r["headers"] for r in app.state.requests)


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_all_upstream_mock_scenarios_have_complete_protocol(scenario):
    name, response = response_for(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "PARITY_SCENARIO:" + scenario}],
                }
            ]
        }
    )
    assert name == scenario and response["content"]
    events = list(sse_events(response))
    assert events[0]["type"] == "message_start" and events[-1]["type"] == "message_stop"
    assert events[-2]["delta"]["stop_reason"] == response["stop_reason"]


def test_lean_cli_real_tool_loop_session_resume_and_toml_precedence(tmp_path, monkeypatch, capsys):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "fixture.txt").write_text("lean fixture")
    (root / ".claw-analog.toml").write_text(
        'permission="read-only"\npreset="explain"\nstream=false\nmax_turns=5\n'
    )
    data = tmp_path / "data"
    store = ConfigStore(data, tmp_path)
    store.update(
        {
            "provider": "anthropic",
            "base_url": "http://mock.test",
            "api_key": "mock",
            "model": "test",
        }
    )
    app = create_app()
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        analog.httpx,
        "AsyncClient",
        lambda **kw: original_client(**kw, transport=httpx.ASGITransport(app)),
    )
    common = ["--workspace", str(root), "--data-dir", str(data)]
    assert analog.main(["config", "validate", *common, "--max-turns", "6"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["options"]["max_turns"] == 6 and preview["provenance"]["max_turns"] == "CLI"
    session = root / "session.json"
    assert (
        analog.main(["PARITY_SCENARIO:read_file_roundtrip", *common, "--session", str(session)])
        == 0
    )
    assert "lean fixture" in capsys.readouterr().out
    saved = json.loads(session.read_text())
    assert saved["version"] == 1 and any(
        b["type"] == "tool_result" for m in saved["messages"] for b in m["content"]
    )
    assert analog.main(["PARITY_SCENARIO:streaming_text", *common, "--session", str(session)]) == 0
    capsys.readouterr()
    assert len(json.loads(session.read_text())["messages"]) > len(saved["messages"])
    assert analog.main(["--print-tools", *common]) == 0
    tools = json.loads(capsys.readouterr().out)["tools"]
    assert "write_file" not in {t["function"]["name"] for t in tools}
