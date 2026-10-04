import io
import json

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
