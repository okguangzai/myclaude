"""Lane event ordering, terminal reconciliation and heartbeat projections."""

import copy
import hashlib
import json
import time

TERMINAL = {"lane.finished", "lane.failed", "lane.merged", "lane.superseded", "lane.closed"}
CONFIDENCE = {"unknown": 0, "low": 1, "medium": 2, "high": 3}


def fingerprint(event):
    payload = {key: event.get(key) for key in ("event", "status", "data")}
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]


def build(name, status, sequence, data=None, provenance="live_lane", **metadata):
    result = {
        "event": name,
        "status": status,
        "data": data,
        "metadata": {
            "seq": sequence,
            "timestamp_ms": int(time.time() * 1000),
            "provenance": provenance,
            **metadata,
        },
    }
    if name in TERMINAL:
        result["metadata"]["event_fingerprint"] = fingerprint(result)
    return result


def materially_differ(first, second):
    return any(
        first.get(key) != second.get(key) for key in ("event", "status", "failure_class", "data")
    )


def reconcile(events):
    terminal, advisory, uncertain = None, [], False
    for event in sorted(copy.deepcopy(events), key=lambda e: e["metadata"]["seq"]):
        if event["event"] in TERMINAL:
            if terminal:
                current = event["metadata"].get("event_fingerprint")
                if current and current == terminal["metadata"].get("event_fingerprint"):
                    continue
                if materially_differ(terminal, event):
                    terminal = event
            else:
                terminal = event
        else:
            if event["event"] == "lane.reconciled" and terminal:
                uncertain = True
            advisory.append(event)
    if terminal and uncertain:
        terminal.update(
            event="lane.reconciled",
            status="reconciled",
            detail="Session terminal state uncertain: transport died after terminal event",
        )
    return {"terminal": terminal, "advisory": advisory}


def filter_events(events, provenance=None, environment=None, min_confidence=None):
    return [
        e
        for e in events
        if (provenance is None or e["metadata"].get("provenance") == provenance)
        and (environment is None or e["metadata"].get("environment_label") == environment)
        and (
            min_confidence is None
            or e["metadata"].get("confidence_level") is not None
            and CONFIDENCE.get(e["metadata"]["confidence_level"], 0) >= CONFIDENCE[min_confidence]
        )
    ]


def dedupe_commits(events):
    keep, latest = set(range(len(events))), {}
    for index, event in enumerate(events):
        if event["event"] != "lane.commit.created" or not event.get("data"):
            continue
        data = event["data"]
        if isinstance(data.get("supersededBy"), str):
            keep.discard(index)
            continue
        key = data.get("canonicalCommit", data.get("commit"))
        if key:
            keep.discard(latest.get(key))
            latest[key] = index
    return [event for index, event in enumerate(events) if index in keep]


def dedupe_terminal(events):
    seen, result = set(), []
    for event in events:
        fp = event.get("metadata", {}).get("event_fingerprint")
        if event["event"] in TERMINAL and fp:
            if fp in seen:
                continue
            seen.add(fp)
        result.append(copy.deepcopy(event))
    return result


def nudge(nudge_id, delivered_at, original_nudge_id=None):
    return {
        "nudge_id": nudge_id,
        "delivered_at": delivered_at,
        "acknowledged": False,
        "acknowledged_at": None,
        "is_retry": original_nudge_id is not None,
        "original_nudge_id": original_nudge_id,
    }


def acknowledge_nudge(tracking, at):
    return {**tracking, "acknowledged": True, "acknowledged_at": at}


def classify_nudge(nudge_id, tracking=(), acknowledged_ids=()):
    if nudge_id in acknowledged_ids:
        return "stale_duplicate"
    for item in tracking:
        if item["nudge_id"] == nudge_id:
            return "stale_duplicate" if item["acknowledged"] else "retry"
        if item.get("original_nudge_id") == nudge_id:
            return "stale_duplicate"
    return "new"


def freshness(heartbeat, now=None, stalled_after=60):
    if not heartbeat:
        return "unknown"
    if not heartbeat["transport_alive"]:
        return "transport_dead"
    age = max(0, (int(time.time()) if now is None else now) - heartbeat["observed_at"])
    return "stalled" if age > stalled_after else "healthy"


def board(tasks, now=None, stalled_after=60):
    now = int(time.time()) if now is None else now
    result = {"generated_at": now, "active": [], "blocked": [], "finished": []}
    for task in tasks:
        item = {
            key: task.get(key) for key in ("task_id", "prompt", "status", "team_id", "heartbeat")
        }
        item["freshness"] = freshness(item["heartbeat"], now, stalled_after)
        group = (
            "active"
            if item["status"] in {"queued", "created", "running"}
            else "blocked"
            if item["status"] == "blocked"
            else "finished"
        )
        result[group].append(item)
    return result
