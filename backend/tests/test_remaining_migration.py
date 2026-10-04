import asyncio
import json
import zlib
from pathlib import Path

import httpx
import pytest
from app.agent import Agent
from app.commands import Commands
from app.config import ConfigStore
from app.config_layers import translate
from app.db import Database
from app.extensions import Extensions
from app.pdf_extract import extract_text_from_bytes, maybe_extract_pdf_from_prompt
from app.tools import Tools


@pytest.mark.asyncio
async def test_cron_execution_history_and_missing_delete(runtime, monkeypatch):
    ext = runtime
    created = await ext.execute(
        ext.store.current,
        "danger-full-access",
        "CronCreate",
        {"schedule": "* * * * *", "prompt": "check"},
    )
    calls = []

    def start(*args):
        calls.append(args)
        return {"id": "job-1"}

    monkeypatch.setattr(ext.jobs, "agent_job", start)
    ext.jobs.tick_once(created["next_run"])
    item = ext.jobs.record("cron", created["id"])
    assert item["run_count"] == 1 and item["last_run_at"] and len(calls) == 1
    ext.jobs.running["job-1"] = object()
    ext.jobs.tick_once(item["next_run"])
    assert ext.jobs.record("cron", created["id"])["run_count"] == 1
    ext.jobs.running.clear()
    listed = await ext.execute(ext.store.current, "read-only", "CronList", {})
    assert listed["count"] == 1 and listed["crons"][0]["cron_id"] == created["id"]
    await ext.execute(
        ext.store.current, "danger-full-access", "CronDelete", {"cron_id": created["id"]}
    )
    with pytest.raises(ValueError):
        await ext.execute(
            ext.store.current, "danger-full-access", "CronDelete", {"cron_id": created["id"]}
        )


@pytest.mark.asyncio
async def test_lsp_cached_diagnostics_are_workspace_scoped(runtime, monkeypatch):
    from app import lsp

    ext = runtime
    cfg = ext.store.current
    path = Path(cfg.workspace) / "sample.py"
    path.write_text("invalid Python")

    async def response(*args):
        return {"uri": path.as_uri(), "diagnostics": [{"message": "syntax error"}]}

    monkeypatch.setattr(lsp, "query", response)
    await ext.execute(cfg, "read-only", "LSP", {"action": "diagnostics", "path": "sample.py"})
    report = await ext.execute(cfg, "read-only", "LSP", {"action": "diagnostics"})
    assert report["source"] == "cached" and report["count"] == 1
    other = cfg.model_copy(update={"workspace": str(path.parent.parent)})
    assert (await ext.execute(other, "read-only", "LSP", {"action": "diagnostics"}))["count"] == 0


@pytest.mark.parametrize(
    "status,error,blocker,green,pushed,complete",
    [
        ("Finished", None, None, True, True, True),
        ("COMPLETED", None, None, True, True, True),
        ("Running", None, None, True, True, False),
        ("Finished", "", None, True, True, False),
        ("Finished", None, "", True, True, False),
        ("Finished", None, None, False, True, False),
        ("Finished", None, None, True, False, False),
    ],
)
def test_lane_completion_requires_all_upstream_evidence(
    status, error, blocker, green, pushed, complete
):
    from app.orchestration import detect_lane_completion, evaluate_completed_lane

    context = detect_lane_completion(
        {"agent_id": "lane", "status": status, "error": error, "current_blocker": blocker},
        green,
        pushed,
    )
    assert bool(context) is complete
    if context:
        assert {a["kind"] for a in evaluate_completed_lane(context)["actions"]} == {
            "closeout_lane",
            "cleanup_session",
        }


@pytest.mark.parametrize(
    "elapsed,classification", [(-5, "unknown"), (30, "unknown"), (31, "prompt_misdelivery")]
)
def test_worker_startup_clock_boundary(tmp_path, monkeypatch, elapsed, classification):
    from app import worker

    monkeypatch.setattr(worker.time, "time", lambda: 100)
    item = worker.create("clock", str(tmp_path))
    item.update(prompt_sent_at=100, prompt_in_flight=True)
    worker.startup_timeout(item, "claw", True, True, now=100 + elapsed)
    payload = item["events"][-1]["payload"]
    assert payload["classification"] == classification
    assert payload["evidence"]["elapsed_seconds"] == max(0, elapsed)


def test_worker_trust_path_boundary_and_unicode_preview(tmp_path):
    from app import worker

    trusted = tmp_path / "repo"
    sibling = tmp_path / "repo-other"
    trusted.mkdir()
    sibling.mkdir()
    assert worker.create("yes", str(trusted), [str(trusted)])["trust_auto_resolve"]
    assert not worker.create("no", str(sibling), [str(trusted)])["trust_auto_resolve"]
    assert worker.prompt_preview("学" * 49) == "学" * 48 + "…"


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    (tmp_path / "workspace").mkdir()
    monkeypatch.setenv("CLAW_CONFIG_HOME", str(tmp_path / "config"))
    store = ConfigStore(tmp_path / "data", tmp_path)
    db = Database(store.data / "app.sqlite")
    async with httpx.AsyncClient(trust_env=False) as client:
        ext = Extensions(db, store, client)
        Tools(None, ext)
        try:
            yield ext
        finally:
            await ext.close()


@pytest.mark.asyncio
async def test_session_delete_confirmation_backup_and_active_guard(runtime):
    ext = runtime
    old = ext.db.create_session("keep record")
    ext.db.save_messages(old["id"], [{"role": "user", "content": "must survive in backup"}])
    commands = Commands(ext)
    exists = await commands.execute("/session exists " + old["id"], None, "read-only")
    assert exists["result"]["exists"]
    response = await commands.execute("/session delete " + old["id"], None, "workspace-write")
    assert response["result"]["status"] == "confirmation_required"
    with pytest.raises(ValueError, match="活动会话"):
        await commands.execute(
            "/session delete " + old["id"] + " --force", old["id"], "workspace-write"
        )
    run = ext.db.start_run(old["id"], "test", "read-only")
    with pytest.raises(ValueError, match="正在运行"):
        ext.sessions.delete(old["id"])
    ext.db.finish_run(run, "completed", 1, {})
    response = await commands.execute(
        "/session delete " + old["id"] + " --force", None, "workspace-write"
    )
    assert not ext.db.session(old["id"])
    backup = Path(response["result"]["backup"]).read_text(encoding="utf-8")
    assert "must survive in backup" in backup
    assert ext.db.all("SELECT id FROM runs WHERE id=?", (run,))
    # Another process may hold a session snapshot loaded just before deletion.
    with pytest.raises(ValueError, match="已删除"):
        ext.db.start_run(old["id"], "test", "read-only")
    with pytest.raises(ValueError, match="已删除"):
        ext.jobs.submit("agent", {}, lambda: None, session_id=old["id"])


@pytest.mark.asyncio
async def test_slash_init_memory_teleport_and_debug_do_not_reexecute(runtime):
    ext, workspace = runtime, Path(runtime.store.current.workspace)
    commands = Commands(ext)
    await commands.execute("/init", None, "workspace-write")
    assert (workspace / "CLAUDE.md").exists()
    (workspace / "example.py").write_text("important_symbol = 1\n", encoding="utf-8")
    report = (await commands.execute("/teleport important_symbol", None, "read-only"))["result"]
    assert report["content"]["matches"][0]["path"] == "example.py"
    memory = (await commands.execute("/memory", None, "read-only"))["result"]
    assert any(file["path"].endswith("CLAUDE.md") for file in memory["files"])
    session = ext.db.create_session()
    ext.db.save_messages(
        session["id"],
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "old", "function": {"name": "write_file", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "tool_call_id": "old", "content": "previous result"},
        ],
    )
    report = (await commands.execute("/debug-tool-call", session["id"], "read-only"))["result"]
    assert not report["replayed"] and report["output"]["content"] == "previous result"


@pytest.mark.asyncio
async def test_approval_command_can_answer_running_session(runtime):
    from types import SimpleNamespace

    from app.cli import interact

    ext = runtime
    session = ext.db.create_session()
    ext.db.start_run(session["id"], "mock", "workspace-write")
    future = asyncio.get_running_loop().create_future()
    ext.questions["pending"] = {
        "future": future,
        "session_id": session["id"],
        "options": ["允许本次", "拒绝"],
    }
    response = await interact(
        ext,
        None,
        session["id"],
        "/approve",
        SimpleNamespace(permission="workspace-write", output_format="json"),
    )
    assert response == 0 and await future == "允许本次"


@pytest.mark.asyncio
async def test_config_preferences_survive_restart_and_subagent_model_import(runtime):
    ext, cfg = runtime, runtime.store.current
    for setting, value in [("autoCompactEnabled", "false"), ("editorMode", "vim")]:
        result = await ext.execute(
            cfg, "workspace-write", "Config", {"setting": setting, "value": value}
        )
        assert result["success"] and result["operation"] == "set"
    recovered = ConfigStore(ext.store.data, ext.store.root)
    assert recovered.current.upstream_preferences == {
        "autoCompactEnabled": False,
        "editorMode": "vim",
    }
    with pytest.raises(ValueError):
        await ext.execute(
            cfg, "workspace-write", "Config", {"setting": "editorMode", "value": "invalid"}
        )
    translated = translate(
        {"subagentModel": "test-agent", "autoCompactEnabled": False, "$schema": "fixture"}
    )
    assert not translated["unmapped"] and translated["changes"]["subagent_model"] == "test-agent"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,allowed,denied",
    [
        ("explorer", "read_file", "write_file"),
        ("Plan", "TodoWrite", "bash"),
        ("Verification", "bash", "write_file"),
        ("statusline", "write_file", "AskUserQuestion"),
    ],
)
async def test_subagent_types_restrict_actual_model_tools(runtime, kind, allowed, denied):
    ext, seen = runtime, []
    ext.store.update({"subagent_model": "child-model"})

    class Provider:
        async def stream(self, cfg, messages, tools, system):
            names = {spec["function"]["name"] for spec in tools}
            seen.append(names)
            assert allowed in names and denied not in names and "AskUserQuestion" not in names
            assert cfg.model == "child-model" and "background sub-agent" in system
            yield {
                "type": "complete",
                "message": {"role": "assistant", "content": "done"},
                "usage": {},
                "stop": "stop",
            }

    ext.jobs.agent = Agent(ext.db, Provider(), ext.tools, ext.store)
    result = await ext.execute(
        ext.store.current,
        "danger-full-access",
        "Agent",
        {
            "description": "verify subtype",
            "prompt": "check",
            "subagent_type": kind,
            "name": "named-agent",
        },
    )
    assert seen and result["status"] == "completed" and result["name"] == "named-agent"


@pytest.mark.asyncio
async def test_notebook_replacement_preserves_metadata_and_clears_stale_outputs(runtime):
    workspace = Path(runtime.store.current.workspace)
    notebook = {
        "metadata": {},
        "cells": [
            {
                "id": "cell-1",
                "cell_type": "code",
                "metadata": {"tags": ["keep"]},
                "outputs": [{"text": "stale"}],
                "execution_count": 4,
                "source": ["old"],
            }
        ],
    }
    (workspace / "book.ipynb").write_text(json.dumps(notebook))
    await runtime.execute(
        runtime.store.current,
        "workspace-write",
        "NotebookEdit",
        {"notebook_path": "book.ipynb", "new_source": "new"},
    )
    cell = json.loads((workspace / "book.ipynb").read_text())["cells"][0]
    assert (
        cell["metadata"] == {"tags": ["keep"]}
        and not cell["outputs"]
        and cell["execution_count"] is None
    )


@pytest.mark.asyncio
async def test_glob_braces_and_skill_legacy_metadata_name(runtime):
    workspace = Path(runtime.store.current.workspace)
    (workspace / "one.py").write_text("one")
    (workspace / "two.ts").write_text("two")
    result = await runtime.execute(
        runtime.store.current, "read-only", "glob_search", {"pattern": "*.{py,ts}"}
    )
    assert set(result["matches"]) == {"one.py", "two.ts"} and result["numFiles"] == 2
    legacy = workspace / ".claw/commands/file-name.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        "---\nname: Named Skill\ndescription: fixture\n---\nRead carefully", encoding="utf-8"
    )
    result = await runtime.execute(
        runtime.store.current, "read-only", "Skill", {"skill": "named-skill"}
    )
    assert "Read carefully" in result["prompt"]


@pytest.mark.parametrize("compressed", [False, True])
def test_pdf_text_helper_and_budget(compressed, tmp_path):
    content = b"BT\n(Hello \\(world\\)) Tj\n[(A) -10 (B)] TJ\n(next) '\nET"
    data = (
        b"<< /Length 123 "
        + (b"/Filter /FlateDecode" if compressed else b"")
        + b">>\nstream\n"
        + (zlib.compress(content) if compressed else content)
        + b"\nendstream"
    )
    assert extract_text_from_bytes(data) == "Hello (world) AB\nnext"
    with pytest.raises(ValueError, match="budget"):
        extract_text_from_bytes(data, 2)
    (tmp_path / "sample.pdf").write_bytes(data)
    assert (
        maybe_extract_pdf_from_prompt("read `sample.pdf`", tmp_path)[1] == "Hello (world) AB\nnext"
    )
    assert maybe_extract_pdf_from_prompt("read ../secret.pdf", tmp_path) is None
