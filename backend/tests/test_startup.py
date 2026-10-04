"""Startup contracts must work without cloud credentials or an HTTP service."""

import json

import pytest
from app.cli import main
from app.config import Settings
from app.prompt import assemble, discover_instructions


def test_instruction_hierarchy_frameworks_and_budget(tmp_path):
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "src" / "feature"
    nested.mkdir(parents=True)
    (tmp_path / "AGENTS.md").write_text("root rule\n\n\n shared")
    (nested / "CLAUDE.md").write_text("root rule\n\n shared")
    (nested / ".cursorrules").write_text("cursor only")
    (nested / ".github").mkdir()
    (nested / ".github/copilot-instructions.md").write_text("copilot only")
    basic = discover_instructions(nested, "none")
    assert len(basic) == 1 and basic[0]["path"] == str(tmp_path / "AGENTS.md")
    chosen = discover_instructions(nested, ["cursor"])
    assert len(chosen) == 2 and chosen[-1]["content"] == "cursor only"
    assert len(discover_instructions(nested)) == 3
    rules = nested / ".claw/rules"
    rules.mkdir(parents=True)
    for n in range(6):
        (rules / f"{n}.md").write_text(str(n) * 6000)
    bounded = discover_instructions(nested)
    assert sum(len(item["content"]) for item in bounded) == 12000
    assert all(len(item["content"]) <= 4000 for item in bounded)
    assert any(item["truncated"] for item in bounded)


@pytest.mark.asyncio
async def test_prompt_shared_context_and_date(tmp_path):
    (tmp_path / "AGENTS.md").write_text("Use the existing test runner.")
    cfg = Settings(workspace=str(tmp_path), system_prompt="assistant")
    result = await assemble(cfg, "read-only", "2026-10-04")
    assert "Use the existing test runner." in result["prompt"]
    assert result["context"]["instruction_files"][0]["chars"] == 29
    assert result["context"]["date"] == "2026-10-04"
    with pytest.raises(ValueError):
        await assemble(cfg, "read-only", "yesterday")


def test_bootstrap_fast_paths_do_not_open_bad_runtime_config(tmp_path, capsys):
    data = tmp_path / "data"
    data.mkdir()
    (data / "settings.json").write_text("broken JSON")
    common = ["--data-dir", str(data), "--output-format", "json"]
    assert main(["bootstrap-plan", *common]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["total_phases"] == 12
    assert [p["order"] for p in value["phases"]] == list(range(12))
    assert main(["--dump-manifests", "--manifests-dir", str(tmp_path), *common]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["workspace"] == str(tmp_path) and value["tools"] == 55
    assert main(["acp", "serve", *common]) == 2
    assert json.loads(capsys.readouterr().out)["protocol"]["daemon"] is False
    assert main(["acp", "unexpected", *common]) == 2
    assert json.loads(capsys.readouterr().out)["kind"] == "unsupported_acp_invocation"
    assert not (data / "app.sqlite").exists()
    assert main(["dump-manifests", "--manifests-dir", str(tmp_path / "missing"), *common]) == 1
    assert "missing_manifests" in json.loads(capsys.readouterr().out)["message"]
