"""Durable job metadata with bounded, cooperative asyncio execution."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

from croniter import croniter

from .db import now, uid
from .execution import RunContext, current


class Jobs:
    def __init__(self, db, store):
        self.db, self.store = db, store
        self.running = {}
        self.slots = asyncio.Semaphore(4)
        self.agent = None
        self.scheduler = None
        with db.connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,kind TEXT,status TEXT,
                parent_id TEXT,session_id TEXT,created_at TEXT,updated_at TEXT,payload TEXT,result TEXT);
            CREATE TABLE IF NOT EXISTS extension_state(kind TEXT,id TEXT,value TEXT,PRIMARY KEY(kind,id));
            """)
            if db.recover:
                from .process_owner import alive

                for row in c.execute(
                    "SELECT id FROM jobs WHERE status IN ('queued','running')"
                ).fetchall():
                    if not alive(c, "job", row[0]):
                        c.execute(
                            "UPDATE jobs SET status='interrupted',updated_at=? WHERE id=?",
                            (now(), row[0]),
                        )

    def records(self, kind):
        return [
            dict(id=r["id"], **json.loads(r["value"]))
            for r in self.db.all("SELECT * FROM extension_state WHERE kind=?", (kind,))
        ]

    def put(self, kind, key, value):
        with self.db.connect() as c:
            c.execute(
                "INSERT OR REPLACE INTO extension_state VALUES(?,?,?)",
                (kind, key, json.dumps(value, ensure_ascii=False)),
            )
        return dict(id=key, **value)

    def remove(self, kind, key):
        with self.db.connect() as c:
            c.execute("DELETE FROM extension_state WHERE kind=? AND id=?", (kind, key))

    def record(self, kind, key):
        return next((r for r in self.records(kind) if r["id"] == key), None)

    def list(self):
        rows = self.db.all("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 200")
        for r in rows:
            for k in ("payload", "result"):
                r[k] = json.loads(r[k]) if r[k] else None
        return [self.describe(r) for r in rows]

    def get(self, key):
        rows = self.db.all("SELECT * FROM jobs WHERE id=?", (key,))
        if not rows:
            raise ValueError("任务不存在")
        row = rows[0]
        for field in ("payload", "result"):
            row[field] = json.loads(row[field]) if row[field] else None
        return self.describe(row)

    def describe(self, row):
        payload = row.get("payload") or {}
        messages = (self.record("task_messages", row["id"]) or {}).get("messages", [])
        output = (row.get("result") or {}).get("answer", "")
        return {
            **row,
            "task_id": row["id"],
            "prompt": payload.get("prompt"),
            "description": payload.get("description"),
            "task_packet": payload.get("task_packet"),
            "messages": messages,
            "output": output,
            "has_output": bool(output),
            "heartbeat": (self.record("task_heartbeat", row["id"]) or {}).get("heartbeat"),
        }

    def heartbeat(self, key, transport_alive=True, status="running", observed_at=None):
        self.get(key)
        return self.put(
            "task_heartbeat",
            key,
            {
                "heartbeat": {
                    "observed_at": int(time.time()) if observed_at is None else observed_at,
                    "transport_alive": transport_alive,
                    "status": status,
                }
            },
        )["heartbeat"]

    def lane_board(self, now=None, stalled_after=60):
        from .lanes import board

        return board(self.list(), now, stalled_after)

    def append_message(self, key, text):
        job = self.get(key)
        if job["kind"] != "agent":
            raise ValueError("只有 Agent 任务接受追加消息")
        messages = job["messages"]
        if len(messages) >= 100 or not text.strip() or len(text) > 16000:
            raise ValueError("任务消息为空或超过数量/长度限制")
        message = {"role": "user", "content": self.store.redact(text), "timestamp": now()}
        self.put("task_messages", key, {"messages": [*messages, message]})
        queued = self.record("task_inbox", job["session_id"]) or {"messages": []}
        self.put("task_inbox", job["session_id"], {"messages": [*queued["messages"], message]})
        return {
            "task_id": key,
            "status": job["status"],
            "message_count": len(messages) + 1,
            "last_message": message["content"],
        }

    def active_children(self, run_id):
        return self.db.all(
            "SELECT id FROM jobs WHERE parent_id=? AND status IN ('queued','running')", (run_id,)
        )

    def finish(self, key, status, result):
        with self.db.connect() as c:
            c.execute(
                "UPDATE jobs SET status=?,updated_at=?,result=? WHERE id=?",
                (status, now(), self.store.redact(json.dumps(result, ensure_ascii=False)), key),
            )

    def submit(self, kind, payload, work, session_id=None, *, use_slot=True):
        key, ctx = uid(), current.get()
        if len(self.running) >= 32:
            raise ValueError("后台任务队列已满（32）")
        with self.db.connect() as c:
            c.execute(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    kind,
                    "queued",
                    ctx.run_id if ctx else None,
                    session_id,
                    now(),
                    now(),
                    self.store.redact(json.dumps(payload, ensure_ascii=False)),
                    None,
                ),
            )
            from .process_owner import claim

            claim(c, "job", key)

        async def run():
            heartbeat_task = None
            try:

                async def perform():
                    nonlocal heartbeat_task
                    self.finish(key, "running", None)
                    self.heartbeat(key)

                    async def pulse():
                        while True:
                            await asyncio.sleep(10)
                            self.heartbeat(key)

                    heartbeat_task = asyncio.create_task(pulse())
                    return await work()

                if use_slot:
                    async with self.slots:
                        result = await perform()
                else:
                    result = await perform()
                status = (
                    result.get("status", "completed") if isinstance(result, dict) else "completed"
                )
                if status not in {"completed", "failed", "timeout", "cancelled", "limit"}:
                    status = "completed"
                self.finish(key, status, result)
            except asyncio.CancelledError:
                self.finish(key, "cancelled", {"message": "任务已取消"})
            except Exception as exc:
                self.finish(key, "failed", {"error": self.store.redact(str(exc))[:1000]})
            finally:
                if heartbeat_task:
                    heartbeat_task.cancel()
                    await asyncio.gather(heartbeat_task, return_exceptions=True)
                self.heartbeat(key, status=self.get(key)["status"])
                self.running.pop(key, None)
                if kind == "agent" and self.agent:
                    self.agent.active.discard(session_id)

        task = asyncio.create_task(run(), name=f"job:{key}")
        self.running[key] = task
        return self.get(key)

    def agent_job(self, prompt, permission, cfg, description="Agent", model=None, session_id=None):
        parent = current.get()
        if parent:
            if parent.depth >= 2 or parent.budget["children"] >= 8:
                raise ValueError("子 Agent 深度上限 2；每棵任务树最多 8 个子任务")
            parent.budget["children"] += 1
        child = RunContext(
            permission=permission,
            depth=parent.depth + 1 if parent else 0,
            budget=parent.budget if parent else {"children": 0, "turns": 0},
        )
        session = (
            self.db.session(session_id)
            if session_id
            else self.db.session(self.db.create_session(description)["id"])
        )
        if session["id"] in self.agent.active:
            raise ValueError("Worker 会话正在运行")
        cfg = cfg.model_copy(deep=True)
        if model:
            cfg.model = model
        self.agent.active.add(session["id"])

        async def work():
            token = current.set(child)
            final = {}
            try:
                active_prompt, active_session = prompt, session
                while True:
                    self.agent.active.add(session["id"])
                    async for event in self.agent.run(
                        active_session, active_prompt, permission, cfg
                    ):
                        if event["type"] == "run_end":
                            final = event
                    queued = self.record("task_inbox", session["id"])
                    if final.get("status") != "completed" or not queued or not queued["messages"]:
                        break
                    self.remove("task_inbox", session["id"])
                    active_prompt = "\n".join(m["content"] for m in queued["messages"])
                    active_session = self.db.session(session["id"])
                messages = self.db.session(session["id"])["messages"]
                return {
                    **final,
                    "session_id": session["id"],
                    "answer": next(
                        (m.get("content") for m in reversed(messages) if m["role"] == "assistant"),
                        "",
                    ),
                }
            finally:
                self.agent.active.discard(session["id"])
                current.reset(token)

        try:
            # Descendants skip semaphore to prevent parents awaiting children from deadlocking.
            # The shared tree budget and max depth still strictly bound fan-out.
            return self.submit(
                "agent",
                {"prompt": prompt, "description": description, "permission": permission},
                work,
                session["id"],
                use_slot=not parent or parent.depth == 0,
            )
        except BaseException:
            self.agent.active.discard(session["id"])
            raise

    async def wait(self, key, timeout=600):
        task = self.running.get(key)
        if task:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        return self.get(key)

    async def stop(self, key):
        task = self.running.get(key)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            row = self.get(key)
            if row.get("session_id"):
                self.remove("task_inbox", row["session_id"])
            if row["status"] in {"queued", "running"}:
                self.finish(key, "cancelled", {"message": "任务在启动前取消"})
            if self.agent:
                self.agent.active.discard(row["session_id"])
            self.running.pop(key, None)
        return self.get(key)

    def cron(self, schedule, prompt, permission, description=""):
        if len(schedule.split()) != 5 or not croniter.is_valid(schedule):
            raise ValueError("使用五字段 cron 表达式，时区 UTC，最小间隔一分钟")
        value = {
            "schedule": schedule,
            "prompt": prompt,
            "permission": permission,
            "description": description,
            "enabled": True,
            "next_run": croniter(schedule, datetime.now(timezone.utc)).get_next(float),
            "last_job": None,
        }
        return self.put("cron", uid(), value)

    async def tick(self):
        while True:
            try:
                for item in self.records("cron"):
                    timestamp = datetime.now(timezone.utc).timestamp()
                    if not item["enabled"] or item["next_run"] > timestamp:
                        continue
                    key = item.pop("id")
                    item["next_run"] = croniter(item["schedule"], timestamp).get_next(float)
                    if item.get("last_job") not in self.running:
                        job = self.agent_job(
                            item["prompt"],
                            item["permission"],
                            self.store.current,
                            item["description"] or "Scheduled Agent",
                        )
                        item["last_job"] = job["id"]
                    self.put("cron", key, item)
            except Exception as exc:
                self.put("scheduler", "error", {"error": self.store.redact(str(exc)), "at": now()})
            await asyncio.sleep(5)

    async def close(self):
        tasks = list(self.running.values()) + ([self.scheduler] if self.scheduler else [])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for key in list(self.running):
            await self.stop(key)
