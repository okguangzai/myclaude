"""Frozen Rust traces plus Python persistent runtime concurrency regressions."""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from app import worker

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from verify_worker_parity import python_trace  # noqa: E402

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/worker_upstream.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda c: c["name"])
def test_rust_worker_lifecycle_contract(case, tmp_path):
    cwd = tmp_path / "expected"
    cwd.mkdir()
    assert python_trace(case, cwd) == case["expected"]


def test_worker_state_file_and_blocked_observer_directory(tmp_path):
    item = worker.create("one", str(tmp_path))
    path = tmp_path / ".claw/worker-state.json"
    worker.observe(item, "Ready for input")
    state = json.loads(path.read_text(encoding="utf-8"))
    assert state["worker_id"] == "one" and state["is_ready"]
    assert state["last_event"]["seq"] == 2
    assert state["last_event"]["timestamp"] == state["updated_at"]
    worker.send_prompt(item, "check")
    assert json.loads(path.read_text())["prompt_in_flight"]
    worker.terminate(item)
    assert json.loads(path.read_text())["status"] == "finished"
    # An unwritable observer surface must not stop execution.
    other = tmp_path / "blocked"
    other.mkdir()
    (other / ".claw").write_text("not a directory")
    assert worker.create("two", str(other))["status"] == "spawning"


@pytest.mark.asyncio
async def test_worker_concurrent_delivery_and_restart(tmp_path, monkeypatch):
    import httpx
    from app.agent import Agent
    from app.config import ConfigStore
    from app.db import Database
    from app.extensions import Extensions
    from app.tools import Tools

    (tmp_path / "workspace").mkdir()
    store = ConfigStore(tmp_path / "data", tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    provider_started = asyncio.Event()
    calls = []

    class Provider:
        async def stream(self, *args):
            provider_started.set()
            await asyncio.Event().wait()
            yield {}

    async def preflight(*args):
        calls.append(args)
        entered.set()
        await release.wait()
        return []

    monkeypatch.setattr(worker, "preflight", preflight)
    async with httpx.AsyncClient(trust_env=False) as client:
        ext = Extensions(Database(store.data / "app.sqlite"), store, client)
        ext.jobs.agent = Agent(ext.db, Provider(), Tools(None, ext), store)
        cfg = store.current

        async def call(name, **args):
            return await ext.execute(cfg, "danger-full-access", name, args)

        try:
            item = await call("WorkerCreate", cwd=cfg.workspace)
            key = item["id"]
            await call("WorkerObserve", worker_id=key, screen_text="Ready for input")
            first = asyncio.create_task(call("WorkerSendPrompt", worker_id=key, prompt="first"))
            await asyncio.wait_for(entered.wait(), 5)
            second = asyncio.create_task(call("WorkerSendPrompt", worker_id=key, prompt="second"))
            release.set()
            active = await asyncio.wait_for(first, 5)
            with pytest.raises(ValueError):
                await second
            assert len(calls) == 1
            await asyncio.wait_for(provider_started.wait(), 5)
            with pytest.raises(ValueError, match="不能伪造"):
                await call(
                    "WorkerObserveCompletion", worker_id=key, finish_reason="stop", tokens_output=5
                )
            restarted = await call("WorkerRestart", worker_id=key)
            assert restarted["status"] == "spawning" and restarted["job_id"] is None
            assert ext.jobs.get(active["job_id"])["status"] == "cancelled"
            assert not ext.jobs.agent.active
            # A failed dispatch must persist a failure, never a phantom running job.
            await call("WorkerObserve", worker_id=key, screen_text="Ready for input")

            def unavailable(*args, **kwargs):
                raise ValueError("executor unavailable")

            monkeypatch.setattr(ext.jobs, "agent_job", unavailable)
            with pytest.raises(ValueError, match="unavailable"):
                await call("WorkerSendPrompt", worker_id=key, prompt="retry")
            failed = await call("WorkerGet", worker_id=key)
            assert failed["status"] == "failed" and failed["job_id"] is None
            assert failed["last_error"]["kind"] == "protocol"
        finally:
            await ext.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason,tokens,expected",
    [
        ("unknown", 0, "failed"),
        ("unknown", 3, "finished"),
        ("max_tokens", 3, "finished"),
        ("error", 3, "failed"),
    ],
)
async def test_worker_uses_actual_provider_completion(tmp_path, reason, tokens, expected):
    import httpx
    from app.agent import Agent
    from app.config import ConfigStore
    from app.db import Database
    from app.extensions import Extensions
    from app.tools import Tools

    (tmp_path / "workspace").mkdir()
    store = ConfigStore(tmp_path / "data", tmp_path)

    class Provider:
        async def stream(self, *args):
            yield {
                "type": "complete",
                "message": {"role": "assistant", "content": "result" if tokens else ""},
                "usage": {"output_tokens": tokens},
                "stop": reason,
            }

    async with httpx.AsyncClient(trust_env=False) as client:
        ext = Extensions(Database(store.data / "app.sqlite"), store, client)
        ext.jobs.agent = Agent(ext.db, Provider(), Tools(None, ext), store)
        try:

            async def call(name, **arguments):
                return await ext.execute(store.current, "danger-full-access", name, arguments)

            created = await call("WorkerCreate", cwd=store.current.workspace)
            key = created["worker_id"]
            await call("WorkerObserve", worker_id=key, screen_text="Ready for input")
            started = await call("WorkerSendPrompt", worker_id=key, prompt="check")
            job = await ext.jobs.wait(started["job_id"], timeout=10)
            assert job["result"]["finish_reason"] == reason
            assert (await call("WorkerGet", worker_id=key))["status"] == expected
        finally:
            await ext.close()
