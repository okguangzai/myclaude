"""Build a small verifier against the frozen Rust api crate; no network/model calls."""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.bash_validation import RULES, classify_command, validate_command  # noqa: E402
from app.cache_interop import RustPromptCache, request_key  # noqa: E402

SOURCE = r"""
use api::{MessageRequest, MessageResponse, PromptCache};
fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args[1] == "bash" {
        let cases: Vec<String> = serde_json::from_slice(&std::fs::read(&args[2]).unwrap()).unwrap();
        let mut results = Vec::new();
        for command in cases {
            let intent = format!("{:?}", runtime::bash_validation::classify_command(&command));
            for (label, mode) in [("read-only", runtime::PermissionMode::ReadOnly), ("workspace-write", runtime::PermissionMode::WorkspaceWrite), ("danger-full-access", runtime::PermissionMode::DangerFullAccess)] {
                let result = runtime::bash_validation::validate_command(&command, mode, std::path::Path::new("/workspace"));
                let decision = match result {runtime::bash_validation::ValidationResult::Allow => "Allow", runtime::bash_validation::ValidationResult::Block{..} => "Block", runtime::bash_validation::ValidationResult::Warn{..} => "Warn"};
                results.push(serde_json::json!({"command":command,"mode":label,"intent":intent,"decision":decision}));
            }
        }
        println!("{}", serde_json::to_string(&results).unwrap());
        return;
    }
    let request: MessageRequest = serde_json::from_slice(&std::fs::read(&args[2]).unwrap()).unwrap();
    let cache = PromptCache::new(&args[3]);
    if args[1] == "read" {
        let response = cache.lookup_completion(&request).expect("Python completion must be readable");
        println!("{}", serde_json::to_string(&response).unwrap());
    } else {
        let response: MessageResponse = serde_json::from_slice(&std::fs::read(&args[4]).unwrap()).unwrap();
        let _ = cache.record_response(&request, &response);
    }
}
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=ROOT.parent / "claude/rust")
    parser.add_argument("--write-fixtures", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="myclaude-cache-interop-") as directory:
        temp = Path(directory)
        (temp / "src").mkdir()
        (temp / "src/main.rs").write_text(SOURCE, encoding="utf-8")
        (temp / "Cargo.toml").write_text(
            '[package]\nname="myclaude-cache-verifier"\nversion="0.1.0"\nedition="2021"\n'
            "[dependencies]\napi={path="
            + json.dumps((args.source / "crates/api").as_posix())
            + '}\nserde_json="1"\nruntime={path='
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
            / ("myclaude-cache-verifier.exe" if os.name == "nt" else "myclaude-cache-verifier")
        )
        request = {
            "model": "fixture",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "中文 cache"}]}],
            "system": "cache test",
            "tools": [
                {
                    "name": "read",
                    "description": "fixture",
                    "input_schema": {"type": "object", "properties": {"z": {}, "a": {}}},
                }
            ],
        }
        response = {
            "id": "fixture-response",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "cache interop OK"}],
            "model": "fixture",
            "usage": {
                "input_tokens": 2,
                "output_tokens": 3,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 5,
            },
        }
        request_path, response_path = temp / "request.json", temp / "response.json"
        request_path.write_text(json.dumps(request), encoding="utf-8")
        response_path.write_text(json.dumps(response), encoding="utf-8")
        env = {**os.environ, "CLAUDE_CONFIG_HOME": str(temp / "config")}
        root = temp / "config/cache/prompt-cache"
        RustPromptCache(root, "python-written").record(request, response)
        result = subprocess.run(
            [str(executable), "read", str(request_path), "python-written"],
            env=env,
            check=True,
            capture_output=True,
        )
        assert json.loads(result.stdout)["id"] == response["id"]
        subprocess.run(
            [str(executable), "write", str(request_path), "rust-written", str(response_path)],
            env=env,
            check=True,
        )
        loaded = RustPromptCache(root, "rust-written").lookup(request)
        assert loaded and loaded["content"] == response["content"]
        source = (args.source / "crates/runtime/src/bash_validation.rs").read_text(encoding="utf-8")
        examples = re.findall(
            r'(?:classify_command|validate_command|validate_read_only|validate_sed|validate_paths|check_destructive)\(\s*"((?:[^"\\]|\\.)*)"',
            source,
        )
        cases = sorted(
            {
                *[json.loads('"' + item + '"') for item in examples],
                *[
                    command
                    for key, values in RULES.items()
                    if key.endswith("COMMANDS")
                    for command in values
                ],
            }
        )
        cases_path = temp / "bash-cases.json"
        cases_path.write_text(json.dumps(cases), encoding="utf-8")
        compared = subprocess.run(
            [str(executable), "bash", str(cases_path)], check=True, capture_output=True
        )
        results = json.loads(compared.stdout)
        for result in results:
            command, mode = result["command"], result["mode"]
            assert classify_command(command) == result["intent"], result
            assert (
                validate_command(command, mode, "/workspace")["decision"] == result["decision"]
            ), result
        if args.write_fixtures:
            fixture = ROOT / "backend/tests/fixtures/bash_upstream.json"
            fixture.parent.mkdir(exist_ok=True)
            fixture.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        report = {
            "python_to_rust": True,
            "rust_to_python": True,
            "request_key": request_key(request),
            "model_requests": 0,
            "bash_differential_cases": len(results),
        }
        (ROOT / ".local/cache-interchange.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
        print(json.dumps(report))


if __name__ == "__main__":
    main()
