import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from app.compat_harness import extract, extract_bootstrap
from app.remote import ProxyBootstrap, inherited_env
from app.sandbox import wrap
from app.telemetry import JsonlSink, MemorySink, SessionTracer
from app.trident import compact


def test_ccr_descriptor_requires_all_inputs_and_never_exposes_token(tmp_path):
    token = tmp_path / "token"
    token.write_text(" private-token\n")
    env = {
        "CLAUDE_CODE_REMOTE": "true",
        "CCR_UPSTREAM_PROXY_ENABLED": "yes",
        "CLAUDE_CODE_REMOTE_SESSION_ID": "session",
        "CCR_SESSION_TOKEN_PATH": str(token),
        "ANTHROPIC_BASE_URL": "https://example.test/",
    }
    bootstrap = ProxyBootstrap.from_env(env)
    assert (
        bootstrap.should_enable
        and bootstrap.ws_url() == "wss://example.test/v1/code/upstreamproxy/ws"
    )
    assert "private-token" not in repr(bootstrap) + json.dumps(bootstrap.public())
    emitted = bootstrap.subprocess_env(12345)
    assert len(emitted) == 8 and emitted["HTTPS_PROXY"] == "http://127.0.0.1:12345"
    assert inherited_env({"HTTPS_PROXY": "x"}) == {}
    assert inherited_env(emitted) == emitted
    assert not bootstrap.public()["proxy_started"]
    token.unlink()
    assert not ProxyBootstrap.from_env(env).should_enable


def test_namespace_launch_and_unsupported_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr("app.sandbox.probe", lambda network=False: None)
    with pytest.raises(ValueError, match="OS 沙箱"):
        wrap(["sh", "-c", "echo ok"], str(tmp_path), {"namespaceRestrictions": True})
    with pytest.raises(ValueError, match="文件系统隔离"):
        wrap(["sh"], str(tmp_path), {"filesystemMode": "workspace-only"})
    monkeypatch.setattr(
        "app.sandbox.probe", lambda network=False: ("--user", "--net") if network else ("--user",)
    )
    argv, env, isolation = wrap(["sh", "-c", "echo ok"], str(tmp_path), {"isolateNetwork": True})
    assert argv == ["unshare", "--user", "--net", "sh", "-c", "echo ok"]
    assert isolation["network_active"] and not isolation["filesystem_active"]
    assert env["TMPDIR"] == str(tmp_path / ".sandbox-tmp")


def test_trident_supersedes_complete_pairs_and_preserves_mixed_calls():
    def call(key, name, path):
        return {"id": key, "function": {"name": name, "arguments": json.dumps({"path": path})}}

    messages = [
        {"role": "assistant", "tool_calls": [call("read", "read_file", "a.py")]},
        {"role": "tool", "tool_call_id": "read", "content": "old"},
        {"role": "assistant", "tool_calls": [call("write", "write_file", "a.py")]},
        {"role": "tool", "tool_call_id": "write", "content": "new"},
    ]
    result, stats = compact(messages, {"collapse_enabled": False, "cluster_enabled": False})
    assert stats["superseded_count"] == 2 and result == messages[2:]
    messages[0]["tool_calls"].append(call("other", "read_file", "b.py"))
    messages.insert(2, {"role": "tool", "tool_call_id": "other", "content": "still relevant"})
    result, stats = compact(messages, {"collapse_enabled": False, "cluster_enabled": False})
    assert result == messages and stats["superseded_count"] == 0
    plain = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": "short exchange"}
        for i in range(8)
    ]
    assert compact(plain)[1]["messages_collapsed"] == 8
    clustered, stats = compact(
        [{"role": "user", "content": "long related message " * 30} for _ in range(3)]
    )
    assert stats["clusters_found"] == 1 and clustered[0]["role"] == "system"


def test_trace_concurrent_sequence_and_redacted_jsonl(tmp_path):
    sink = MemorySink()
    tracer = SessionTracer("session", sink)
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda n: tracer.record("event", {"n": n}), range(50)))
    assert [event["sequence"] for event in sink.records] == list(range(50))
    path = tmp_path / "trace.jsonl"
    tracer = SessionTracer(
        "session", JsonlSink(path, lambda value: value.replace("private", "[REDACTED]"))
    )
    tracer.record("test", {"key": "private"})
    assert "private" not in path.read_text()
    assert json.loads(path.read_text())["type"] == "session_trace"


def test_archived_source_manifest_extraction():
    commands = extract(
        "import {foo as localFoo, bar} from './commands/a';\nexport const INTERNAL_ONLY_COMMANDS = [\n internal,\n]\n",
        "commands",
    )
    assert commands == [
        {"name": "foo", "source": "Builtin"},
        {"name": "bar", "source": "Builtin"},
        {"name": "internal", "source": "InternalOnly"},
    ]
    assert extract("import {AgentTool, helper} from './tools/a'", "tools") == [
        {"name": "AgentTool", "source": "Base"}
    ]
    assert extract_bootstrap("--version startupProfiler --dump-system-prompt") == [
        "CliEntry",
        "FastPathVersion",
        "StartupProfiler",
        "SystemPromptFastPath",
        "MainRuntime",
    ]
