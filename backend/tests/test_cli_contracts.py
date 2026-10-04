import io
import json
from pathlib import Path

import pytest
from app import cli
from app.db import Database


def test_cli_prompt_subcommand_and_piped_input(tmp_path, monkeypatch):
    (tmp_path / "workspace").mkdir()
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("piped context"))
    seen = []

    async def capture(ext, agent, session_id, prompt, args):
        seen.append((prompt, args.compact, args.output_format))
        return 0

    monkeypatch.setattr(cli, "interact", capture)
    assert (
        cli.main(
            [
                "--data-dir",
                str(tmp_path / "data"),
                "--output-format",
                "TEXT",
                "--compact",
                "prompt",
                "instruction",
                "--stdin",
            ]
        )
        == 0
    )
    assert seen == [("instruction\npiped context", True, "text")]


def test_cli_output_env_and_override(monkeypatch, capsys):
    monkeypatch.setenv("CLAW_OUTPUT_FORMAT", "JSON")
    assert cli.main(["--version"]) == 0
    assert json.loads(capsys.readouterr().out)["kind"] == "version"
    assert cli.parser().parse_args(["--output-format", "TEXT"]).output_format == "text"
    monkeypatch.setenv("CLAW_OUTPUT_FORMAT", "invalid")
    with pytest.raises(SystemExit):
        cli.main(["--version"])


def test_cli_resume_without_prompt_is_local(tmp_path, monkeypatch, capsys):
    (tmp_path / "workspace").mkdir()
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    db = Database(tmp_path / "data/app.sqlite")
    session = db.create_session("saved session")
    db.save_messages(session["id"], [{"role": "user", "content": "saved request"}])
    assert cli.main(["--data-dir", str(tmp_path / "data"), "--resume", "latest", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["kind"] == "resume"
    assert result["result"]["id"] == session["id"]


@pytest.mark.parametrize(
    "arguments,kind",
    [
        (["--unknown-option"], "unknown_option"),
        (["--model"], "missing_flag_value"),
        (["--permission", "invalid"], "invalid_flag_value"),
    ],
)
def test_cli_machine_readable_argument_errors(arguments, kind, capsys):
    with pytest.raises(SystemExit) as raised:
        cli.main(["--output-format", "json", *arguments])
    assert raised.value.code == 1
    value = json.loads(capsys.readouterr().out)
    assert value["error_kind"] == kind
    assert value["action"] == "abort" and value["exit_code"] == 1


CLI_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/cli_errors_upstream.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", CLI_FIXTURE["cases"], ids=lambda c: " ".join(c["arguments"]))
def test_cli_original_error_contract(case, capsys):
    with pytest.raises(SystemExit) as raised:
        cli.main(["--output-format", "json", *case["arguments"]])
    value = json.loads(capsys.readouterr().out)
    result = {
        "exit_code": raised.value.code,
        "status": value["status"],
        "error_kind": value["error_kind"],
        "has_hint": bool(value["hint"]),
    }
    expected = case["expected"]
    if case["arguments"] == ["--unknown-option"]:
        # Original treats an unknown leading option as a prompt, then needs credentials.
        # Python deliberately rejects it before any possible model invocation.
        expected = {**expected, "error_kind": "unknown_option"}
    assert result == expected


def test_cli_literal_prompt_repeated_tools_and_no_abbreviation(tmp_path, monkeypatch):
    (tmp_path / "workspace").mkdir()
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    seen = []

    async def capture(ext, agent, session_id, prompt, args):
        seen.append((prompt, ext.store.current.allowed_tools))
        return 0

    monkeypatch.setattr(cli, "interact", capture)
    assert (
        cli.main(
            [
                "--data-dir",
                str(tmp_path / "data"),
                "-p",
                "--",
                "--literal prompt",
                "--allowedTools",
                "read_file",
                "--allowed-tools",
                "search_files,read_file",
            ]
        )
        == 0
    )
    assert seen == [("--literal prompt", ["read_file", "search_files"])]
    with pytest.raises(SystemExit):
        cli.main(["--vers"])
