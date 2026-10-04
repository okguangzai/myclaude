import json

import httpx
import pytest
from app.agent import Agent
from app.config import ConfigStore
from app.db import Database
from app.extensions import Extensions
from app.mock_service import SCENARIOS, create_app
from app.providers import Provider
from app.tools import Tools


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
async def test_upstream_scenario_through_real_python_agent_and_tools(tmp_path, scenario):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "fixture.txt").write_text("parity fixture\nparity second line\n")
    store = ConfigStore(tmp_path / "data", tmp_path)
    cfg = store.current.model_copy(
        update={
            "provider": "anthropic",
            "base_url": "http://mock.test",
            "api_key": "mock-key",
            "model": "fixture",
            "rag_enabled": False,
            "provider_retries": 0,
        }
    )
    store.current = cfg
    permission = (
        "workspace-write"
        if scenario == "write_file_allowed"
        else "danger-full-access"
        if scenario.startswith("bash") or scenario.startswith("plugin")
        else "read-only"
    )
    db = Database(store.data / "app.sqlite")
    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app)) as client:
        ext = Extensions(db, store, client)
        tools = Tools(None, ext)
        agent = Agent(db, Provider(client, store=store), tools, store)
        ext.jobs.agent = agent
        answers = []

        async def answer(question, options):
            answers.append(question)
            return "拒绝" if scenario.endswith("denied") else "允许本次"

        ext.question_handler = answer
        if "permission_prompt" in scenario:
            cfg.permission_rules = {"ask": ["bash"]}
        if scenario == "plugin_tool_roundtrip":
            source = tmp_path / "echo"
            (source / ".claude-plugin").mkdir(parents=True)
            (source / "echo.py").write_text(
                "import json,sys\nprint(json.dumps({'input':json.load(sys.stdin)}))\n"
            )
            (source / ".claude-plugin/plugin.json").write_text(
                json.dumps(
                    {
                        "name": "echo",
                        "tools": [
                            {
                                "name": "plugin_echo",
                                "command": "$python echo.py",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"message": {"type": "string"}},
                                    "required": ["message"],
                                },
                            }
                        ],
                    }
                )
            )
            ext.plugins.install(source)
            await ext.plugins.enable("echo", True)
            cfg.allowed_tools = ["plugin_echo"]
        session = db.create_session(scenario)
        session = db.session(session["id"])
        if scenario == "auto_compact_triggered":
            messages = [
                {"role": "user", "content": "Earlier context"},
                {
                    "role": "assistant",
                    "content": "Earlier answer",
                    "usage": {"input_tokens": 50000},
                },
            ] * 4
            db.save_messages(session["id"], messages)
            session = db.session(session["id"])
        try:
            events = [
                event
                async for event in agent.run(
                    session, "PARITY_SCENARIO:" + scenario, permission, cfg
                )
            ]
            assert events[-1]["status"] == "completed", events[-1]
            results = [event for event in events if event["type"] == "tool_result"]
            if scenario.endswith("denied"):
                assert results and all(event["is_error"] for event in results)
                assert not (workspace / "generated/denied.txt").exists()
            elif results:
                assert all(not event["is_error"] for event in results), results
            if scenario == "write_file_allowed":
                assert (
                    workspace / "generated/output.txt"
                ).read_text() == "created by mock service\n"
            if "permission_prompt" in scenario:
                assert len(answers) == 1
            if scenario == "multi_tool_turn_roundtrip":
                assert len(results) == 2 and results[1]["output"]["numMatches"] == 2
            if scenario == "auto_compact_triggered":
                assert any(event["type"] == "auto_compact" for event in events)
                assert len(db.session(session["id"])["messages"]) == len(session["messages"]) + 2
            if scenario == "token_cost_reporting":
                assert events[-1]["usage"]["input_tokens"] == 1000
            if scenario == "plugin_tool_roundtrip":
                assert results[0]["output"]["input"]["message"] == "hello from plugin parity"
                cfg.denied_tools = ["plugin__echo__plugin_echo"]
                with pytest.raises(PermissionError):
                    await ext.execute(cfg, permission, "plugin_echo", {"message": "denied alias"})
        finally:
            await ext.close()
