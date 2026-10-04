"""Capture deterministic contracts from the frozen Rust tools crate, without cloud calls."""

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = r"""
use serde_json::{json, Value};
fn main() {
    let args: Vec<String> = std::env::args().collect();
    let cases: Vec<Value> = serde_json::from_slice(&std::fs::read(&args[1]).unwrap()).unwrap();
    let mut results = Vec::new();
    for case in cases {
        std::env::set_current_dir(case["root"].as_str().unwrap()).unwrap();
        let mut trace = Vec::new();
        for step in case["steps"].as_array().unwrap() {
            let result = tools::execute_tool(step["tool"].as_str().unwrap(), &step["args"]);
            trace.push(match result {
                Ok(value) => json!({"value": serde_json::from_str::<Value>(&value).unwrap()}),
                Err(error) => json!({"error": true, "detail": error}),
            });
        }
        results.push(trace);
    }
    std::fs::write(&args[2], serde_json::to_vec(&results).unwrap()).unwrap();
}
"""


def cases():
    result = []

    def add(name, steps, files=None, git=False):
        result.append(
            {
                "name": name,
                "steps": [{"tool": t, "args": a} for t, a in steps],
                "files": files or {},
                "git": git,
            }
        )

    for args in ({}, {"offset": 1, "limit": 1}, {"offset": 99}, {"limit": 0}):
        add(
            "read-" + str(len(result)),
            [("read_file", {"path": "one.txt", **args})],
            {"one.txt": "one\ntwo\nthree\n"},
        )
    add(
        "write-edit",
        [
            ("write_file", {"path": "nested/file.txt", "content": "one\ntwo\n"}),
            ("write_file", {"path": "nested/file.txt", "content": "one\ntwo\nthree\n"}),
            ("edit_file", {"path": "nested/file.txt", "old_string": "two", "new_string": "TWO"}),
            ("edit_file", {"path": "nested/file.txt", "old_string": "one", "new_string": "one"}),
        ],
    )
    add(
        "edit-repeated",
        [
            (
                "edit_file",
                {"path": "one.txt", "old_string": "a", "new_string": "b", "replace_all": True},
            )
        ],
        {"one.txt": "a\na\n"},
    )
    notebook = {
        "metadata": {"kernelspec": {"language": "python"}},
        "cells": [
            {
                "id": "first",
                "cell_type": "markdown",
                "metadata": {"custom": "keep"},
                "source": ["first"],
            },
            {
                "id": "second",
                "cell_type": "code",
                "metadata": {},
                "source": ["print(1)"],
                "outputs": [],
                "execution_count": None,
            },
        ],
    }
    for label, args in [
        ("last", {"new_source": "print(2)"}),
        ("markdown", {"cell_id": "first", "new_source": "updated"}),
        (
            "insert",
            {"edit_mode": "insert", "cell_id": "first", "new_source": "", "cell_type": "markdown"},
        ),
        ("delete", {"edit_mode": "delete"}),
        ("missing-id", {"cell_id": "missing", "new_source": "x"}),
        ("missing-source", {"cell_id": "first"}),
    ]:
        add(
            "notebook-" + label,
            [("NotebookEdit", {"notebook_path": "book.ipynb", **args})],
            {"book.ipynb": json.dumps(notebook)},
        )
    todos = [{"content": x, "activeForm": x, "status": "pending"} for x in ("one", "two", "three")]
    completed = [{**todo, "status": "completed"} for todo in todos]
    add(
        "todos",
        [
            ("TodoWrite", {"todos": todos}),
            ("TodoWrite", {"todos": completed}),
            ("TodoWrite", {"todos": todos}),
        ],
    )
    add("structured", [("StructuredOutput", {"answer": 42}), ("StructuredOutput", {})])
    add("sleep", [("Sleep", {"duration_ms": 0})])
    add(
        "notification",
        [("SendUserMessage", {"message": "ready", "status": "normal", "attachments": ["one.txt"]})],
        {"one.txt": "text"},
    )
    add(
        "git",
        [
            ("GitStatus", {}),
            ("GitShow", {"commit": "HEAD", "path": "one.txt"}),
            ("GitShow", {"commit": "HEAD", "path": "one.txt", "format": "metadata"}),
            ("GitDiff", {"commit": "HEAD", "commit2": "HEAD"}),
        ],
        {"one.txt": "committed\n"},
        git=True,
    )
    add("repl", [("REPL", {"language": "python", "code": "print(3 * 7)"})])
    for query in ["", "GitStatus", "select:git_status,TodoWrite", "+git changes", "worker trust"]:
        add("search-" + query, [("ToolSearch", {"query": query})])
    for tool, args in [
        ("TodoWrite", {"todos": []}),
        ("TodoWrite", {"todos": [{"content": " ", "activeForm": "doing", "status": "pending"}]}),
        ("TodoWrite", {"todos": [{"content": "valid", "activeForm": " ", "status": "pending"}]}),
        ("SendUserMessage", {"message": " ", "status": "normal"}),
        ("REPL", {"language": "python", "code": " "}),
        ("Agent", {"description": " ", "prompt": "never call model"}),
        ("Agent", {"description": "test", "prompt": " "}),
    ]:
        add("invalid-" + tool + "-" + str(len(result)), [(tool, args)])
    return result


def prepare(root, case):
    root.mkdir(parents=True, exist_ok=True)
    for name, text in case["files"].items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    if case["git"]:
        for args in (["init", "-q", "-b", "fixture"], ["add", "."], ["commit", "-qm", "fixture"]):
            subprocess.run(
                [
                    "git",
                    "-c",
                    "core.hooksPath=",
                    "-c",
                    "core.autocrlf=false",
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.test",
                    *args,
                ],
                cwd=root,
                check=True,
                capture_output=True,
            )


KEYS = {
    "read_file": ["type", "file"],
    "write_file": ["type", "filePath", "content", "structuredPatch", "originalFile", "gitDiff"],
    "edit_file": [
        "filePath",
        "oldString",
        "newString",
        "originalFile",
        "structuredPatch",
        "userModified",
        "replaceAll",
        "gitDiff",
    ],
    "NotebookEdit": [
        "new_source",
        "cell_id",
        "cell_type",
        "language",
        "edit_mode",
        "error",
        "notebook_path",
        "original_file",
        "updated_file",
    ],
    "TodoWrite": ["oldTodos", "newTodos", "verificationNudgeNeeded"],
    "StructuredOutput": ["data", "structured_output"],
    "Sleep": ["duration_ms", "message"],
    "SendUserMessage": ["message", "attachments"],
    "REPL": ["language", "stdout", "stderr", "exitCode"],
    "ToolSearch": [
        "matches",
        "query",
        "normalized_query",
        "total_deferred_tools",
        "pending_mcp_servers",
    ],
}


def project(tool, result, root):
    if result.get("error"):
        return {"error": True}
    value = result["value"]
    try:
        selected = {key: value[key] for key in KEYS.get(tool, ["output"])}
    except KeyError as error:
        raise ValueError(f"Unexpected {tool} fixture: {value}") from error
    if tool == "NotebookEdit":
        for key in ("original_file", "updated_file"):
            selected[key] = json.loads(selected[key])

    def normalize(value):
        if isinstance(value, str):
            # Windows Rust canonical paths include the extended-length prefix.
            value = value.removeprefix("\\\\?\\")
            value = value.replace(str(root), "<workspace>")
            return value.replace("\\", "/") if value.startswith("<workspace>") else value
        if isinstance(value, dict):
            return {k: normalize(v) for k, v in value.items()}
        if isinstance(value, list):
            return [normalize(v) for v in value]
        return value

    return {"value": normalize(selected)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=ROOT.parent / "claude/rust")
    args = parser.parse_args()
    baseline = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=args.source, text=True
    ).strip()
    with tempfile.TemporaryDirectory(prefix="myclaude-tool-contracts-") as directory:
        temp = Path(directory)
        (temp / "src").mkdir()
        (temp / "src/main.rs").write_text(SOURCE, encoding="utf-8")
        (temp / "Cargo.toml").write_text(
            '[package]\nname="myclaude-tool-verifier"\nversion="0.1.0"\nedition="2021"\n[dependencies]\nserde_json="1"\ntools={path='
            + json.dumps((args.source / "crates/tools").as_posix())
            + "}\n",
            encoding="utf-8",
        )
        log = ROOT / ".local/tool-contract-build.log"
        with log.open("w", encoding="utf-8") as output:
            subprocess.run(
                [
                    "cargo",
                    "build",
                    "--offline",
                    "--quiet",
                    "--manifest-path",
                    str(temp / "Cargo.toml"),
                    "--target-dir",
                    str(args.source / "target"),
                ],
                check=True,
                stdout=output,
                stderr=output,
            )
        entries = cases()
        for i, case in enumerate(entries):
            root = temp / "cases" / str(i)
            prepare(root, case)
            case["root"] = str(root)
        request = temp / "cases.json"
        request.write_text(json.dumps(entries), encoding="utf-8")
        executable = (
            args.source
            / "target/debug"
            / ("myclaude-tool-verifier.exe" if os.name == "nt" else "myclaude-tool-verifier")
        )
        env = {
            k: v
            for k, v in os.environ.items()
            if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT"}
        }
        env["CLAW_CONFIG_HOME"] = str(temp / "config")
        response = temp / "response.json"
        subprocess.run(
            [str(executable), str(request), str(response)],
            env=env,
            capture_output=True,
            encoding="utf-8",
            timeout=60,
            check=True,
        )
        traces = json.loads(response.read_text(encoding="utf-8"))
        (ROOT / ".local/tool-contract-traces.json").write_text(
            json.dumps(traces, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        for case, trace in zip(entries, traces, strict=True):
            root = case.pop("root")
            case["expected"] = [
                project(step["tool"], result, root)
                for step, result in zip(case["steps"], trace, strict=True)
            ]
            if case["name"] == "git":
                case["adaptations"] = {
                    "3": {
                        "reason": "Upstream git_stdout treats successful empty diff as an error; Python preserves empty success.",
                        "expected": {"value": {"output": ""}},
                    }
                }
            if case["name"] == "repl" and trace[0].get("error"):
                case["adaptations"] = {
                    "0": {
                        "reason": "Frozen Windows runtime detection depends on sh -lc and cannot find Python in the isolated environment; Python uses sys.executable.",
                        "expected": {
                            "value": {
                                "language": "python",
                                "stdout": "21\n",
                                "stderr": "",
                                "exitCode": 0,
                            }
                        },
                    }
                }
        target = ROOT / "backend/tests/fixtures/tools_upstream.json"
        target.write_text(
            json.dumps({"baseline": baseline, "cases": entries}, ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "cases": len(entries),
                    "invocations": sum(len(case["steps"]) for case in entries),
                    "baseline": baseline,
                }
            )
        )


if __name__ == "__main__":
    main()
