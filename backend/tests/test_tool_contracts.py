import json
import sys
from pathlib import Path

import httpx
import pytest
from app.config import ConfigStore
from app.db import Database
from app.extensions import Extensions
from app.tools import Tools

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from verify_tool_contracts import prepare, project  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures/tools_upstream.json"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"], ids=lambda c: c["name"]
)
async def test_frozen_rust_tool_contracts(case, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    prepare(workspace, case)
    monkeypatch.setenv("CLAW_CONFIG_HOME", str(tmp_path / "config"))
    store = ConfigStore(tmp_path / "data", tmp_path)
    store.update({"workspace": str(workspace), "rag_enabled": False})
    db = Database(tmp_path / "data/app.sqlite")
    async with httpx.AsyncClient(trust_env=False) as client:
        ext = Extensions(db, store, client)
        Tools(None, ext)
        try:
            for index, (step, expected) in enumerate(
                zip(case["steps"], case["expected"], strict=True)
            ):
                adaptation = case.get("adaptations", {}).get(str(index))
                if adaptation:
                    expected = adaptation["expected"]
                try:
                    value = await ext.execute(
                        store.current, "danger-full-access", step["tool"], step["args"]
                    )
                    actual = project(step["tool"], {"value": value}, workspace)
                except (ValueError, OSError) as error:
                    actual = {"error": True}
                    if not expected.get("error"):
                        raise AssertionError(f"Unexpected failure in {step['tool']}") from error
                if step["tool"] == "REPL" and "value" in actual:
                    actual["value"]["stdout"] = actual["value"]["stdout"].replace("\r\n", "\n")
                assert actual == expected
        finally:
            await ext.close()
