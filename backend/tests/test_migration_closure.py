import json
from pathlib import Path

import httpx
import pytest
from app import cli
from app.bash_validation import classify_command, validate_command
from app.cache_interop import RustPromptCache, request_key, session_segment
from app.config import ConfigStore
from app.config_layers import activate, inspect
from app.db import Database
from app.extensions import Extensions


def setup_store(tmp_path):
    (tmp_path / "workspace").mkdir()
    return ConfigStore(tmp_path / "data", tmp_path)


@pytest.mark.asyncio
async def test_config_import_survives_restart_with_private_credentials(tmp_path, monkeypatch):
    store = setup_store(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CLAW_CONFIG_HOME", str(home))
    config = {
        "permissionMode": "acceptEdits",
        "enabledPlugins": {"echo": True},
        "apiTimeout": {"maxRetries": 8},
        "env": {"CUSTOM_TOKEN": "private-environment-value"},
        "plugins": {
            "installRoot": "installed",
            "registryPath": "registry.json",
            "maxOutputTokens": 12345,
        },
        "mcpServers": {
            "fixture": {
                "command": "$python",
                "env": {"TOKEN": "private-mcp-value"},
                "toolCallTimeoutMs": 12345,
            }
        },
        "oauth": {
            "clientId": "fixture",
            "authorizeUrl": "https://auth.example/authorize",
            "tokenUrl": "https://auth.example/token",
            "callbackPort": 8000,
        },
    }
    (home / "settings.json").write_text(json.dumps(config), encoding="utf-8")
    report = inspect(store, home, apply=True)
    assert not report["unmapped"]
    assert "private-environment-value" not in json.dumps(report)
    (home / "settings.json").unlink()
    restarted = ConfigStore(store.data, tmp_path)
    activate(restarted)
    assert restarted.current.permission_mode == "workspace-write"
    assert restarted.current.provider_retries == 8
    assert restarted.current.oauth_config["client_id"] == "fixture"
    assert "private-mcp-value" not in json.dumps(restarted.public())
    assert "private-environment-value" not in json.dumps(restarted.public())
    async with httpx.AsyncClient() as client:
        ext = Extensions(Database(store.data / "app.sqlite"), restarted, client)
        assert ext.mcp.list()[0]["timeout"] == 13
        assert ext.plugins.home == Path(store.current.workspace) / "installed"
        await ext.close()


@pytest.mark.asyncio
async def test_plugin_registry_install_update_restart_and_uninstall(tmp_path):
    store = setup_store(tmp_path)
    store.update({"plugin_install_root": "installed", "plugin_registry_path": "registry.json"})
    source = tmp_path / "source"
    (source / ".claude-plugin").mkdir(parents=True)
    manifest = source / ".claude-plugin/plugin.json"
    manifest.write_text(
        json.dumps({"name": "echo", "version": "1.0.0", "tools": []}), encoding="utf-8"
    )
    async with httpx.AsyncClient() as client:
        ext = Extensions(Database(store.data / "app.sqlite"), store, client)
        ext.plugins.install(source)
        registry = Path(store.current.workspace) / "registry.json"
        assert (
            json.loads(registry.read_text(encoding="utf-8"))["plugins"]["echo"]["version"]
            == "1.0.0"
        )
        manifest.write_text(
            json.dumps({"name": "echo", "version": "2.0.0", "tools": []}), encoding="utf-8"
        )
        await ext.plugins.update("echo")
        assert (
            json.loads(registry.read_text(encoding="utf-8"))["plugins"]["echo"]["version"]
            == "2.0.0"
        )
        await ext.close()
        # A fresh database can reconstruct installed metadata from the upstream file.
        ext = Extensions(Database(store.data / "fresh.sqlite"), store, client)
        assert ext.plugins.list()[0]["manifest"]["version"] == "2.0.0"
        await ext.plugins.uninstall("echo")
        assert json.loads(registry.read_text(encoding="utf-8"))["plugins"] == {}
        await ext.close()


def test_cli_export_positional_path_and_json_envelope(tmp_path, monkeypatch, capsys):
    setup_store(tmp_path)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    db = Database(tmp_path / "data/app.sqlite")
    session = db.create_session("transcript")
    db.save_messages(
        session["id"],
        [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "answer"}],
    )
    target = tmp_path / "transcript.md"
    assert (
        cli.main(
            [
                "--data-dir",
                str(tmp_path / "data"),
                "export",
                str(target),
                "--session",
                session["id"],
                "--output-format",
                "json",
            ]
        )
        == 0
    )
    reply = json.loads(capsys.readouterr().out)
    assert reply["action"] == "export" and reply["messages"] == 2
    content = target.read_text(encoding="utf-8")
    assert content.startswith("# Conversation Export") and "## 2. Assistant\n\nanswer" in content
    assert (
        cli.main(["--data-dir", str(tmp_path / "data"), "export", "--output-format", "json"]) == 0
    )
    assert json.loads(capsys.readouterr().out)["markdown"] == content


def test_cache_file_expiry_version_and_serialization(tmp_path):
    cache = RustPromptCache(tmp_path, "session/unsafe")
    request = {
        "model": "fixture",
        "max_tokens": 32,
        "messages": [],
        "tools": [{"name": "tool", "input_schema": {"z": 1, "a": 2}}],
    }
    equivalent = {**request, "tools": [{"input_schema": {"a": 2, "z": 1}, "name": "tool"}]}
    assert request_key(request) == request_key(equivalent)
    response = {"id": "response", "content": [], "model": "fixture"}
    cache.record(request, response, now=100)
    assert cache.lookup(equivalent, now=129) == response
    assert cache.lookup(request, now=130) is None
    assert cache.path(request).parent.parent.name == "session-unsafe"
    assert len(session_segment("太长" * 100)) <= 80


def test_frozen_rust_bash_contracts():
    cases = json.loads(
        Path(__file__)
        .with_name("fixtures")
        .joinpath("bash_upstream.json")
        .read_text(encoding="utf-8")
    )
    assert len(cases) == 648
    for case in cases:
        assert classify_command(case["command"]) == case["intent"], case
        assert (
            validate_command(case["command"], case["mode"], "/workspace")["decision"]
            == case["decision"]
        ), case
