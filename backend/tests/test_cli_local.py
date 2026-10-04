import json
import subprocess
import sys
from pathlib import Path

import pytest
from app import cli
from app.cli_local import HELP, HELP_ALIASES, diff_value, initialize
from app.config import ConfigStore

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from verify_cli_local import prepare, project  # noqa: E402

FIXTURE_PATH = Path(__file__).parent / "fixtures/cli_local_upstream.json"


@pytest.mark.parametrize(
    "case",
    json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["cases"],
    ids=lambda c: c["scenario"] + "-" + " ".join(c["arguments"]),
)
def test_original_local_cli_contract(case, tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    prepare(workspace, case["scenario"])
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setenv("CLAW_CONFIG_HOME", str(tmp_path / "config"))
    for expected in case["expected"]:
        code = cli.main(
            [
                "--output-format",
                "json",
                "--cwd",
                str(workspace),
                "--data-dir",
                str(tmp_path / "data"),
                *case["arguments"],
            ]
        )
        value = json.loads(capsys.readouterr().out)
        kind = "models" if case["arguments"][0] == "model" else case["arguments"][0]
        assert {"exit_code": code, "value": project(value, kind)} == expected
    if case["scenario"] == "existing":
        assert (workspace / "CLAUDE.md").read_text() == "user guidance\n"
        assert json.loads((workspace / ".claw/settings.json").read_text()) == {
            "model": "custom-model"
        }


@pytest.mark.parametrize("topic", [*HELP, *HELP_ALIASES])
def test_help_is_scoped_and_never_loads_runtime(topic, tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("help must not load runtime")

    monkeypatch.setattr(cli, "ConfigStore", forbidden)
    for words in ([topic, "--help"], ["help", topic], ["--help", topic]):
        assert (
            cli.main(["--output-format", "json", "--cwd", str(tmp_path / "missing"), *words]) == 0
        )
        value = json.loads(capsys.readouterr().out)
        assert value["topic"] == HELP_ALIASES.get(topic, topic)
        assert not value["requires_provider_request"] and not value["mutates_workspace"]
        assert "myclaude " + value["command"] in value["usage"]
    assert not list(tmp_path.iterdir())


def test_config_alias_masking_models_do_not_mutate_and_invalid_definitions(
    tmp_path, monkeypatch, capsys
):
    workspace = tmp_path / "workspace"
    prepare(workspace, "definitions")
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setenv("CLAW_CONFIG_HOME", str(tmp_path / "config"))
    store = ConfigStore(tmp_path / "data", tmp_path)
    store.update(
        {
            "api_key": "fixture-private-key",
            "runtime_environment": {"PRIVATE": "fixture-runtime-secret"},
            "model": "sonnet",
        }
    )
    settings = (tmp_path / "data/settings.json").read_bytes()
    base = [
        "--output-format",
        "json",
        "--data-dir",
        str(tmp_path / "data"),
        "--cwd",
        str(workspace),
    ]
    assert cli.main([*base, "settings"]) == 0
    output = capsys.readouterr().out
    assert "fixture-private-key" not in output and "fixture-runtime-secret" not in output
    first = json.loads(output)
    assert cli.main([*base, "config", "settings"]) == 0
    assert json.loads(capsys.readouterr().out) == first
    assert cli.main([*base, "agents", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["count"] == 2 and listed["invalid_count"] == 1
    (workspace / ".claw/agents/broken.toml").write_text("invalid = [", encoding="utf-8")
    assert cli.main([*base, "agents", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["count"] == 2 and listed["invalid_count"] == 2
    # Deliberate extension: bare model is a read-only alias, not a slash-command error.
    assert cli.main([*base, "models"]) == 0
    models = json.loads(capsys.readouterr().out)
    assert cli.main([*base, "model"]) == 0
    assert json.loads(capsys.readouterr().out) == models
    with pytest.raises(SystemExit):
        cli.main([*base, "models", "typo"])
    assert json.loads(capsys.readouterr().out)["error_kind"] == "unsupported_models_action"
    assert (tmp_path / "data/settings.json").read_bytes() == settings


def test_init_rejects_conflicting_paths_without_overwriting(tmp_path):
    (tmp_path / "CLAUDE.md").mkdir()
    with pytest.raises(ValueError, match="类型冲突"):
        initialize(tmp_path)
    assert not (tmp_path / ".claw").exists()


@pytest.mark.asyncio
async def test_diff_disables_repository_external_commands(tmp_path):
    prepare(tmp_path, "both")
    for key in ("diff.external", "diff.fixture.textconv"):
        subprocess.run(
            ["git", "config", key, "myclaude-fixture-must-never-run"], cwd=tmp_path, check=True
        )
    (tmp_path / ".gitattributes").write_text("*.txt diff=fixture\n", encoding="utf-8")
    report = await diff_value(tmp_path)
    assert report["changed_file_count"] == 1
    assert "staged" in report["staged"] and "unstaged" in report["unstaged"]
