import asyncio
import json
from pathlib import Path

import httpx
import pytest
from app.config import ConfigStore
from app.config_layers import activate
from app.db import Database
from app.extensions import Extensions
from app.lanes import acknowledge_nudge, build, classify_nudge, dedupe_terminal, nudge
from app.lifecycle import Lifecycle, degraded_report
from app.model_routing import resolve, route
from app.telemetry import JsonlSink


def make_store(tmp_path):
    (tmp_path / "workspace").mkdir()
    return ConfigStore(tmp_path / "data", tmp_path)


def test_model_routing_never_cross_sends_credentials(tmp_path):
    store = make_store(tmp_path)
    cfg = store.current.model_copy(update={"model": "sonnet", "api_key": "dashscope-secret"})
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        route(cfg, {})
    result = route(
        cfg, {"ANTHROPIC_API_KEY": "anthropic-secret", "ANTHROPIC_BASE_URL": "http://test/v1"}
    )
    assert result.provider == "anthropic" and result.model == "claude-sonnet-4-6"
    assert result.api_key == "anthropic-secret" and result.base_url == "http://test/v1"
    local = route(
        cfg.model_copy(update={"model": "ollama/llama3"}), {"OLLAMA_HOST": "http://localhost:11434"}
    )
    assert local.base_url.endswith("/v1") and local.api_key == "ollama"
    with pytest.raises(ValueError, match="循环"):
        resolve("a", {"a": "b", "b": "a"})


@pytest.mark.asyncio
async def test_layer_activation_and_mcp_credentials_remain_private(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    workspace = Path(store.current.workspace)
    (workspace / ".claw").mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CLAW_CONFIG_HOME", str(home))
    (home / "settings.json").write_text(
        json.dumps({"model": "sonnet", "permissions": {"defaultMode": "plan"}})
    )
    (workspace / ".claw/settings.local.json").write_text(
        json.dumps(
            {
                "model": "haiku",
                "env": {"CUSTOM_TOKEN": "layer-private-secret"},
                "mcpServers": {
                    "files": {"command": "$python", "env": {"SECRET": "mcp-private-secret"}},
                    "bad": {"type": "websocket", "url": "ws://localhost"},
                },
            }
        )
    )
    before = store.path.read_bytes()
    report = activate(store)
    assert store.current.model == "haiku" and store.current.permission_mode == "read-only"
    assert store.path.read_bytes() == before
    assert "layer-private-secret" not in json.dumps(report)
    async with httpx.AsyncClient() as client:
        ext = Extensions(Database(store.data / "app.sqlite"), store, client)
        assert len(ext.mcp.list()) == 1
        assert "mcp-private-secret" not in json.dumps(ext.mcp.list())
        assert ext.mcp.configuration_errors[0]["server_name"] == "bad"
        health = Lifecycle(ext.jobs, "files")
        health.record("ready", degraded=True)
        assert degraded_report(ext.jobs.records("mcp_health"))["state"] == "degraded"
        assert ext.mcp.health()[0]["status"] == "stopped"
        await ext.close()


@pytest.mark.asyncio
async def test_bundled_external_registry_and_packaged_skill(tmp_path):
    store = make_store(tmp_path)
    source = tmp_path / "external/demo"
    (source / ".claude-plugin").mkdir(parents=True)
    (source / ".claude-plugin/plugin.json").write_text(
        json.dumps({"name": "demo", "version": "1", "tools": []})
    )
    skill = source / "skills/packaged/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: packaged\n---\nExplain a repository.")
    store.update({"plugin_external_directories": [str(source.parent)]})
    async with httpx.AsyncClient() as client:
        ext = Extensions(Database(store.data / "app.sqlite"), store, client)
        report = await ext.plugins.sync()
        assert not report["failures"]
        assert {p["name"] for p in ext.plugins.list()} == {
            "demo",
            "sample-hooks",
            "example-bundled",
        }
        assert not ext.assets.list("skills")
        await ext.plugins.enable("demo", True)
        assert ext.assets.read("skills", "packaged")["content"].endswith("Explain a repository.")
        (source / ".claude-plugin/plugin.json").write_text(
            json.dumps({"name": "demo", "version": "2"})
        )
        await ext.plugins.sync()
        assert ext.jobs.record("plugin", "demo")["manifest"]["version"] == "2"
        assert ext.jobs.record("plugin", "demo")["enabled"]
        assert all(p["healthy"] for p in ext.plugins.registry()["installed"])
        await ext.close()


@pytest.mark.asyncio
async def test_mcp_startup_retries_once_and_preserves_failure_phases(tmp_path, monkeypatch):
    from app import mcp_client

    store = make_store(tmp_path)
    attempts = []

    class FakeConnection:
        def __init__(self, *args):
            attempts.append(self)
            self.ready = asyncio.get_running_loop().create_future()
            self.task = asyncio.create_task(asyncio.Event().wait())
            if len(attempts) == 1:
                self.ready.set_exception(ValueError("first handshake failed"))
            else:
                args[-1].record("ready")
                self.ready.set_result(True)

        async def close(self):
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    monkeypatch.setattr(mcp_client, "Connection", FakeConnection)
    async with httpx.AsyncClient() as client:
        ext = Extensions(Database(store.data / "app.sqlite"), store, client)
        await ext.mcp.configure(
            {"name": "test", "command": "$python", "enabled": True, "startup_retries": 1}
        )
        await ext.mcp.connect("test", "danger-full-access")
        assert len(attempts) == 2 and attempts[0].task.done()
        assert ext.mcp.health()[0]["status"] == "healthy"
        events = ext.jobs.record("mcp_health", "test")["events"]
        assert any(e["phase"] == "error_surfacing" and e["error"]["recoverable"] for e in events)
        await ext.close()
        assert ext.mcp.health()[0]["status"] == "stopped"


def test_trace_write_failure_and_lane_deduplication(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    sink = JsonlSink(blocked / "trace.jsonl")
    sink.record({"test": "does not interrupt agent"})
    assert sink.last_error
    first = build("lane.finished", "completed", 1, {"answer": "same"})
    later = build("lane.finished", "completed", 2, {"answer": "same"})
    assert len(dedupe_terminal([first, later])) == 1
    record = nudge("one", "2026-10-04")
    assert classify_nudge("one", [record]) == "retry"
    assert classify_nudge("one", [acknowledge_nudge(record, "later")]) == "stale_duplicate"
    assert classify_nudge("two", [record]) == "new"
