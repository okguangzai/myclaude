"""Durable prompt fingerprint diagnostics and short-lived completion cache."""

import hashlib
import json
import sqlite3
import time
from pathlib import Path


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class PromptCache:
    def __init__(self, data, session, completion_ttl=30, prompt_ttl=300, break_min_drop=2000):
        from .cache_interop import RustPromptCache

        self.interop = RustPromptCache(Path(data) / "cache/prompt-cache", session, completion_ttl)
        self.path, self.session = Path(data) / "prompt-cache.sqlite", session
        self.completion_ttl, self.prompt_ttl, self.break_min_drop = (
            completion_ttl,
            prompt_ttl,
            break_min_drop,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(
                "CREATE TABLE IF NOT EXISTS state(session TEXT PRIMARY KEY, previous TEXT, stats TEXT); CREATE TABLE IF NOT EXISTS completions(session TEXT, key TEXT, at INTEGER, response TEXT, PRIMARY KEY(session,key));"
            )

    def connect(self):
        from contextlib import contextmanager

        @contextmanager
        def connection():
            conn = sqlite3.connect(self.path, timeout=15)
            try:
                with conn:
                    yield conn
            finally:
                conn.close()

        return connection()

    def load(self, conn):
        row = conn.execute(
            "SELECT previous,stats FROM state WHERE session=?", (self.session,)
        ).fetchone()
        return (
            (json.loads(row[0]) if row[0] else None, json.loads(row[1]))
            if row
            else (
                None,
                {
                    key: 0
                    for key in (
                        "tracked_requests",
                        "completion_cache_hits",
                        "completion_cache_misses",
                        "completion_cache_writes",
                        "expected_invalidations",
                        "unexpected_cache_breaks",
                        "total_cache_creation_input_tokens",
                        "total_cache_read_input_tokens",
                    )
                },
            )
        )

    def save(self, conn, previous, stats):
        conn.execute(
            "INSERT OR REPLACE INTO state VALUES(?,?,?)",
            (self.session, json.dumps(previous), json.dumps(stats)),
        )

    def stats(self):
        with self.connect() as conn:
            return self.load(conn)[1]

    def record(self, request, usage, response=None, now=None):
        now = int(time.time()) if now is None else now
        current = {
            key: fingerprint(request.get(key)) for key in ("model", "system", "tools", "messages")
        }
        current.update(
            observed_at=now, cache_read_input_tokens=usage.get("cache_read_input_tokens", 0)
        )
        key, cache_break = fingerprint(request), None
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous, stats = self.load(conn)
            drop = max(
                0,
                (previous or {}).get("cache_read_input_tokens", 0)
                - current["cache_read_input_tokens"],
            )
            if previous and drop >= self.break_min_drop:
                reasons = [
                    label + " changed"
                    for field, label in (
                        ("model", "model"),
                        ("system", "system prompt"),
                        ("tools", "tool definitions"),
                        ("messages", "message payload"),
                    )
                    if previous[field] != current[field]
                ]
                expired = max(0, now - previous["observed_at"]) > self.prompt_ttl
                unexpected = not reasons and not expired
                reason = (
                    ", ".join(reasons)
                    if reasons
                    else "possible prompt cache TTL expiry"
                    if expired
                    else "cache read tokens dropped while prompt fingerprint remained stable"
                )
                cache_break = {
                    "unexpected": unexpected,
                    "reason": reason,
                    "previous_cache_read_input_tokens": previous["cache_read_input_tokens"],
                    "current_cache_read_input_tokens": current["cache_read_input_tokens"],
                    "token_drop": drop,
                }
                stats["unexpected_cache_breaks" if unexpected else "expected_invalidations"] += 1
                stats["last_break_reason"] = reason
            stats["tracked_requests"] += 1
            for field in ("cache_read_input_tokens", "cache_creation_input_tokens"):
                stats["total_" + field] += usage.get(field, 0)
                stats["last_" + field] = usage.get(field, 0)
            stats.update(last_request_hash=key, last_cache_source="api-response")
            if response is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO completions VALUES(?,?,?,?)",
                    (self.session, key, now, json.dumps(response)),
                )
                stats["completion_cache_writes"] += 1
            self.save(conn, current, stats)
            # The SQLite database remains the concurrent diagnostic store. The file
            # mirror permits upstream consumers to read the same protocol requests.
            try:
                self.interop.record(request, response or {"usage": usage}, stats, now)
            except (OSError, ValueError, KeyError, TypeError):
                pass  # Cache persistence must not break a successful model response.
            return {"cache_break": cache_break, "stats": stats}

    def lookup(self, request, now=None):
        now, key = int(time.time()) if now is None else now, fingerprint(request)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous, stats = self.load(conn)
            row = conn.execute(
                "SELECT at,response FROM completions WHERE session=? AND key=?", (self.session, key)
            ).fetchone()
            hit = bool(row and max(0, now - row[0]) < self.completion_ttl)
            stats["completion_cache_hits" if hit else "completion_cache_misses"] += 1
            stats["last_completion_cache_key"] = key
            if hit:
                stats["last_cache_source"] = "completion-cache"
            elif row:
                conn.execute(
                    "DELETE FROM completions WHERE session=? AND key=?", (self.session, key)
                )
            self.save(conn, previous, stats)
            return json.loads(row[1]) if hit else None
