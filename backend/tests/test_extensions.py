import asyncio
import json
import os
import socket
import sys
from pathlib import Path

import httpx
import pytest
from app.agent import Agent
from app.commands import UNSUPPORTED, Commands
from app.config import ROOT, ConfigStore
from app.db import Database
from app.execution import RunContext, current, process
from app.extensions import CATALOG, Extensions
from app.tools import Tools
from jsonschema import ValidationError


class Provider:
    async def stream(self, cfg, messages, tools, system):
        yield {
            "type": "complete",
            "message": {"role": "assistant", "content": "verified: " + messages[-1]["content"]},
            "usage": {"input_tokens": 2, "output_tokens": 3},
            "stop": "stop",
        }


@pytest.fixture
async def runtime(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "README.txt").write_text("hello agent\nsecond line\nhello again\n", encoding="utf-8")
    store = ConfigStore(tmp_path / "data", tmp_path)
    store.update({"workspace": str(ws), "rag_enabled": False})
    db = Database(tmp_path / "data" / "app.sqlite")
    async with httpx.AsyncClient(trust_env=False) as client:
        ext = Extensions(db, store, client)
        tools = Tools(None, ext)
        ext.jobs.agent = Agent(db, Provider(), tools, store)
        yield ext
        await ext.close()


async def call(e, name, args=None, permission="danger-full-access"):
    return await e.execute(e.store.current.model_copy(deep=True), permission, name, args or {})


@pytest.mark.asyncio
async def test_catalog_and_permission_enforcement(runtime):
    e = runtime
    assert len(CATALOG["tools"]) == 55
    assert len(CATALOG["commands"]) == 140
    for spec in CATALOG["tools"]:
        if spec["required_permission"] != "read-only":
            with pytest.raises(PermissionError):
                await call(e, spec["name"], {}, "read-only")
    with pytest.raises(ValidationError):
        await call(e, "read_file", {"path": "README.txt", "injected": True})
    with pytest.raises(ValueError):
        await call(e, "read_file", {"path": ".env"})
    statuses = Commands(e).catalog()
    assert all(
        c["python_status"] == "available-parity-pending"
        for c in statuses
        if c["upstream_implemented"]
    )
    assert all(not c["upstream_implemented"] for c in statuses if c["name"] in UNSUPPORTED)


@pytest.mark.asyncio
async def test_file_tools_and_regex(runtime):
    e = runtime
    assert (await call(e, "read_file", {"path": "README.txt", "offset": 1, "limit": 1}))[
        "content"
    ] == "second line\n"
    with pytest.raises(ValueError, match="匹配 2"):
        await call(
            e, "edit_file", {"path": "README.txt", "old_string": "hello", "new_string": "hi"}
        )
    assert (
        await call(
            e,
            "edit_file",
            {"path": "README.txt", "old_string": "hello", "new_string": "hi", "replace_all": True},
        )
    )["replacements"] == 2
    assert len((await call(e, "grep_search", {"pattern": "HI", "-i": True}))["matches"]) == 2
    assert (await call(e, "glob_search", {"pattern": "*.txt"}))["matches"] == ["README.txt"]
    assert (await call(e, "grep_search", {"pattern": "hi", "output_mode": "count"}))["matches"][0][
        "count"
    ] == 2
    p = Path(e.store.current.workspace) / "test.ipynb"
    p.write_text(json.dumps({"nbformat": 4, "nbformat_minor": 5, "metadata": {}, "cells": []}))
    await call(
        e,
        "NotebookEdit",
        {"notebook_path": "test.ipynb", "edit_mode": "insert", "new_source": "print(1)"},
    )
    cell = json.loads(p.read_text())["cells"][0]
    await call(
        e,
        "NotebookEdit",
        {
            "notebook_path": "test.ipynb",
            "cell_id": cell["id"],
            "new_source": "# title",
            "cell_type": "markdown",
        },
    )
    assert "outputs" not in json.loads(p.read_text())["cells"][0]
    await call(
        e,
        "NotebookEdit",
        {"notebook_path": "test.ipynb", "cell_id": cell["id"], "edit_mode": "delete"},
    )
    assert not json.loads(p.read_text())["cells"]


@pytest.mark.asyncio
async def test_plan_cannot_leak_write_tools(runtime):
    token = current.set(RunContext(permission="workspace-write"))
    try:
        await call(runtime, "EnterPlanMode", permission="workspace-write")
        names = {x["function"]["name"] for x in runtime.tools.definitions("workspace-write", False)}
        assert "write_file" not in names
        assert "ExitPlanMode" in names
        with pytest.raises(PermissionError):
            await call(
                runtime, "write_file", {"path": "no.txt", "content": "no"}, "workspace-write"
            )
        await call(runtime, "ExitPlanMode", permission="workspace-write")
        await call(runtime, "write_file", {"path": "yes.txt", "content": "yes"}, "workspace-write")
    finally:
        current.reset(token)


@pytest.mark.asyncio
async def test_hook_argument_rewrite_is_validated_before_side_effect(runtime, monkeypatch):
    async def hook(event, name, args, permission, output=None):
        return {**args, "unexpected": "injected"}

    monkeypatch.setattr(runtime.plugins, "hooks", hook)
    with pytest.raises(ValidationError):
        await call(runtime, "write_file", {"path": "blocked.txt", "content": "no"})
    assert not (Path(runtime.store.current.workspace) / "blocked.txt").exists()


@pytest.mark.asyncio
async def test_parent_cancellation_stops_background_children(runtime):
    e = runtime
    entered = asyncio.Event()

    class WaitingProvider:
        async def stream(self, cfg, messages, tools, system):
            if messages[0]["content"] == "child":
                entered.set()
                await asyncio.sleep(100)
            if messages[-1]["role"] == "user":
                yield {
                    "type": "complete",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "child",
                                "type": "function",
                                "function": {
                                    "name": "TaskCreate",
                                    "arguments": '{"prompt":"child"}',
                                },
                            }
                        ],
                    },
                    "usage": {},
                    "stop": "tool_calls",
                }
            else:
                await asyncio.sleep(100)

    e.jobs.agent.provider = WaitingProvider()
    parent = e.jobs.agent_job("parent", "danger-full-access", e.store.current)
    await asyncio.wait_for(entered.wait(), 3)
    await e.jobs.stop(parent["id"])
    assert not e.jobs.running and not e.jobs.agent.active
    assert all(j["status"] == "cancelled" for j in e.jobs.list())


@pytest.mark.asyncio
async def test_process_output_timeout_and_background(runtime):
    e = runtime
    output = await call(e, "REPL", {"language": "python", "code": "print('x'*200000)"})
    assert output["truncated"] and len(output["stdout"]) == 128000
    assert (
        (await call(e, "PowerShell", {"command": "Write-Output 'POWERSHELL_OK'"}))["exit_code"] == 0
        if os.name == "nt"
        else True
    )
    with pytest.raises(TimeoutError):
        await call(
            e,
            "REPL",
            {"language": "python", "code": "import time;time.sleep(30)", "timeout_ms": 100},
        )
    with pytest.raises(ValueError, match="沙箱"):
        await call(e, "bash", {"command": "echo x", "isolateNetwork": True})
    if os.name == "nt":
        job = await call(
            e, "PowerShell", {"command": "Start-Sleep -Seconds 30", "run_in_background": True}
        )
        await asyncio.sleep(0.2)
        assert (await call(e, "TaskStop", {"task_id": job["id"]}))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_mcp_real_stdio_tools_resources_prompts(runtime):
    e = runtime
    await e.mcp.configure(
        {
            "name": "example",
            "command": "$python",
            "args": [str(ROOT / "examples/mcp_server.py")],
            "enabled": True,
            "permission": "read-only",
        }
    )
    discovered = await e.mcp.discover("example", "read-only")
    assert discovered["list_tools"]["tools"][0]["name"] == "add"
    dynamic = next(
        t["function"]["name"]
        for t in e.definitions("read-only")
        if t["function"]["name"].startswith("mcp__example__add")
    )
    assert not (await call(e, dynamic, {"a": 1, "b": 2}, "read-only")).get("isError")
    result = await call(
        e, "MCP", {"server": "example", "tool": "add", "arguments": {"a": 3, "b": 4}}
    )
    assert "7" in json.dumps(result) and not result.get("isError")
    resources = await call(e, "ListMcpResources", {"server": "example"}, "read-only")
    assert resources["resources"]
    data = await call(
        e, "ReadMcpResource", {"server": "example", "uri": "learning://agent-loop"}, "read-only"
    )
    assert "permission check" in json.dumps(data)
    prompt = await e.mcp.call(
        "example", "read-only", "get_prompt", name="explain_agent", arguments={"topic": "tools"}
    )
    assert "Explain tools" in json.dumps(prompt)
    assert (await call(e, "McpAuth", {"server": "example"}))["connected"]
    await e.mcp.disconnect("example")
    assert (await e.mcp.discover("example", "read-only"))["list_tools"]["tools"]


@pytest.mark.asyncio
@pytest.mark.parametrize("transport,path", [("streamable-http", "/mcp"), ("sse", "/sse")])
async def test_mcp_http_transports_and_permissions(runtime, transport, path):
    e = runtime
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server = asyncio.create_task(
        process(
            [
                sys.executable,
                str(ROOT / "examples/mcp_server.py"),
                "--transport",
                transport,
                "--port",
                str(port),
            ],
            e.store.current.workspace,
            timeout=30,
        )
    )
    try:
        for _ in range(100):
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                await asyncio.sleep(0.05)
        await e.mcp.configure(
            {
                "name": "remote",
                "transport": transport,
                "url": f"http://127.0.0.1:{port}{path}",
                "enabled": True,
            }
        )
        with pytest.raises(PermissionError):
            await e.mcp.call("remote", "read-only", "list_tools")
        result = await e.mcp.call(
            "remote", "danger-full-access", "call_tool", name="add", arguments={"a": 2, "b": 5}
        )
        assert "7" in json.dumps(result) and not result.get("isError")
        await e.mcp.disconnect("remote")
    finally:
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)


@pytest.mark.asyncio
async def test_web_boundaries_search_filter_and_methods(runtime, monkeypatch):
    e = runtime
    seen = []

    async def dns(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", dns)

    def handle(request):
        seen.append(request)
        if request.url.host == "www.google.com":
            return httpx.Response(
                200,
                text='<a href="/url?q=https%3A%2F%2Fdocs.example.org%2Fguide">Guide</a><a href="https://other.example.com">Other</a>',
            )
        return httpx.Response(
            200,
            text="<h1>Useful content</h1><script>hidden()</script>",
            headers={"content-type": "text/html"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        e.client = client
        result = await call(
            e, "WebSearch", {"query": "agent tools", "allowed_domains": ["example.org"]}
        )
        assert len(result["results"]) == 1 and result["results"][0]["title"] == "Guide"
        result = await call(e, "WebFetch", {"url": "https://example.org/doc", "prompt": "read"})
        assert "Useful content" in result["content"] and "hidden" not in result["content"]
        await call(
            e,
            "RemoteTrigger",
            {"url": "https://example.org/hook", "method": "POST", "body": "data"},
        )
        assert seen[-1].method == "POST" and seen[-1].content == b"data"
        assert seen[-1].url.host == "93.184.216.34" and seen[-1].headers["Host"] == "example.org"

    async def private_dns(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", private_dns)
    with pytest.raises(ValueError, match="公网"):
        await call(e, "WebFetch", {"url": "http://localhost", "prompt": "read"})


@pytest.mark.asyncio
async def test_scheduler_fires_due_job(runtime):
    e = runtime
    item = e.jobs.cron("* * * * *", "scheduled", "read-only")
    key = item.pop("id")
    item["next_run"] = 0
    e.jobs.put("cron", key, item)
    e.jobs.scheduler = asyncio.create_task(e.jobs.tick())
    await asyncio.sleep(0.05)
    saved = e.jobs.record("cron", key)
    assert saved["next_run"] > 0 and saved["last_job"]
    assert (await e.jobs.wait(saved["last_job"]))["status"] == "completed"


@pytest.mark.asyncio
async def test_plugin_lifecycle_hook_denial_and_tamper(runtime):
    e = runtime
    item = e.plugins.install(ROOT / "examples/plugins/learning")
    assert not item["enabled"]
    await e.plugins.enable("learning", True)
    assert "learning" in e.plugins.started
    assert (await call(e, "plugin__learning__word_count", {"text": "two words"}))["words"] == 2
    with pytest.raises(PermissionError):
        await call(e, "plugin__learning__word_count", {"text": "x"}, "read-only")
    with pytest.raises(PermissionError, match="protected"):
        await call(e, "write_file", {"path": "no.protected", "content": "no"})
    assert not (Path(e.store.current.workspace) / "no.protected").exists()
    await call(e, "write_file", {"path": "yes.txt", "content": "yes"})
    assert "Hello" in json.dumps(
        await Commands(e).execute("/learning:hello world", None, "danger-full-access")
    )
    await e.plugins.enable("learning", False)
    assert "learning" not in e.plugins.started
    (e.plugins.home / "learning/plugin.py").write_text("print('modified')")
    with pytest.raises(ValueError, match="修改"):
        await e.plugins.enable("learning", True)


@pytest.mark.asyncio
async def test_background_agent_and_team_results(runtime):
    e = runtime
    job = await call(e, "TaskCreate", {"prompt": "one", "description": "test"})
    result = await e.jobs.wait(job["id"])
    assert result["status"] == "completed"
    assert result["result"]["answer"] == "verified: one"
    assert (await call(e, "TaskOutput", {"task_id": job["id"]}))["session_id"]
    follow = await call(e, "TaskUpdate", {"task_id": job["id"], "message": "two"})
    assert (await e.jobs.wait(follow["id"]))["result"]["answer"] == "verified: two"
    team = await call(
        e, "TeamCreate", {"name": "team", "tasks": [{"prompt": "a"}, {"prompt": "b"}]}
    )
    results = await asyncio.gather(*(e.jobs.wait(k) for k in team["tasks"]))
    assert all(r["status"] == "completed" for r in results)
    await call(e, "TeamDelete", {"team_id": team["id"]})
    assert not e.jobs.records("team")
    sync = await call(e, "Agent", {"description": "sync", "prompt": "sync"})
    assert sync["result"]["answer"] == "verified: sync"
    packet = await call(
        e,
        "RunTaskPacket",
        {
            "objective": "test",
            "scope": "workspace",
            "repo": e.store.current.workspace,
            "branch_policy": "current",
            "acceptance_tests": ["read"],
            "commit_policy": "none",
            "reporting_contract": "summary",
            "escalation_policy": "ask",
        },
    )
    assert (await e.jobs.wait(packet["id"]))["status"] == "completed"


@pytest.mark.asyncio
async def test_child_depth_and_early_cancellation(runtime):
    e = runtime
    token = current.set(RunContext(depth=2, permission="danger-full-access"))
    try:
        with pytest.raises(ValueError, match="深度"):
            await call(e, "TaskCreate", {"prompt": "no"})
    finally:
        current.reset(token)
    job = e.jobs.agent_job("cancel", "read-only", e.store.current)
    assert (await e.jobs.stop(job["id"]))["status"] == "cancelled"
    assert not e.jobs.agent.active


@pytest.mark.asyncio
async def test_worker_actual_lifecycle(runtime):
    e = runtime
    worker = await call(e, "WorkerCreate", {"cwd": e.store.current.workspace})
    args = {"worker_id": worker["id"]}
    with pytest.raises(ValueError):
        await call(e, "WorkerSendPrompt", {**args, "prompt": "no"})
    assert not (await call(e, "WorkerAwaitReady", args))["ready"]
    await call(
        e, "WorkerObserve", {**args, "screen_text": "Do you trust the files in this folder?"}
    )
    await call(e, "WorkerResolveTrust", args)
    await call(e, "WorkerObserve", {**args, "screen_text": "Ready for input"})
    assert (await call(e, "WorkerAwaitReady", args))["status"] == "ready_for_prompt"
    await call(e, "WorkerObserve", {**args, "screen_text": "local observation"})
    worker = await call(e, "WorkerSendPrompt", {**args, "prompt": "hello"})
    await e.jobs.wait(worker["job_id"])
    assert (await call(e, "WorkerGet", args))["status"] == "finished"
    await call(e, "WorkerObserveCompletion", {**args, "finish_reason": "stop", "tokens_output": 3})
    await call(e, "WorkerRestart", args)
    assert (await call(e, "WorkerTerminate", args))["status"] == "finished"


@pytest.mark.asyncio
async def test_cron_persistence_and_validation(runtime):
    e = runtime
    with pytest.raises(ValueError):
        await call(e, "CronCreate", {"schedule": "bad", "prompt": "no"})
    job = await call(e, "CronCreate", {"schedule": "0 9 * * 1-5", "prompt": "check"})
    assert (await call(e, "CronList"))["schedules"][0]["id"] == job["id"]
    assert job["next_run"] > 0
    await call(e, "CronDelete", {"cron_id": job["id"]})
    assert not e.jobs.records("cron")


@pytest.mark.asyncio
async def test_questions_todos_skill_and_structured_output(runtime):
    e = runtime
    task = asyncio.create_task(
        call(e, "AskUserQuestion", {"question": "choose", "options": ["A", "B"]})
    )
    await asyncio.sleep(0.02)
    question = next(iter(e.questions.values()))
    question["future"].set_result("A")
    assert (await task)["answer"] == "A"
    assert not e.questions
    await call(
        e, "TodoWrite", {"todos": [{"content": "a", "activeForm": "working", "status": "pending"}]}
    )
    assert e.state("todos")["todos"][0]["content"] == "a"
    assert (await call(e, "StructuredOutput", {"done": True}))["accepted"]
    await call(e, "SendUserMessage", {"message": "done", "status": "normal"})
    assert e.jobs.records("notification")
    await call(e, "Sleep", {"duration_ms": 1})
    assert (await call(e, "TestingPermission", {"action": "probe"}))["allowed"]
    assert (await call(e, "ToolSearch", {"query": "GitStatus"}))["tools"]
    with pytest.raises(PermissionError):
        await call(e, "Config", {"setting": "api_key", "value": "no"})
    assert (await call(e, "Config", {"setting": "max_turns", "value": 3}))["value"] == 3
    skill = Path(e.store.current.workspace) / "skills/test/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("Read and verify.")
    assert (await call(e, "Skill", {"skill": "test"}))["instructions"] == "Read and verify."


@pytest.mark.asyncio
async def test_real_lsp_and_git_queries(runtime):
    e = runtime
    ws = e.store.current.workspace
    (Path(ws) / "sample.py").write_text(
        'def greet(name: str):\n    """Say hello."""\n    return "hello " + name\n\ngreet("Ada")\n'
    )
    symbols = await call(e, "LSP", {"action": "symbols", "path": "sample.py"})
    assert "greet" in json.dumps(symbols)
    definition = await call(
        e, "LSP", {"action": "definition", "path": "sample.py", "line": 4, "character": 2}
    )
    assert definition["result"]
    for action in ("hover", "references", "diagnostics"):
        assert "result" in await call(
            e, "LSP", {"action": action, "path": "sample.py", "line": 4, "character": 2}
        )
    assert (await process(["git", "init"], ws))["exit_code"] == 0
    await process(["git", "add", "sample.py"], ws)
    await process(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "test",
        ],
        ws,
    )
    for name, args in [
        ("GitStatus", {}),
        ("GitDiff", {}),
        ("GitLog", {}),
        ("GitShow", {"commit": "HEAD"}),
        ("GitBlame", {"path": "sample.py"}),
    ]:
        assert (await call(e, name, args))["exit_code"] == 0
    with pytest.raises(ValueError):
        await call(e, "GitShow", {"commit": "--output=evil"})


@pytest.mark.asyncio
async def test_commands_session_workflows(runtime):
    e = runtime
    commands = Commands(e)
    session = e.db.create_session("test")
    key = session["id"]
    e.db.save_messages(
        key, [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    )
    for command in (
        "help",
        "status",
        "sandbox",
        "cost",
        "config",
        "version",
        "providers",
        "doctor",
        "stats",
        "history",
        "tokens",
        "cache",
        "api-key",
        "context",
        "allowed-tools",
    ):
        assert (await commands.execute("/" + command, key, "read-only"))["kind"] in {
            "result",
            "navigate",
        }
    branch = await commands.execute("/branch fork", key, "read-only")
    assert e.db.session(branch["session_id"])["messages"]
    assert (await commands.execute("/export", key, "read-only"))["text"]
    await commands.execute("/rename new", key, "read-only")
    assert e.db.session(key)["title"] == "new"
    await commands.execute("/pin important", key, "read-only")
    assert e.jobs.records("bookmark")
    await commands.execute("/unpin", key, "read-only")
    await commands.execute("/clear", key, "read-only")
    assert not e.db.session(key)["messages"]
    assert (await commands.execute("/plan investigate", key, "danger-full-access"))[
        "permission"
    ] == "read-only"
    assert (await commands.execute("/vim", key, "read-only"))["result"][
        "status"
    ] == "upstream-placeholder"
