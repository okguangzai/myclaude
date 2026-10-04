"""Behavior tests for migrated runtime contracts using isolated files and processes."""

import asyncio
import json
import subprocess
from pathlib import Path

import httpx
import pytest
from app.agent import Agent
from app.config import ConfigStore
from app.config_layers import inspect, load
from app.db import Database
from app.extensions import Extensions
from app.git_context import check_base, detect, freshness, stale_action
from app.providers import Provider
from app.recovery import RecoveryContext, simulate
from app.reports import canonicalize, project
from app.tools import Tools
from app.usage import summarize


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


@pytest.mark.asyncio
async def test_hooks_actual_process_rewrite_deny_and_prompt(runtime):
    cfg = runtime.store.current
    path = Path(cfg.workspace)
    (path / "original.txt").write_text("original")
    (path / "target.txt").write_text("target")
    hook = path / "hook.py"
    hook.write_text(
        'import json,sys\nx=json.load(sys.stdin)\nassert x["tool_name"]=="read_file"\nprint(json.dumps({"hookSpecificOutput":{"updatedInput":{"path":"target.txt"}}}))'
    )
    cfg.hooks = {"PreToolUse": [{"matcher": "read_file", "command": ["$python", str(hook)]}]}
    output = await runtime.execute(cfg, "danger-full-access", "read_file", {"path": "original.txt"})
    assert "target" in str(output) and "original" not in str(output)
    # Rewritten input must pass deny rules again, before touching a file.
    cfg.permission_rules = {"deny": ["read_file(target.txt)"]}
    with pytest.raises(PermissionError, match="deny"):
        await runtime.execute(cfg, "danger-full-access", "read_file", {"path": "original.txt"})
    cfg.permission_rules = {}
    hook.write_text('import sys\nprint("blocked-by-hook")\nsys.exit(2)')
    with pytest.raises(PermissionError, match="blocked-by-hook"):
        await runtime.execute(cfg, "danger-full-access", "read_file", {"path": "original.txt"})
    hook.write_text('print(\'{"hookSpecificOutput":{"permissionDecision":"ask"}}\')')
    answers = []

    async def deny(question, options):
        answers.append((question, options))
        return "拒绝"

    runtime.question_handler = deny
    with pytest.raises(PermissionError, match="用户拒绝"):
        await runtime.execute(cfg, "danger-full-access", "read_file", {"path": "original.txt"})
    assert len(answers) == 1


@pytest.mark.asyncio
async def test_running_task_receives_queued_message_at_next_turn(runtime):
    started, release = asyncio.Event(), asyncio.Event()
    prompts = []

    class FakeProvider:
        async def stream(self, cfg, messages, tools, system):
            prompts.append(messages[-1]["content"])
            if len(prompts) == 1:
                started.set()
                await release.wait()
            yield {
                "type": "complete",
                "message": {"role": "assistant", "content": "done-" + str(len(prompts))},
                "usage": {},
                "stop": "stop",
            }

    runtime.jobs.agent = Agent(runtime.db, FakeProvider(), runtime.tools, runtime.store)
    job = runtime.jobs.agent_job("first", "read-only", runtime.store.current)
    await asyncio.wait_for(started.wait(), 5)
    runtime.jobs.append_message(job["id"], "follow-up")
    release.set()
    done = await runtime.jobs.wait(job["id"], 10)
    assert prompts == ["first", "follow-up"]
    assert done["status"] == "completed" and done["output"] == "done-2"
    assert not runtime.jobs.agent.active
    assert runtime.jobs.record("task_inbox", job["session_id"]) is None


@pytest.mark.asyncio
async def test_git_context_base_and_stale_branch_use_real_repository(tmp_path):
    def git(*args):
        return subprocess.check_output(
            ["git", *args], cwd=tmp_path, text=True, stderr=subprocess.DEVNULL
        ).strip()

    git("init", "-q", "-b", "main")
    git("config", "user.name", "Migration Test")
    git("config", "user.email", "migration@example.test")
    git("-c", "core.hooksPath=", "commit", "--allow-empty", "-qm", "initial")
    base = git("rev-parse", "HEAD")
    git("branch", "worker")
    git("-c", "core.hooksPath=", "commit", "--allow-empty", "-qm", "fix: startup")
    state = await freshness(str(tmp_path), "worker", "main")
    assert state["state"] == "stale" and state["behind"] == 1
    assert state["missing_fixes"] == ["fix: startup"]
    assert stale_action(state, "block")["kind"] == "block"
    assert stale_action(state, "auto_rebase")["kind"] == "rebase"
    assert git("rev-parse", "worker") == base  # Evaluating a policy never performs a rebase.
    (tmp_path / ".claw-base").write_text(base)
    assert (await check_base(str(tmp_path)))["state"] == "diverged"
    assert (await check_base(str(tmp_path), "HEAD"))["state"] == "matches"
    assert (await detect(str(tmp_path)))["recent_commits"][0]["subject"] == "fix: startup"
    with pytest.raises(ValueError):
        await freshness(str(tmp_path), "missing-branch", "main")


@pytest.mark.asyncio
async def test_recovery_partial_failure_then_exhaustion_without_second_execution():
    context = RecoveryContext()
    observed = []

    async def execute(step):
        observed.append(step)
        if step == "clean_build":
            raise ValueError("test compiler failed")
        return "actual executor result"

    first = await context.attempt("stale_branch", execute)
    assert first["partial_recovery"]["recovered"] == ["rebase_branch"]
    assert context.status("stale_branch")["state"] == "failed"
    second = await context.attempt("stale_branch", execute)
    assert "escalation_required" in second and len(observed) == 2
    simulated = await simulate("provider_failure", attempts=2)
    assert simulated["side_effects_executed"] is False
    assert simulated["status"]["state"] == "exhausted"


def test_report_identity_stable_and_all_sensitive_families_filtered():
    def claim(key, sensitivity):
        return {
            "id": key,
            "kind": "observed_fact",
            "text": "sensitive-" + key,
            "confidence": "high",
            "sensitivity": sensitivity,
            "evidence": [key],
        }

    report = {
        "generated_at": "2026-10-04T00:00:00Z",
        "producer": "test",
        "claims": [claim("b", "secret"), claim("a", "internal"), claim("c", "public")],
        "negative_evidence": [
            {
                "id": "negative",
                "status": "unknown_not_checked",
                "query": "secret-query",
                "window": "today",
                "sensitivity": "secret",
            }
        ],
    }
    first = canonicalize(report)
    report["claims"].reverse()
    assert first["identity"] == canonicalize(report)["identity"]
    view = project(
        report, {"consumer": "public-ui", "max_sensitivity": "public", "schema_versions": []}
    )
    encoded = json.dumps(view)
    assert (
        "sensitive-b" not in encoded
        and "sensitive-a" not in encoded
        and "secret-query" not in encoded
    )
    assert "sensitive-c" in encoded and view["provenance"]["downgraded"]
    assert len(view["provenance"]["redactions"]) == 3


@pytest.mark.asyncio
async def test_config_precedence_secret_mask_and_atomic_import(runtime, tmp_path):
    home = tmp_path / "home/.claw"
    home.mkdir(parents=True)
    (home / "settings.json").write_text(
        json.dumps({"model": "user", "provider": {"apiKey": "secret-legacy", "kind": "openai"}})
    )
    cwd = Path(runtime.store.current.workspace)
    (cwd / ".claw").mkdir()
    (cwd / ".claw/settings.json").write_text(
        json.dumps({"model": "project", "aliases": {"fast": "project-fast"}})
    )
    local = cwd / ".claw/settings.local.json"
    local.write_text(json.dumps({"model": "local"}))
    report = load(cwd, home)
    assert report["merged"]["model"] == "local"
    assert report["merged"]["provider"]["apiKey"] == "secret-legacy"
    assert report["provenance"]["model"] == str(local)
    assert "secret-legacy" not in json.dumps(inspect(runtime.store, home))
    assert inspect(runtime.store, home, apply=True)["applied"]
    assert (
        runtime.store.current.model == "local"
        and runtime.store.current.model_aliases["fast"] == "project-fast"
    )
    local.write_text('{"model":"should-not-apply","unsupported":true}')
    with pytest.raises(ValueError, match="未映射"):
        inspect(runtime.store, home, apply=True)
    assert runtime.store.current.model == "local"


@pytest.mark.asyncio
async def test_reasoning_history_and_cache_usage_protocol(runtime):
    payloads = []
    chunks = [
        {"choices": [{"index": 0, "delta": {"reasoning_content": "provider reasoning"}}]},
        {
            "choices": [{"index": 0, "delta": {"content": "answer"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 12,
                "prompt_tokens_details": {"cached_tokens": 70},
            },
        },
    ]

    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200, text="\n\n".join("data: " + json.dumps(c) for c in chunks) + "\n\ndata: [DONE]\n\n"
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        cfg = runtime.store.current.model_copy(
            update={"api_key": "fake", "model": "deepseek-v4-test", "reasoning_effort": "high"}
        )
        provider = Provider(client)
        events = [e async for e in provider.stream(cfg, [], [], "test")]
        result = events[-1]
        assert (
            result["usage"]["input_tokens"] == 30
            and result["usage"]["cache_read_input_tokens"] == 70
        )
        assert result["message"]["thinking"][0]["thinking"] == "provider reasoning"
        [e async for e in provider.stream(cfg, [result["message"]], [], "test")]
    assert payloads[-1]["messages"][1]["reasoning_content"] == "provider reasoning"
    assert payloads[-1]["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_session_run_exclusion_across_database_instances(runtime):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    session = runtime.db.create_session("concurrent")
    another = Database(runtime.db.path, recover=False)
    barrier = Barrier(2)

    def claim(db):
        barrier.wait(timeout=5)
        try:
            return db.start_run(session["id"], "test", "read-only")
        except ValueError:
            return None

    with ThreadPoolExecutor(2) as pool:
        claimed = list(pool.map(claim, [runtime.db, another]))
    assert sum(value is not None for value in claimed) == 1
    with pytest.raises(ValueError, match="运行中"):
        runtime.sessions.compact(session["id"])
    runtime.db.finish_run(next(value for value in claimed if value), "completed", 1, {})
    assert another.start_run(session["id"], "test", "read-only")


def test_usage_cost_requires_explicit_pricing_or_labelled_source_compatibility():
    messages = [
        {"usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 50}}
    ]
    assert summarize(messages)["estimated_cost"] is None
    configured = summarize(
        messages, rates={"input_tokens": 1, "output_tokens": 2, "cache_read_input_tokens": 0.1}
    )
    assert configured["total_tokens"] == 170
    assert configured["estimated_cost"]["total_cost_usd"] == pytest.approx(0.000145)
    assert (
        "not current pricing"
        in summarize(messages, "unknown", source_compatibility=True)["pricing_source"]
    )


@pytest.mark.asyncio
async def test_latest_session_excludes_current_and_other_workspaces(runtime, tmp_path):
    first = runtime.db.create_session("current-workspace")
    runtime.db.save_messages(first["id"], [{"role": "user", "content": "first"}])
    second = runtime.db.create_session("other-workspace")
    runtime.db.save_messages(second["id"], [{"role": "user", "content": "second"}])
    runtime.jobs.put("session_metadata", second["id"], {"workspace_root": str(tmp_path / "other")})
    assert runtime.sessions.resolve("latest")["id"] == first["id"]
    with pytest.raises(ValueError, match="其他非空会话"):
        runtime.sessions.resolve("latest", exclude_id=first["id"])


def test_skill_multiline_frontmatter_and_alias_rejection():
    from app.assets import frontmatter

    value = frontmatter(
        "---\r\nname: reviewer\r\ndescription: |\r\n  First line\r\n  Second line\r\ntools: [read_file, glob_search]\r\n---\r\nInstructions"
    )
    assert value["description"] == "First line\nSecond line\n"
    assert value["tools"] == ["read_file", "glob_search"]
    with pytest.raises(ValueError, match="alias"):
        frontmatter("---\na: &a [x]\nb: *a\n---\n")


@pytest.mark.asyncio
async def test_task_packet_full_runtime_fields_are_accepted_and_persisted(runtime):
    from types import SimpleNamespace

    from app.extensions import CATALOG, SPECS

    runtime.jobs.agent = SimpleNamespace(active=set())
    packet = {
        "objective": "inspect a module",
        "scope": "module",
        "scope_path": "backend",
        "repo": runtime.store.current.workspace,
        "branch_policy": "stay",
        "commit_policy": "none",
        "acceptance_criteria": ["explain findings"],
        "reporting_targets": ["local"],
        "recovery_policy": "stop",
        "resources": [{"kind": "directory", "value": "backend"}],
    }
    job = await runtime.execute(
        runtime.store.current, "danger-full-access", "RunTaskPacket", packet
    )
    assert job["task_packet"] == packet
    assert "scope_path" in SPECS["RunTaskPacket"]["input_schema"]["properties"]
    assert (
        "scope_path"
        not in next(t for t in CATALOG["tools"] if t["name"] == "RunTaskPacket")["input_schema"][
            "properties"
        ]
    )
    await runtime.jobs.stop(job["id"])


@pytest.mark.asyncio
async def test_mcp_pagination_and_repeated_cursor_guard(runtime, monkeypatch):
    class Connection:
        repeat = False

        async def call(self, method, **args):
            if not args.get("cursor"):
                return {"tools": [{"name": "first"}], "nextCursor": "page2"}
            return {"tools": [{"name": "second"}], "nextCursor": "page2" if self.repeat else None}

    connection = Connection()

    async def connect(*args):
        return connection

    monkeypatch.setattr(runtime.mcp, "connect", connect)
    result = await runtime.mcp.call("fixture", "read-only", "list_tools")
    assert [tool["name"] for tool in result["tools"]] == ["first", "second"]
    assert len(runtime.mcp.cache["fixture"]) == 2
    connection.repeat = True
    with pytest.raises(ValueError, match="游标重复"):
        await runtime.mcp.call("fixture", "read-only", "list_tools")


@pytest.mark.asyncio
async def test_fallback_routes_switch_protocol_only_before_visible_output(runtime, monkeypatch):
    from app.config import Settings
    from app.providers import ProviderError

    monkeypatch.setenv("MIGRATION_BACKUP_KEY", "fixture-backup-secret")
    cfg = Settings.model_validate(
        {
            **runtime.store.current.model_dump(),
            "model": "primary",
            "base_url": "https://primary.test/v1",
            "api_key": "fixture-primary",
            "provider_retries": 0,
            "fallback_models": ["missing", "backup"],
            "provider_routes": {
                "missing": {
                    "provider": "openai",
                    "base_url": "https://missing.test",
                    "api_key_env": "MIGRATION_ABSENT_KEY",
                },
                "backup": {
                    "provider": "anthropic",
                    "base_url": "https://backup.test",
                    "api_key_env": "MIGRATION_BACKUP_KEY",
                    "model": "backup-wire-model",
                },
            },
        }
    )
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.host == "primary.test":
            return httpx.Response(503)
        assert request.headers["x-api-key"] == "fixture-backup-secret"
        assert json.loads(request.content)["model"] == "backup-wire-model"
        events = [
            {"type": "message_start", "message": {"usage": {"input_tokens": 7}}},
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "backup answer"},
            },
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 3},
            },
            {"type": "message_stop"},
        ]
        return httpx.Response(
            200, text="\n\n".join("data: " + json.dumps(event) for event in events) + "\n\n"
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = Provider(client, store=runtime.store)
        result = [event async for event in provider.stream(cfg, [], [], "test")]
        assert result[-1]["message"]["content"] == "backup answer"
        assert result[-1]["model"] == "backup-wire-model"
        assert [event["status"] for event in result if event["type"] == "provider_fallback"] == [
            "retryable_failure",
            "skipped",
        ]
        assert [request.url.host for request in calls] == ["primary.test", "backup.test"]
        assert runtime.store.redact("fixture-backup-secret") == "[REDACTED]"
        calls.clear()

        async def partial(*args):
            yield {"type": "delta", "text": "already visible"}
            raise ProviderError("stream broke", retryable=True)

        monkeypatch.setattr(provider, "_openai", partial)
        with pytest.raises(ProviderError, match="stream broke"):
            [event async for event in provider.stream(cfg, [], [], "test")]
        assert calls == []  # Never called the backup after exposing primary output.
