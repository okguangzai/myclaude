import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from app import worker
from app.assets import Assets
from app.config import ROOT, ConfigStore
from app.db import Database
from app.extensions import Extensions
from app.oauth import OAuth, code_challenge
from app.orchestration import ApprovalLedger, branch_collisions, evaluate_policy, green_contract
from app.policy import tool_allowed
from app.sessions import compact_messages, compress_summary
from app.task_packet import validate_packet
from app.tools import Tools


@pytest.fixture
async def runtime(tmp_path):
    (tmp_path / "workspace").mkdir()
    store = ConfigStore(tmp_path / "data", tmp_path)
    db = Database(store.data / "app.sqlite")
    async with httpx.AsyncClient(trust_env=False) as client:
        ext = Extensions(db, store, client)
        Tools(None, ext)
        yield ext
        await ext.close()


def test_pkce_rfc7636_vector():
    assert (
        code_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
        == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )


@pytest.mark.asyncio
async def test_oauth_state_refresh_and_secret_boundaries(runtime):
    requests = []

    def handler(request):
        payload = parse_qs(request.content.decode())
        requests.append(payload)
        if payload["grant_type"] == ["authorization_code"]:
            assert payload["code"] == ["code-one"]
            assert len(payload["code_verifier"][0]) >= 43
            return httpx.Response(
                200,
                json={
                    "access_token": "access-secret-one",
                    "refresh_token": "refresh-secret-one",
                    "expires_in": 0,
                },
            )
        assert payload["refresh_token"] == ["refresh-secret-one"]
        return httpx.Response(200, json={"access_token": "access-secret-two", "expires_in": 3600})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = OAuth(runtime.store, client)
        service.save({"unrelated": {"keep": True}})
        started = service.begin(
            {
                "authorize_url": "https://example.test/authorize",
                "token_url": "https://example.test/token",
                "client_id": "public-client",
                "scopes": ["read", "write"],
            }
        )
        query = parse_qs(urlsplit(started["authorize_url"]).query)
        assert query["code_challenge_method"] == ["S256"]
        with pytest.raises(ValueError, match="state"):
            await service.complete("code=code-one&state=forged")
        state = query["state"][0]
        public = await service.complete("code=code-one&state=" + state)
        assert public["authenticated"]
        assert "access-secret" not in json.dumps(public)
        with pytest.raises(ValueError, match="state"):
            await service.complete("code=code-one&state=" + state)
        tokens = await asyncio.gather(*(service.access_token() for _ in range(8)))
        assert tokens == ["access-secret-two"] * 8
        assert len(requests) == 2  # simultaneous callers share one refresh
        assert service.load()["oauth"]["refreshToken"] == "refresh-secret-one"
        assert (
            runtime.store.redact("access-secret-two refresh-secret-one") == "[REDACTED] [REDACTED]"
        )
        await service.logout()
        assert service.load()["unrelated"] == {"keep": True}
        assert not service.status()["authenticated"]


@pytest.mark.asyncio
async def test_skill_install_agent_definition_and_scope_precedence(runtime, tmp_path):
    source = tmp_path / "source-skill"
    source.mkdir()
    (source / "SKILL.md").write_text(
        "---\nname: Code Review\ndescription: read first\n---\nReview code carefully",
        encoding="utf-8",
    )
    (source / "helper.txt").write_text("helper", encoding="utf-8")
    registry = Assets(runtime.store)
    installed = registry.install_skill(str(source))
    assert installed["name"] == "code-review"
    assert (runtime.store.data / "skills/code-review/helper.txt").read_text() == "helper"
    with pytest.raises(ValueError):
        registry.install_skill(str(source))
    result = await runtime.execute(
        runtime.store.current, "read-only", "Skill", {"skill": "code-review"}
    )
    assert "Review code" in result["instructions"]
    agent = registry.create_agent("reviewer", prompt="find bugs", model="test-model")
    assert agent["metadata"]["model"] == "test-model"
    with pytest.raises(FileExistsError):
        registry.create_agent("reviewer")
    registry.uninstall_skill("code-review")
    assert not registry.list("skills")


@pytest.mark.asyncio
async def test_session_rust_jsonl_roundtrip_fork_and_invalid_pairs(runtime):
    document = {
        "version": 1,
        "session_id": "original",
        "workspace_root": runtime.store.current.workspace,
        "messages": [
            {"role": "user", "blocks": [{"type": "text", "text": "inspect"}]},
            {
                "role": "assistant",
                "blocks": [
                    {"type": "thinking", "thinking": "check", "signature": "signature"},
                    {
                        "type": "tool_use",
                        "id": "call-1",
                        "name": "read_file",
                        "input": '{"path":"a.py"}',
                    },
                ],
            },
            {
                "role": "tool",
                "blocks": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call-1",
                        "tool_name": "read_file",
                        "output": "hello",
                        "is_error": False,
                    }
                ],
            },
            {"role": "assistant", "blocks": [{"type": "text", "text": "done"}]},
        ],
    }
    imported = runtime.sessions.import_text(json.dumps(document))
    assert imported["id"] != "original"
    text = runtime.sessions.export(imported["id"], "jsonl")
    restored = runtime.sessions.import_text(text)
    assert restored["messages"] == imported["messages"]
    fork = runtime.sessions.fork(restored["id"], "review")
    assert fork["messages"] == restored["messages"]
    exported = json.loads(runtime.sessions.export(fork["id"]))
    assert exported["fork"]["parent_session_id"] == restored["id"]
    document["messages"].pop(1)
    with pytest.raises(ValueError, match="工具结果"):
        runtime.sessions.import_text(json.dumps(document))


def test_compaction_keeps_tool_pairs_and_unicode_summary():
    messages = [
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "old response"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "x", "function": {"name": "read_file", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "x", "content": "file"},
        {"role": "assistant", "content": "done"},
    ]
    result, report = compact_messages(messages, preserve=2)
    assert result[1:] == messages[2:]
    assert report["removed_message_count"] == 2
    summary = compress_summary(
        "Conversation summary:\n- Scope:  abc\n- Scope: abc\n- Current work: 中文" * 10,
        max_chars=120,
        max_lines=3,
    )
    assert len(summary) <= 120
    assert len(summary.splitlines()) <= 3


def test_worker_source_scenarios(tmp_path):
    w = worker.create("one", str(tmp_path))
    assert not worker.ready_snapshot(w)["ready"]
    worker.observe(w, "Do you trust the files in this folder?")
    assert worker.ready_snapshot(w)["blocked"]
    worker.resolve_trust(w)
    assert w["status"] == "spawning"
    worker.observe(w, "Ready for input")
    worker.send_prompt(w, "implement a test")
    worker.observe(w, "implement a test: command not found")
    assert w["status"] == "ready_for_prompt" and w["replay_prompt"]
    worker.send_prompt(w)
    assert w["prompt_delivery_attempts"] == 2
    worker.completion(w, "unknown", 0)
    assert w["last_error"]["kind"] == "provider"
    worker.restart(w)
    assert w["status"] == "spawning" and w["prompt_delivery_attempts"] == 0
    trusted = worker.create("two", str(tmp_path / "child"), [str(tmp_path)])
    worker.observe(trusted, "Trust this folder")
    assert trusted["trust_gate_cleared"]
    assert [e["kind"] for e in trusted["events"]] == [
        "spawning",
        "trust_required",
        "trust_resolved",
    ]


@pytest.mark.asyncio
async def test_allowed_tools_cannot_be_bypassed_with_direct_call(runtime):
    runtime.store.update({"allowed_tools": ["read_*", "write_file"], "denied_tools": ["write_*"]})
    assert tool_allowed(runtime.store.current, "read_file")
    assert not tool_allowed(runtime.store.current, "write_file")
    assert {
        s["function"]["name"] for s in runtime.tools.definitions("danger-full-access", False)
    } == {"read_file"}
    with pytest.raises(PermissionError):
        await runtime.execute(
            runtime.store.current,
            "danger-full-access",
            "write_file",
            {"path": "a", "content": "no"},
        )


@pytest.mark.asyncio
async def test_plugin_update_is_atomic_and_rolls_back_failed_init(runtime, tmp_path):
    source = tmp_path / "plugin"
    (source / ".claude-plugin").mkdir(parents=True)
    manifest = {"name": "updatable", "version": "1", "tools": []}
    path = source / ".claude-plugin/plugin.json"
    path.write_text(json.dumps(manifest))
    runtime.plugins.install(str(source))
    await runtime.plugins.enable("updatable", True)
    manifest["version"] = "2"
    path.write_text(json.dumps(manifest))
    updated = await runtime.plugins.update("updatable")
    assert updated["new_version"] == "2" and updated["plugin"]["enabled"]
    manifest["version"] = "3"
    manifest["lifecycle"] = {"Init": ['$python -c "raise RuntimeError()"']}
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        await runtime.plugins.update("updatable")
    item, _ = runtime.plugins.verified("updatable")
    assert item["manifest"]["version"] == "2" and item["enabled"]


def test_task_packet_rejects_blank_contract_and_scope():
    good = {
        "objective": "work",
        "repo": ".",
        "scope": "workspace",
        "branch_policy": "current",
        "commit_policy": "none",
        "acceptance_tests": ["test"],
        "reporting_contract": "summary",
        "escalation_policy": "ask",
    }
    assert validate_packet(good) == good
    for change in (
        {"scope": "read"},
        {"acceptance_tests": [""]},
        {"scope": "module"},
        {"objective": " "},
    ):
        with pytest.raises(ValueError):
            validate_packet({**good, **change})


def test_cli_help_offline_and_does_not_recover_other_process_jobs(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "backend"), "PYTHONIOENCODING": "utf-8"}
    data = tmp_path / "data"
    data.mkdir()
    (data / "settings.json").write_text("{broken")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.cli",
            "--data-dir",
            str(data),
            "help",
            "--output-format",
            "json",
        ],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert result.returncode == 0 and json.loads(result.stdout)["kind"] == "help"
    db = Database(data / "runs.sqlite")
    sid = db.create_session()["id"]
    run_id = db.start_run(sid, "test", "read-only")
    second = Database(data / "runs.sqlite", recover=False)
    assert second.all("SELECT status FROM runs WHERE id=?", (run_id,))[0]["status"] == "running"


def test_orchestration_source_contracts():
    collisions = branch_collisions(
        [
            {"laneId": "a", "branch": "work", "modules": ["runtime"]},
            {"laneId": "b", "branch": "work", "modules": ["runtime/mcp"]},
            {"laneId": "c", "branch": "other", "modules": ["runtime"]},
        ]
    )
    assert collisions == [{"branch": "work", "module": "runtime", "laneIds": ["a", "b"]}]
    contract = {
        "required_level": "workspace",
        "requirements": [
            "test_command_provenance",
            "base_branch_freshness",
            "recovery_attempt_context",
        ],
        "block_known_flakes": True,
    }
    evidence = {
        "observed_level": "workspace",
        "test_commands": [{"command": "pytest", "exit_code": 0}],
        "base_branch_fresh": False,
        "recovery_attempt_context_recorded": True,
    }
    assert green_contract(contract, evidence)["missing"] == ["base_branch_freshness"]
    evidence["base_branch_fresh"] = True
    assert green_contract(contract, evidence)["outcome"] == "satisfied"
    rules = [
        {
            "name": "merge",
            "priority": 2,
            "condition": {"kind": "green_at", "level": 2},
            "action": "merge_to_dev",
        },
        {
            "name": "retry",
            "priority": 1,
            "condition": "startup_blocked",
            "action": {
                "kind": "chain",
                "actions": ["recover_once", {"kind": "notify", "channel": "local"}],
            },
        },
    ]
    context = {"lane_id": "a", "green_level": 3, "blocker": "startup"}
    assert [a["kind"] for a in evaluate_policy(rules, context)["actions"]] == [
        "recover_once",
        "notify",
    ]
    context["green_contract_satisfied"] = True
    assert [a["kind"] for a in evaluate_policy(rules, context)["actions"]] == [
        "recover_once",
        "notify",
        "merge_to_dev",
    ]


@pytest.mark.asyncio
async def test_approval_scope_actor_expiration_and_replay(runtime):
    ledger = ApprovalLedger(runtime.jobs)
    scope = {"policy": "merge", "action": "git merge", "repository": "repo", "branch": "main"}
    grant = {
        "token": "test-token",
        "scope": scope,
        "approving_actor": "owner",
        "approved_executor": "worker",
        "expires_at_epoch_seconds": 100,
    }
    ledger.insert(grant)
    with pytest.raises(ValueError, match="pending"):
        ledger.verify("test-token", scope, "worker", 10)
    ledger.insert({**grant, "status": "approval_granted"})
    with pytest.raises(ValueError, match="scope_mismatch"):
        ledger.verify("test-token", {**scope, "branch": "other"}, "worker", 10)
    with pytest.raises(ValueError, match="unauthorized_delegate"):
        ledger.verify("test-token", scope, "other", 10)
    with pytest.raises(ValueError, match="expired"):
        ledger.verify("test-token", scope, "worker", 101)
    assert ledger.verify("test-token", scope, "worker", 100, consume=True)["delegated_execution"]
    with pytest.raises(ValueError, match="already_consumed"):
        ledger.verify("test-token", scope, "worker", 100, consume=True)


@pytest.mark.asyncio
async def test_mcp_server_both_framings_and_real_client(runtime, tmp_path):
    import io

    from app.mcp_server import McpServer, read_frame, write_frame

    server = McpServer(runtime)
    assert await server.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert (await server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "unknown"}))["error"][
        "code"
    ] == -32601
    for framed in [False, True]:
        incoming, outgoing = io.BytesIO(), io.BytesIO()
        write_frame(incoming, {"jsonrpc": "2.0", "id": "中文", "method": "initialize"}, framed)
        incoming.seek(0)
        await server.serve(incoming, outgoing)
        outgoing.seek(0)
        raw, mode = read_frame(outgoing)
        assert mode == framed and json.loads(raw)["id"] == "中文"
    # Official SDK client starts a separate Python MCP server and calls a real file tool.
    workspace = Path(runtime.store.current.workspace)
    (workspace / "mcp-proof.txt").write_text("python-runtime-mcp", encoding="utf-8")
    launcher = tmp_path / "launch_mcp.py"
    launcher.write_text(
        f"import sys\nsys.path.insert(0, {str(ROOT / 'backend')!r})\nfrom app.mcp_server import main\nmain()\n",
        encoding="utf-8",
    )
    await runtime.mcp.configure(
        {
            "name": "runtime",
            "transport": "stdio",
            "command": "$python",
            "args": [
                str(launcher),
                "--data-dir",
                str(runtime.store.data),
                "--workspace",
                str(workspace),
            ],
            "permission": "read-only",
            "enabled": True,
        }
    )
    discovered = await runtime.mcp.discover("runtime", "read-only")
    assert any(t["name"] == "read_file" for t in discovered["list_tools"]["tools"])
    response = await runtime.mcp.call(
        "runtime", "read-only", "call_tool", name="read_file", arguments={"path": "mcp-proof.txt"}
    )
    assert "python-runtime-mcp" in str(response)


@pytest.mark.asyncio
async def test_provider_retries_only_before_stream_is_visible(runtime, monkeypatch):
    from app.providers import Provider, ProviderError

    provider = Provider(runtime.client)
    cfg = runtime.store.current.model_copy(update={"api_key": "fake-key", "provider_retries": 1})
    calls = []

    async def transient(*args):
        calls.append(1)
        if len(calls) == 1:
            raise ProviderError("temporary", retryable=True)
        yield {"type": "complete", "message": {"role": "assistant", "content": "ok"}}

    monkeypatch.setattr(provider, "_openai", transient)
    assert len([e async for e in provider.stream(cfg, [], [], "")]) == 1
    assert len(calls) == 2
    calls.clear()

    async def partial(*args):
        calls.append(1)
        yield {"type": "delta", "text": "partial"}
        raise ProviderError("temporary", retryable=True)

    monkeypatch.setattr(provider, "_openai", partial)
    with pytest.raises(ProviderError):
        [e async for e in provider.stream(cfg, [], [], "")]
    assert len(calls) == 1
