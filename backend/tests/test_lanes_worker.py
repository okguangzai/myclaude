import asyncio
import subprocess

import pytest
from app import worker
from app.config import ConfigStore
from app.db import Database
from app.jobs import Jobs
from app.lanes import board, build, dedupe_commits, freshness, reconcile
from app.trust import pattern_matches, resolve


def test_terminal_reconciliation_and_provenance():
    started = build("lane.started", "running", 1)
    done = build("lane.finished", "completed", 2, {"answer": "ok"})
    duplicate = build("lane.finished", "completed", 3, {"answer": "ok"})
    assert reconcile([duplicate, done, started])["terminal"]["metadata"]["seq"] == 2
    uncertain = build("lane.reconciled", "reconciled", 4, provenance="transport")
    result = reconcile([done, started, uncertain])
    assert result["terminal"]["status"] == "reconciled"
    assert done["status"] == "completed"  # projection never mutates source evidence
    commits = [
        build("lane.commit.created", "completed", n, d)
        for n, d in enumerate(
            [{"commit": "a"}, {"commit": "a"}, {"commit": "b", "supersededBy": "c"}]
        )
    ]
    assert len(dedupe_commits(commits)) == 1


@pytest.mark.asyncio
async def test_job_heartbeat_is_real_and_board_keeps_terminal_outcome(tmp_path):
    (tmp_path / "workspace").mkdir()
    store = ConfigStore(tmp_path / "data", tmp_path)
    jobs = Jobs(Database(store.data / "app.sqlite"), store)
    started, release = asyncio.Event(), asyncio.Event()

    async def work():
        started.set()
        await release.wait()
        return {"answer": "done"}

    task = jobs.submit("test", {}, work)
    await started.wait()
    current = jobs.get(task["id"])
    assert current["heartbeat"]["status"] == "running"
    assert jobs.lane_board()["active"][0]["freshness"] == "healthy"
    release.set()
    await jobs.wait(task["id"], 5)
    assert jobs.lane_board()["finished"][0]["status"] == "completed"
    heartbeat = {"observed_at": 100, "transport_alive": True, "status": "running"}
    assert freshness(heartbeat, 161, 60) == "stalled"
    assert freshness(heartbeat, 160, 60) == "healthy"
    heartbeat["transport_alive"] = False
    assert freshness(heartbeat, 100, 60) == "transport_dead"
    assert board([{"task_id": "x", "status": "blocked"}], 1)["blocked"][0]["freshness"] == "unknown"


@pytest.mark.asyncio
async def test_worker_preflight_and_timeout_evidence(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src/known.py").write_text("x=1")
    subprocess.run(["git", "add", "src/known.py"], cwd=tmp_path, check=True)
    warnings = await worker.preflight(
        str(tmp_path),
        "Read src/known.py and `src/missing.py`; do not read ../secret.txt or https://x/y.",
    )
    assert [w["path"] for w in warnings] == ["src/missing.py"]
    item = worker.create("test", str(tmp_path))
    worker.observe(
        item, 'Allow the files MCP server tool "mcp__files__read" to run?\nAllow for this session'
    )
    worker.startup_timeout(item, "claw", True, True)
    payload = item["events"][-1]["payload"]
    assert payload["classification"] == "tool_permission_required"
    assert payload["evidence"]["tool_permission_allow_scope"] == "session_only"
    assert payload["evidence"]["transport_health"]["summary"] == "caller_observation"
    item = worker.create("another", str(tmp_path))
    worker.observe(item, "Ready for prompt")
    worker.send_prompt(item, "check tests")
    worker.startup_timeout(item, "claw", True, True)
    assert item["events"][-1]["payload"]["classification"] == "prompt_acceptance_timeout"


def test_trust_denial_wins_even_when_allowlisted_or_manual(tmp_path):
    path = str(tmp_path / "repo")
    decision = resolve(path, "trust this folder; yes, i trust", [str(tmp_path)], [path])
    assert decision["policy"] == "deny" and decision["events"][-1]["type"] == "trust_denied"
    assert (
        resolve(path, "trust this folder; yes, i trust")["events"][-1]["resolution"]
        == "manual_approval"
    )
    assert pattern_matches("/tmp/*/repo-?", "/tmp/x/repo-a")
    assert not pattern_matches("/tmp/repos", "/tmp/repos-other")
