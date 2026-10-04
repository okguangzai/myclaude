"""Local memory/JSONL telemetry and ordered per-session traces. No remote export."""

import contextvars
import copy
import json
import threading
import time
from pathlib import Path

active = contextvars.ContextVar("session_tracer", default=None)


class MemorySink:
    def __init__(self):
        self.lock, self.records = threading.RLock(), []

    def record(self, event):
        with self.lock:
            self.records.append(copy.deepcopy(event))


class JsonlSink:
    def __init__(self, path, redact=lambda text: text):
        self.path, self.redact, self.lock = Path(path), redact, threading.RLock()
        self.last_error = None

    def record(self, event):
        line = self.redact(json.dumps(event, ensure_ascii=False))
        # Observability failure must never interrupt the user's model/tool run.
        try:
            with self.lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as target:
                    target.write(line + "\n")
                    target.flush()
        except OSError as error:
            self.last_error = type(error).__name__


class SessionTracer:
    def __init__(self, session_id, sink):
        self.session_id, self.sink = session_id, sink
        self.sequence, self.lock = 0, threading.RLock()

    def record(self, name, attributes=None):
        with self.lock:
            self.sink.record(
                {
                    "type": "session_trace",
                    "session_id": self.session_id,
                    "sequence": self.sequence,
                    "name": name,
                    "timestamp_ms": int(time.time() * 1000),
                    "attributes": attributes or {},
                }
            )
            self.sequence += 1

    def http(self, state, attempt, path, **attributes):
        self.sink.record(
            {
                "type": "http_request_" + state,
                "session_id": self.session_id,
                "attempt": attempt,
                "method": "POST",
                "path": path,
                "attributes": attributes,
            }
        )
        self.record(
            "http_request_" + state,
            {"attempt": attempt, "method": "POST", "path": path, **attributes},
        )


def record_http(state, attempt, path, **attributes):
    tracer = active.get()
    if tracer:
        tracer.http(state, attempt, path, **attributes)


def request_profile(version="0.6.0", betas=(), extra=None):
    return {
        "headers": {
            "anthropic-version": "2023-06-01",
            "user-agent": "myclaude/" + version,
            **({"anthropic-beta": ",".join(dict.fromkeys(betas))} if betas else {}),
        },
        "extra_body": {**(extra or {}), **({"betas": list(dict.fromkeys(betas))} if betas else {})},
        "runtime": "python",
    }
