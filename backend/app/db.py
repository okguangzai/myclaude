from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def uid() -> str:
    return str(uuid.uuid4())


class Database:
    def __init__(self, path: Path, recover=True):
        self.path = path
        self.recover = recover
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS process_owners(kind TEXT,id TEXT,pid INTEGER,birth TEXT,PRIMARY KEY(kind,id));
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, title TEXT, created_at TEXT, updated_at TEXT,
                    messages TEXT NOT NULL DEFAULT '[]');
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, session_id TEXT, status TEXT, model TEXT,
                    permission TEXT, started_at TEXT, finished_at TEXT, turns INTEGER DEFAULT 0,
                    input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, error TEXT);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, kind TEXT, data TEXT, at TEXT);
                CREATE INDEX IF NOT EXISTS events_run ON events(run_id, id);
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, name TEXT, content TEXT, created_at TEXT,
                    model TEXT, dimension INTEGER, namespace TEXT, chunks INTEGER, backend TEXT);
                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY, document_id TEXT, ordinal INTEGER,
                    text TEXT, vector TEXT, FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE);
                CREATE INDEX IF NOT EXISTS chunks_document ON chunks(document_id);
            """)
            columns = {row[1] for row in c.execute("PRAGMA table_info(runs)")}
            for column in ("cache_read_input_tokens", "cache_creation_input_tokens"):
                if column not in columns:
                    c.execute(f"ALTER TABLE runs ADD COLUMN {column} INTEGER DEFAULT 0")
            if recover:
                from .process_owner import alive

                for row in c.execute("SELECT id FROM runs WHERE status='running'").fetchall():
                    if not alive(c, "run", row[0]):
                        c.execute(
                            "UPDATE runs SET status='interrupted',finished_at=? WHERE id=?",
                            (now(), row[0]),
                        )

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        try:
            with c:
                yield c
        finally:
            c.close()

    def all(self, sql: str, args=()) -> list[dict]:
        with self.connect() as c:
            return [dict(row) for row in c.execute(sql, args).fetchall()]

    def create_session(self, title="新对话") -> dict:
        record = {
            "id": uid(),
            "title": title[:80],
            "created_at": now(),
            "updated_at": now(),
        }
        with self.connect() as c:
            c.execute(
                "INSERT INTO sessions(id,title,created_at,updated_at) VALUES(:id,:title,:created_at,:updated_at)",
                record,
            )
        return record

    def session(self, session_id: str) -> dict | None:
        rows = self.all("SELECT * FROM sessions WHERE id=?", (session_id,))
        if not rows:
            return None
        row = rows[0]
        row["messages"] = json.loads(row["messages"])
        return row

    def save_messages(self, session_id: str, messages: list, title: str | None = None):
        with self.connect() as c:
            c.execute(
                "UPDATE sessions SET messages=?,updated_at=? WHERE id=?",
                (json.dumps(messages, ensure_ascii=False), now(), session_id),
            )
            if title:
                c.execute("UPDATE sessions SET title=? WHERE id=?", (title[:50], session_id))

    def start_run(self, session_id: str, model: str, permission: str) -> str:
        run_id = uid()
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if not c.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
                raise ValueError("会话已删除或不存在，不能启动任务")
            if c.execute(
                "SELECT 1 FROM runs WHERE session_id=? AND status='running'", (session_id,)
            ).fetchone():
                raise ValueError("该会话已有正在执行的任务")
            c.execute(
                "INSERT INTO runs(id,session_id,status,model,permission,started_at) VALUES(?,?,'running',?,?,?)",
                (run_id, session_id, model, permission, now()),
            )
            from .process_owner import claim

            claim(c, "run", run_id)
        return run_id

    def finish_run(
        self,
        run_id: str,
        status: str,
        turns: int,
        usage: dict,
        error: str | None = None,
    ):
        with self.connect() as c:
            c.execute(
                "UPDATE runs SET status=?,finished_at=?,turns=?,input_tokens=?,output_tokens=?,error=?,cache_read_input_tokens=?,cache_creation_input_tokens=? WHERE id=?",
                (
                    status,
                    now(),
                    turns,
                    usage.get("input_tokens", 0),
                    usage.get("output_tokens", 0),
                    error,
                    usage.get("cache_read_input_tokens", 0),
                    usage.get("cache_creation_input_tokens", 0),
                    run_id,
                ),
            )

    def event(self, run_id: str, kind: str, data: dict):
        with self.connect() as c:
            c.execute(
                "INSERT INTO events(run_id,kind,data,at) VALUES(?,?,?,?)",
                (run_id, kind, json.dumps(data, ensure_ascii=False), now()),
            )

    def events(self, run_id: str):
        rows = self.all("SELECT * FROM events WHERE run_id=? ORDER BY id", (run_id,))
        for row in rows:
            row["data"] = json.loads(row["data"])
        return rows
