"""Persist observed extension phases, errors, health and bounded retry decisions."""

import time

PHASES = (
    "config_load",
    "server_registration",
    "spawn_connect",
    "initialize_handshake",
    "tool_discovery",
    "resource_discovery",
    "ready",
    "invocation",
    "error_surfacing",
    "shutdown",
    "cleanup",
)


class Lifecycle:
    def __init__(self, jobs, name):
        self.jobs, self.name = jobs, name

    def record(self, phase, error=None, recoverable=False, **data):
        if phase not in PHASES:
            raise ValueError("unknown lifecycle phase")
        previous = self.jobs.record("mcp_health", self.name) or {}
        events = previous.get("events", [])
        item = {
            "phase": phase,
            "timestamp": int(time.time()),
            "status": "failed" if error else "success",
            **data,
        }
        if error:
            item["error"] = {
                "phase": phase,
                "server_name": self.name,
                "message": self.jobs.store.redact(str(error))[:1000],
                "recoverable": recoverable,
                "timestamp": item["timestamp"],
            }
        events = [*events[-99:], item]
        state = previous.get("status", "starting")
        if error:
            state = "failed"
        elif phase == "ready":
            state = "degraded" if data.get("degraded") else "healthy"
        elif phase == "cleanup":
            state = "stopped"
        elif phase in {"server_registration", "spawn_connect", "initialize_handshake"}:
            state = "starting"
        return self.jobs.put(
            "mcp_health",
            self.name,
            {
                "server_name": self.name,
                "status": state,
                "phase": phase,
                "last_error": item.get("error"),
                "updated_at": item["timestamp"],
                "events": events,
            },
        )


def degraded_report(servers, available_tools=(), expected_tools=()):
    healthy = sorted({s["server_name"] for s in servers if s["status"] in {"healthy", "degraded"}})
    failed = [s for s in servers if s["status"] == "failed"]
    unavailable = [s for s in servers if s["status"] not in {"healthy", "degraded"}]
    missing = sorted(set(expected_tools) - set(available_tools))
    state = (
        "unconfigured"
        if not servers
        else "failed"
        if not healthy
        else "degraded"
        if unavailable or missing or any(s["status"] == "degraded" for s in servers)
        else "healthy"
    )
    return {
        "state": state,
        "working_servers": healthy,
        "failed_servers": failed,
        "unavailable_servers": unavailable,
        "available_tools": sorted(set(available_tools)),
        "missing_tools": missing,
    }
