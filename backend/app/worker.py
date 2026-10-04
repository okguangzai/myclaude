"""Worker boot control plane. Events describe observed state, never fabricate job results."""

import json
import os
import re
import tempfile
import time
from contextlib import suppress
from pathlib import Path


def event(worker, kind, status=None, detail=None, payload=None):
    worker["updated_at"] = int(time.time())
    if status:
        worker["status"] = status
    worker.setdefault("events", []).append(
        {
            "seq": len(worker.get("events", [])) + 1,
            "sequence": len(worker.get("events", [])) + 1,
            "kind": kind,
            "status": worker["status"],
            "created_at": int(time.time()),
            "timestamp": worker["updated_at"],
            "detail": detail,
            "payload": payload,
        }
    )
    emit_state_file(worker)
    return worker


def emit_state_file(worker):
    """Best-effort Rust-compatible observer surface; SQLite remains authoritative."""
    from .tools import safe_path

    temporary = None
    try:
        path = safe_path(worker["cwd"], ".claw/worker-state.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        snapshot = {
            "worker_id": worker["worker_id"],
            "status": worker["status"],
            "is_ready": worker["status"] == "ready_for_prompt",
            "trust_gate_cleared": worker["trust_gate_cleared"],
            "prompt_in_flight": worker["prompt_in_flight"],
            "last_event": worker["events"][-1] if worker["events"] else None,
            "updated_at": worker["updated_at"],
            "seconds_since_update": max(0, int(time.time()) - worker["updated_at"]),
        }
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix="worker-state-",
            suffix=".tmp",
            delete=False,
        ) as out:
            temporary = Path(out.name)
            json.dump(snapshot, out, ensure_ascii=False)
        temporary.replace(path)
    except (OSError, ValueError):
        pass
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def prompt_preview(prompt):
    trimmed = prompt.strip()
    return trimmed if len(trimmed) <= 48 else trimmed[:48].rstrip() + "…"


def ascii_lower(value):
    return value.translate(
        str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    )


def observed_cwd(screen):
    for line in screen.splitlines():
        tokens = line.split()
        index = next((i for i, t in enumerate(tokens) if t in {"$", "%", "#", ">", "›", "❯"}), 0)
        if index:
            candidate = tokens[index - 1]
            if candidate.startswith(("/", "~", ".")) or "/" in candidate:
                return candidate
    return None


def misdelivery(worker, screen, lower):
    prompt = worker.get("last_prompt")
    if not prompt or not worker.get("prompt_in_flight"):
        return None
    snippet = next((ascii_lower(line.strip()) for line in prompt.splitlines() if line.strip()), "")
    if not snippet:
        return None
    visible = snippet in lower
    echo = next(
        (
            line.lstrip()[1:].strip()
            for line in screen.splitlines()
            if line.lstrip().startswith("›") and line.lstrip()[1:].strip()
        ),
        None,
    )
    receipt, cwd = worker.get("expected_receipt"), observed_cwd(screen)
    target = None
    if receipt:
        tokens = [
            receipt.get(k, "") for k in ("repo", "task_kind", "source_surface", "objective_preview")
        ]
        tokens.extend(receipt.get("expected_artifacts", []))
        if (visible or (echo and snippet not in ascii_lower(echo))) and not all(
            ascii_lower(token) in lower for token in tokens
        ):
            target = "wrong_task"
    if not target and cwd and visible:
        expected = str(Path(worker["cwd"]).resolve())
        if not (
            expected.endswith(cwd)
            or cwd.endswith(expected)
            or Path(expected).name == Path(cwd).name
        ):
            target = "wrong_target"
    if (
        not target
        and visible
        and any(
            s in lower
            for s in (
                "command not found",
                "syntax error near unexpected token",
                "parse error near",
                "no such file or directory",
                "unknown command",
            )
        )
    ):
        target, cwd = "shell", None
    if target:
        return {
            "type": "prompt_delivery",
            "observed_target": target,
            "observed_cwd": cwd,
            "observed_prompt_preview": echo,
            "task_receipt": receipt,
            "prompt_preview": prompt_preview(prompt),
            "recovery_armed": False,
        }
    return None


def create(key, cwd, trusted_roots=(), auto_recover=True):
    trusted = any(
        Path(cwd).resolve().is_relative_to(Path(root).resolve()) for root in trusted_roots
    )
    worker = {
        "worker_id": key,
        "created_at": int(time.time()),
        "cwd": cwd,
        "status": "spawning",
        "trust_gate_cleared": False,
        "trust_auto_resolve": trusted,
        "auto_recover_prompt_misdelivery": auto_recover,
        "last_prompt": None,
        "replay_prompt": None,
        "last_error": None,
        "expected_receipt": None,
        "prompt_delivery_attempts": 0,
        "prompt_in_flight": False,
        "prompt_sent_at": None,
        "events": [],
    }
    return event(worker, "spawning", detail="worker created")


def failure(worker, kind, message):
    worker["last_error"] = {"kind": kind, "message": message, "created_at": int(time.time())}


def observe(worker, screen):
    lower = ascii_lower(screen)
    gate = permission_prompt(screen)
    if gate:
        failure(worker, "tool_permission_gate", "tool permission prompt detected")
        return event(
            worker,
            "tool_permission_required",
            "tool_permission_required",
            payload={"type": "tool_permission_prompt", "prompt_age_seconds": 0, **gate},
        )
    if not worker["trust_gate_cleared"] and any(
        s in lower
        for s in (
            "do you trust the files in this folder",
            "trust the files in this folder",
            "trust this folder",
            "allow and continue",
            "yes, proceed",
        )
    ):
        failure(worker, "trust_gate", "worker boot blocked on trust prompt")
        event(
            worker,
            "trust_required",
            "trust_required",
            payload={"type": "trust_prompt", "cwd": worker["cwd"]},
        )
        if worker.get("trust_policy") == "deny":
            failure(worker, "trust_gate", "cwd matches denied trust root")
            return event(worker, "trust_denied", "failed")
        if not worker["trust_auto_resolve"]:
            return worker
        worker.update(trust_gate_cleared=True, last_error=None)
        event(
            worker,
            "trust_resolved",
            "spawning",
            payload={
                "type": "trust_prompt",
                "resolution": "auto_allowlisted",
                "cwd": worker["cwd"],
            },
        )
    delivery = misdelivery(worker, screen, lower)
    if delivery:
        worker["prompt_in_flight"] = False
        failure(worker, "prompt_delivery", "worker prompt landed in " + delivery["observed_target"])
        event(worker, "prompt_misdelivery", "failed", payload=delivery)
        if worker["auto_recover_prompt_misdelivery"]:
            worker["replay_prompt"] = worker["last_prompt"]
            event(
                worker,
                "prompt_replay_armed",
                "ready_for_prompt",
                payload={**delivery, "recovery_armed": True},
            )
        return worker
    if worker.get("prompt_in_flight") and any(
        s in lower for s in ("thinking", "working", "running tests", "inspecting", "analyzing")
    ):
        worker.update(prompt_in_flight=False, status="running", last_error=None)
    last = next((line.strip() for line in reversed(screen.splitlines()) if line.strip()), "")
    shell = last.startswith(("$", "%", "#")) or last.endswith(("$", "%", "#"))
    ready = any(
        s in lower
        for s in ("ready for input", "ready for your input", "ready for prompt", "send a message")
    ) or (
        not shell
        and (
            last in {">", "›", "❯"}
            or last.startswith(("> ", "› ", "❯ "))
            or any(s in last for s in ("│ >", "│ ›", "│ ❯"))
        )
    )
    if ready and worker["status"] != "ready_for_prompt":
        worker["prompt_in_flight"] = False
        if (worker.get("last_error") or {}).get("kind") == "trust_gate":
            worker["last_error"] = None
        event(worker, "ready_for_prompt", "ready_for_prompt")
    return worker


def permission_prompt(screen):
    lower = ascii_lower(screen)
    if not (
        all(word in lower for word in ("allow the", "server", "tool", "run"))
        or "allow tool" in lower
        and "run" in lower
    ):
        return None
    line = next(
        (
            line.strip()
            for line in reversed(screen.splitlines())
            if "allow" in ascii_lower(line)
            and "tool" in ascii_lower(line)
            and ("run" in ascii_lower(line) or "server" in ascii_lower(line))
        ),
        screen,
    )
    quoted = re.search(r'"([^"]*)"', line)
    name = re.search(r"tool\s+(\S+)", line, re.I)
    server = re.search(r"the (.+?) server", line)
    tool = quoted[1] if quoted else name[1].strip("?:\"'") if name else None
    server_name = (
        server[1].removesuffix(" MCP")
        if server
        else tool.split("__")[1]
        if tool and tool.startswith("mcp__") and "__" in tool[5:]
        else None
    )
    scope = (
        "session_or_always"
        if any(
            s in lower
            for s in (
                "always allow",
                "allow always",
                "allow this tool always",
                "allow for all sessions",
            )
        )
        else "session_only"
        if any(
            s in lower
            for s in ("allow once", "allow for this session", "allow this session", "yes, allow")
        )
        else "unknown"
    )
    return {
        "server_name": server_name,
        "tool_name": tool,
        "allow_scope": scope,
        "prompt_preview": prompt_preview(line),
    }


async def preflight(cwd, prompt):
    from .git_context import git

    warnings = []
    try:
        metadata = Path(await git(cwd, "rev-parse", "--git-dir"))
        if not metadata.is_absolute():
            metadata = Path(cwd) / metadata
        if not os.access(metadata, os.W_OK):
            warnings.append(
                {
                    "kind": "git_metadata_not_writable",
                    "message": "git metadata is not writable",
                    "path": str(metadata),
                }
            )
    except (ValueError, OSError):
        pass
    paths = []
    for raw in prompt.split():
        token = raw.strip("`\"'()[]{},;:")
        if "/" not in token or "://" in token or token.startswith("/"):
            continue
        token = token.removeprefix("./")
        if (
            ".." not in token
            and re.fullmatch(r"[a-zA-Z0-9/_\-.]+", token)
            and "." in token.rsplit("/", 1)[-1]
            and token not in paths
        ):
            paths.append(token)
    for path in paths[:100]:
        try:
            await git(cwd, "ls-files", "--error-unmatch", "--", path)
        except (ValueError, OSError):
            warnings.append(
                {
                    "kind": "file_absent_on_branch",
                    "message": f"task mentions {path}, but git does not track it on the current branch",
                    "path": path,
                }
            )
    return warnings


def startup_timeout(worker, pane_command, transport_healthy, mcp_healthy, now=None):
    now = int(time.time()) if now is None else now
    events = worker.get("events", [])
    latest = next((e for e in reversed(events) if e["kind"] == "tool_permission_required"), None)
    evidence = {
        "last_lifecycle_state": worker["status"],
        "last_lifecycle_at": worker.get("updated_at", now),
        "pane_command": pane_command,
        "pane_observed_at": now,
        "command_started_at": worker.get("created_at", now),
        "prompt_sent_at": worker.get("prompt_sent_at"),
        "prompt_acceptance_state": worker["status"] == "running"
        and not worker.get("prompt_in_flight"),
        "trust_prompt_detected": any(e["kind"] == "trust_required" for e in events),
        "tool_permission_prompt_detected": latest is not None,
        "tool_permission_prompt_age_seconds": max(0, now - latest["created_at"])
        if latest
        else None,
        "tool_permission_allow_scope": (latest.get("payload") or {}).get("allow_scope")
        if latest
        else None,
        "transport_healthy": transport_healthy,
        "mcp_healthy": mcp_healthy,
        "transport_health": {"healthy": transport_healthy, "summary": "caller_observation"},
        "mcp_health": {"healthy": mcp_healthy, "summary": "caller_observation"},
        "elapsed_seconds": max(0, now - worker.get("created_at", now)),
    }
    if not transport_healthy:
        classification = "transport_dead"
    elif evidence["trust_prompt_detected"] and worker["status"] == "trust_required":
        classification = "trust_required"
    elif latest and worker["status"] == "tool_permission_required":
        classification = "tool_permission_required"
    elif worker.get("prompt_sent_at") is not None and not evidence["prompt_acceptance_state"]:
        classification = (
            "prompt_acceptance_timeout"
            if worker["status"] == "running"
            else "prompt_misdelivery"
            if evidence["elapsed_seconds"] > 30
            else "worker_crashed"
            if not mcp_healthy
            else "unknown"
        )
    else:
        classification = "worker_crashed" if not mcp_healthy else "unknown"
    failure(worker, "startup_no_evidence", f"worker startup stalled: {classification}")
    worker["prompt_in_flight"] = False
    return event(
        worker,
        "startup_no_evidence",
        "failed",
        payload={
            "type": "startup_no_evidence",
            "classification": classification,
            "evidence": evidence,
        },
    )


def resolve_trust(worker):
    if worker.get("trust_policy") == "deny":
        raise PermissionError("cwd matches denied trust root")
    if worker["status"] != "trust_required":
        raise ValueError("worker is not waiting on trust")
    worker.update(trust_gate_cleared=True, last_error=None)
    return event(
        worker,
        "trust_resolved",
        "spawning",
        payload={"type": "trust_prompt", "resolution": "manual_approval", "cwd": worker["cwd"]},
    )


def send_prompt(worker, prompt=None, receipt=None):
    if worker["status"] != "ready_for_prompt":
        raise ValueError("worker is not ready for prompt delivery: " + worker["status"])
    prompt = (prompt or "").strip() or worker.get("replay_prompt")
    if not prompt:
        raise ValueError("worker has no prompt to send or replay")
    worker.update(
        prompt_delivery_attempts=worker["prompt_delivery_attempts"] + 1,
        prompt_in_flight=True,
        prompt_sent_at=int(time.time()),
        last_prompt=prompt,
        expected_receipt=receipt,
        replay_prompt=None,
        last_error=None,
    )
    return event(
        worker, "running", "running", "prompt dispatched to worker: " + prompt_preview(prompt)
    )


def ready_snapshot(worker):
    return {
        "worker_id": worker["worker_id"],
        "status": worker["status"],
        "ready": worker["status"] == "ready_for_prompt",
        "blocked": worker["status"] in {"trust_required", "tool_permission_required", "failed"},
        "replay_prompt_ready": bool(worker.get("replay_prompt")),
        "last_error": worker.get("last_error"),
    }


def completion(worker, reason, tokens):
    worker["prompt_in_flight"] = False
    if reason == "error" or (reason == "unknown" and tokens == 0):
        failure(worker, "provider", f"session failed: finish={reason}, output={tokens}")
        return event(worker, "failed", "failed", "provider failure classified")
    worker["last_error"] = None
    return event(
        worker, "finished", "finished", f"session completed: finish={reason}, tokens={tokens}"
    )


def restart(worker):
    worker.update(
        trust_gate_cleared=False,
        last_prompt=None,
        replay_prompt=None,
        last_error=None,
        prompt_delivery_attempts=0,
        prompt_in_flight=False,
        prompt_sent_at=None,
    )
    return event(worker, "restarted", "spawning")


def terminate(worker):
    worker["prompt_in_flight"] = False
    return event(worker, "finished", "finished", "worker terminated by control plane")
