from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path

from .db import connect, ensure_schema_version
from .event_bus import EventBus, ScheduledTaskEvent

logger = logging.getLogger("navi.scheduler")


class SchedulerStore:
    def __init__(self, home: Path):
        self.db_path = home / "scheduler.db"
        self._init_db()

    def _init_db(self):
        with connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduled_tasks (
                    id TEXT PRIMARY KEY,
                    trigger_at REAL,
                    action TEXT,
                    payload TEXT,
                    status TEXT
                )
                """
            )
            ensure_schema_version(conn, "scheduler", 1)

    def schedule_task(self, trigger_at: float, action: str, payload: dict) -> str:
        task_id = uuid.uuid4().hex
        with connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO scheduled_tasks (id, trigger_at, action, payload, status) VALUES (?, ?, ?, ?, ?)",
                (task_id, trigger_at, action, json.dumps(payload), "pending"),
            )
        return task_id

    def poll_due_tasks(self) -> list[dict]:
        now = time.time()
        tasks = []
        with connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT id, trigger_at, action, payload, status FROM scheduled_tasks WHERE status = 'pending' AND trigger_at <= ?",
                (now,),
            )
            for row in cursor.fetchall():
                tasks.append(
                    {
                        "id": row[0],
                        "trigger_at": row[1],
                        "action": row[2],
                        "payload": json.loads(row[3]),
                        "status": row[4],
                    }
                )
        return tasks

    def mark_completed(self, task_id: str):
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE scheduled_tasks SET status = 'completed' WHERE id = ?",
                (task_id,),
            )


class SchedulerRunner:
    def __init__(self, store: SchedulerStore, event_bus: EventBus):
        self.store = store
        self.event_bus = event_bus
        self._task: asyncio.Task | None = None

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def _loop(self):
        while True:
            try:
                due_tasks = self.store.poll_due_tasks()
                for task in due_tasks:
                    event = ScheduledTaskEvent(
                        action=task["action"],
                        payload=task["payload"],
                    )
                    await self.event_bus.publish(event)
                    self.store.mark_completed(task["id"])
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"SchedulerRunner error: {e}", exc_info=True)
            await asyncio.sleep(60)
