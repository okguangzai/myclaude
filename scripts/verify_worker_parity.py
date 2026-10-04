"""Capture WorkerRegistry traces from the frozen Rust library, then compare Python.

Only nondeterministic identity/time and human-readable detail/error wording are
excluded. Event sequence, typed payloads and lifecycle fields remain contractual.
"""

import argparse
import copy
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app import worker  # noqa: E402

SOURCE = r"""
use runtime::WorkerRegistry;
use serde_json::{Value, json};
fn main() {
    let args: Vec<String> = std::env::args().collect();
    let cases: Vec<Value> = serde_json::from_slice(&std::fs::read(&args[1]).unwrap()).unwrap();
    let mut results = Vec::new();
    for case in cases {
        let registry = WorkerRegistry::new();
        let cwd = case["cwd"].as_str().unwrap();
        let roots = if case["trusted"].as_bool().unwrap_or(false) { vec![cwd.to_string()] } else {vec![]};
        let w = registry.create(cwd, &roots, case["recover"].as_bool().unwrap_or(true));
        let id = w.worker_id;
        let mut trace = vec![json!({"worker": registry.get(&id)})];
        for step in case["steps"].as_array().unwrap() {
            let result = match step["op"].as_str().unwrap() {
                "observe" => registry.observe(&id, step["screen"].as_str().unwrap()),
                "send" => registry.send_prompt(&id, step["prompt"].as_str(),
                    step.get("receipt").map(|v| serde_json::from_value(v.clone()).unwrap())),
                "trust" => registry.resolve_trust(&id),
                "restart" => registry.restart(&id),
                "terminate" => registry.terminate(&id),
                "complete" => registry.observe_completion(&id, step["reason"].as_str().unwrap(), step["tokens"].as_u64().unwrap()),
                "timeout" => registry.observe_startup_timeout(&id, "claw", step["transport"].as_bool().unwrap(), step["mcp"].as_bool().unwrap()),
                _ => panic!("unknown operation"),
            };
            trace.push(json!({"error":result.err(), "worker":registry.get(&id)}));
        }
        results.push(json!({"name":case["name"], "trace":trace}));
    }
    println!("{}", serde_json::to_string(&results).unwrap());
}
"""


def cases():
    result = []

    def add(name, steps, **kwargs):
        result.append({"name": name, "steps": steps, **kwargs})

    ready = {"op": "observe", "screen": "Ready for input"}
    send = {"op": "send", "prompt": "implement a test"}
    for screen in ["", "$", "repo %", "# root", ">", "›", "❯", "│ >", "send a message"]:
        add("ready-" + repr(screen), [{"op": "observe", "screen": screen}])
    for trusted in [False, True]:
        add(
            f"trust-{trusted}",
            [{"op": "observe", "screen": "Trust this folder\n>"}, {"op": "trust"}, ready, send],
            trusted=trusted,
        )
    for scope in ["", "Allow once", "Allow for this session", "Allow this tool always"]:
        for prompt in [
            'Allow the files MCP server tool "mcp__files__read" to run?',
            "Allow tool 'mcp__files__read'? to run?",
        ]:
            add(
                "permission-" + prompt + scope,
                [
                    {"op": "observe", "screen": prompt + "\n" + scope},
                    {"op": "timeout", "transport": True, "mcp": True},
                    ready,
                ],
            )
    receipt = {
        "repo": "expected",
        "task_kind": "build",
        "source_surface": "cli",
        "objective_preview": "implement",
        "expected_artifacts": ["report.md"],
    }
    screens = [
        "implement a test: command not found",
        "/different/repo $ implement a test",
        "/some/expected $ implement a test",
        "› unrelated work\nThinking",
        "implement a test expected build cli",
        "implement a test expected build cli report.md\nThinking",
        "unrelated command not found",
        "Working\nimplement a test: command not found",
    ]
    for recover in [False, True]:
        for with_receipt in [False, True]:
            for index, screen in enumerate(screens):
                prompt = {**send, **({"receipt": receipt} if with_receipt else {})}
                add(
                    f"delivery-{recover}-{with_receipt}-{index}",
                    [
                        ready,
                        prompt,
                        {"op": "observe", "screen": screen},
                        {"op": "send"},
                        {"op": "restart"},
                        ready,
                        send,
                        {"op": "observe", "screen": "Thinking"},
                        {"op": "observe", "screen": "implement a test: command not found"},
                    ],
                    recover=recover,
                )
    for reason in ["stop", "end_turn", "unknown", "error", "max_tokens"]:
        for tokens in [0, 3]:
            add(
                f"completion-{reason}-{tokens}",
                [
                    ready,
                    send,
                    {"op": "complete", "reason": reason, "tokens": tokens},
                    {"op": "restart"},
                    {"op": "terminate"},
                ],
            )
    for prefix, steps in [
        ("empty", []),
        ("trust", [{"op": "observe", "screen": "Trust this folder"}]),
        ("sent", [ready, send]),
        ("accepted", [ready, send, {"op": "observe", "screen": "Thinking"}]),
    ]:
        for transport in [False, True]:
            for mcp in [False, True]:
                add(
                    f"timeout-{prefix}-{transport}-{mcp}",
                    [*steps, {"op": "timeout", "transport": transport, "mcp": mcp}],
                )
    add(
        "invalid-transitions",
        [{"op": "send"}, {"op": "trust"}, ready, {"op": "send", "prompt": "  "}, send, send],
    )
    add(
        "preview-unicode",
        [
            ready,
            {"op": "send", "prompt": "学" * 60},
            {"op": "observe", "screen": "学" * 60 + ": command not found"},
        ],
    )
    return result


def project(value):
    """Version-independent observable contract, documented exclusions above."""
    if isinstance(value, list):
        return [project(v) for v in value]
    if not isinstance(value, dict):
        return value
    skip = {
        "worker_id",
        "cwd",
        "created_at",
        "updated_at",
        "timestamp",
        "sequence",
        "detail",
        "message",
        "pane_observed_at",
        "command_started_at",
        "last_lifecycle_at",
        "elapsed_seconds",
        "tool_permission_prompt_age_seconds",
        "prompt_age_seconds",
    }
    output = {}
    for key, item in value.items():
        if key in skip or item is None:
            continue
        if key == "error":
            output[key] = True
        elif key == "prompt_sent_at":
            output[key] = True
        elif key in {"transport_health", "mcp_health"}:
            output[key] = {"healthy": item["healthy"]}
        else:
            output[key] = project(item)
    return output


def python_trace(case, cwd):
    item = worker.create(
        "fixture", str(cwd), [str(cwd)] if case.get("trusted") else [], case.get("recover", True)
    )
    trace = [{"worker": copy.deepcopy(item)}]
    for step in case["steps"]:
        error = None
        try:
            match step["op"]:
                case "observe":
                    worker.observe(item, step["screen"])
                case "send":
                    worker.send_prompt(item, step.get("prompt"), step.get("receipt"))
                case "trust":
                    worker.resolve_trust(item)
                case "restart":
                    worker.restart(item)
                case "terminate":
                    worker.terminate(item)
                case "complete":
                    worker.completion(item, step["reason"], step["tokens"])
                case "timeout":
                    worker.startup_timeout(item, "claw", step["transport"], step["mcp"])
        except ValueError as exc:
            error = str(exc)
        trace.append({"error": error, "worker": copy.deepcopy(item)})
    return project(trace)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=ROOT.parent / "claude/rust")
    parser.add_argument("--write-fixtures", action="store_true")
    args = parser.parse_args()
    baseline = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=args.source, text=True
    ).strip()
    with tempfile.TemporaryDirectory(prefix="myclaude-worker-") as directory:
        temp = Path(directory)
        cwd = temp / "expected"
        cwd.mkdir()
        (temp / "src").mkdir()
        (temp / "src/main.rs").write_text(SOURCE, encoding="utf-8")
        (temp / "Cargo.toml").write_text(
            '[package]\nname="myclaude-worker-verifier"\nversion="0.1.0"\nedition="2021"\n'
            '[dependencies]\nserde_json="1"\nruntime={path='
            + json.dumps((args.source / "crates/runtime").as_posix())
            + "}\n",
            encoding="utf-8",
        )
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
        )
        executable = (
            args.source
            / "target/debug"
            / ("myclaude-worker-verifier.exe" if os.name == "nt" else "myclaude-worker-verifier")
        )
        scenarios = cases()
        inputs = temp / "cases.json"
        inputs.write_text(json.dumps([{**c, "cwd": str(cwd)} for c in scenarios]), encoding="utf-8")
        rust = json.loads(subprocess.check_output([str(executable), str(inputs)], encoding="utf-8"))
        fixtures = {
            "baseline": baseline,
            "cases": [
                {**case, "expected": project(actual["trace"])}
                for case, actual in zip(scenarios, rust, strict=True)
            ],
        }
        if args.write_fixtures:
            (ROOT / "backend/tests/fixtures/worker_upstream.json").write_text(
                json.dumps(fixtures, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        failures = [c["name"] for c in fixtures["cases"] if python_trace(c, cwd) != c["expected"]]
        print(json.dumps({"baseline": baseline, "scenarios": len(scenarios), "failures": failures}))
        return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
