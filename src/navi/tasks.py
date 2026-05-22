from __future__ import annotations

import secrets
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .db import connect


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    status: str
    created_at: float
    updated_at: float
    kind: str = "manual"
    prompt: str = ""
    source: str = "local"
    peer_id: str = ""
    sender_id: str = ""
    provider: str = ""
    workspace: str = ""
    autonomy_level: str = "L2"
    trust_rule_id: str = ""
    why_now: str = ""
    plan_summary: str = ""
    result_summary: str = ""
    error: str = ""


@dataclass(frozen=True)
class Approval:
    id: str
    task_id: str
    code: str
    action: str
    peer_id: str
    sender_id: str
    status: str
    expires_at: float
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class Watch:
    id: str
    cron: str
    prompt: str
    peer_id: str
    sender_id: str
    enabled: bool
    next_run_at: float
    last_run_at: float
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class ExecutionLog:
    id: str
    task_id: str
    provider: str
    phase: str
    command: str
    stdout: str
    stderr: str
    exit_code: int
    started_at: float
    ended_at: float


@dataclass(frozen=True)
class ToolCallLog:
    id: str
    tool: str
    args_json: str
    ok: bool
    facts_json: str
    error: str
    started_at: float
    ended_at: float


TASK_COLUMNS = {
    "kind": "TEXT NOT NULL DEFAULT 'manual'",
    "prompt": "TEXT NOT NULL DEFAULT ''",
    "source": "TEXT NOT NULL DEFAULT 'local'",
    "peer_id": "TEXT NOT NULL DEFAULT ''",
    "sender_id": "TEXT NOT NULL DEFAULT ''",
    "provider": "TEXT NOT NULL DEFAULT ''",
    "workspace": "TEXT NOT NULL DEFAULT ''",
    "autonomy_level": "TEXT NOT NULL DEFAULT 'L2'",
    "trust_rule_id": "TEXT NOT NULL DEFAULT ''",
    "why_now": "TEXT NOT NULL DEFAULT ''",
    "plan_summary": "TEXT NOT NULL DEFAULT ''",
    "result_summary": "TEXT NOT NULL DEFAULT ''",
    "error": "TEXT NOT NULL DEFAULT ''",
}


class TaskStore:
    def __init__(self, home: Path):
        self.home = home
        self.home.mkdir(parents=True, exist_ok=True)
        self.db_path = home / "tasks.db"
        self._init_db()

    def _init_db(self) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            existing = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
            for name, ddl in TASK_COLUMNS.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {ddl}")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    code TEXT NOT NULL UNIQUE,
                    action TEXT NOT NULL,
                    peer_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS watches (
                    id TEXT PRIMARY KEY,
                    cron TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    peer_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    next_run_at REAL NOT NULL,
                    last_run_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS execution_logs (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    command TEXT NOT NULL,
                    stdout TEXT NOT NULL,
                    stderr TEXT NOT NULL,
                    exit_code INTEGER NOT NULL,
                    started_at REAL NOT NULL,
                    ended_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tool_call_logs (
                    id TEXT PRIMARY KEY,
                    tool TEXT NOT NULL,
                    args_json TEXT NOT NULL,
                    ok INTEGER NOT NULL,
                    facts_json TEXT NOT NULL,
                    error TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    ended_at REAL NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_approvals_code ON approvals(code)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_watches_next ON watches(enabled, next_run_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_call_logs_tool ON tool_call_logs(tool, started_at)")

    def create(
        self,
        title: str,
        *,
        kind: str = "manual",
        prompt: str = "",
        source: str = "local",
        peer_id: str = "",
        sender_id: str = "",
        provider: str = "",
        workspace: str | None = None,
        autonomy_level: str = "L2",
        trust_rule_id: str = "",
        why_now: str = "",
        status: str = "pending",
    ) -> Task:
        now = time.time()
        task = Task(
            id=uuid.uuid4().hex,
            title=title,
            status=status,
            created_at=now,
            updated_at=now,
            kind=kind,
            prompt=prompt or title,
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            provider=provider,
            workspace=workspace or str(Path.cwd().resolve()),
            autonomy_level=autonomy_level,
            trust_rule_id=trust_rule_id,
            why_now=why_now,
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO tasks(
                    id, title, status, created_at, updated_at, kind, prompt, source,
                    peer_id, sender_id, provider, workspace, autonomy_level, trust_rule_id, why_now,
                    plan_summary, result_summary, error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.id,
                    task.title,
                    task.status,
                    task.created_at,
                    task.updated_at,
                    task.kind,
                    task.prompt,
                    task.source,
                    task.peer_id,
                    task.sender_id,
                    task.provider,
                    task.workspace,
                    task.autonomy_level,
                    task.trust_rule_id,
                    task.why_now,
                    task.plan_summary,
                    task.result_summary,
                    task.error,
                ),
            )
        return task

    def get(self, task_id: str) -> Task | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT id, title, status, created_at, updated_at, kind, prompt, source,
                       peer_id, sender_id, provider, workspace, autonomy_level, trust_rule_id,
                       why_now, plan_summary, result_summary, error
                FROM tasks WHERE id = ?
                """,
                (task_id,),
            ).fetchone()
        return self._task_from_row(row) if row else None

    def list(self, *, limit: int = 50) -> list[Task]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, title, status, created_at, updated_at, kind, prompt, source,
                       peer_id, sender_id, provider, workspace, autonomy_level, trust_rule_id,
                       why_now, plan_summary, result_summary, error
                FROM tasks ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def list_by_status(self, status: str, *, limit: int = 20) -> list[Task]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, title, status, created_at, updated_at, kind, prompt, source,
                       peer_id, sender_id, provider, workspace, autonomy_level, trust_rule_id,
                       why_now, plan_summary, result_summary, error
                FROM tasks WHERE status = ? ORDER BY updated_at ASC LIMIT ?
                """,
                (status, limit),
            ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def list_by_statuses(self, statuses: list[str], *, limit: int = 60) -> list[Task]:
        if not statuses:
            return []
        placeholders = ", ".join("?" for _ in statuses)
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT id, title, status, created_at, updated_at, kind, prompt, source,
                       peer_id, sender_id, provider, workspace, autonomy_level, trust_rule_id,
                       why_now, plan_summary, result_summary, error
                FROM tasks WHERE status IN ({placeholders}) ORDER BY updated_at ASC LIMIT ?
                """,
                [*statuses, limit],
            ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def update_status(self, task_id: str, status: str) -> Task | None:
        return self.update_task(task_id, status=status)

    def delete_task(self, task_id: str) -> Task | None:
        task = self.get(task_id)
        if task is None:
            return None
        with connect(self.db_path) as conn:
            conn.execute("DELETE FROM approvals WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM execution_logs WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return task

    def update_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        plan_summary: str | None = None,
        result_summary: str | None = None,
        error: str | None = None,
        trust_rule_id: str | None = None,
        autonomy_level: str | None = None,
    ) -> Task | None:
        task = self.get(task_id)
        if task is None:
            return None
        values = {
            "status": task.status if status is None else status,
            "plan_summary": task.plan_summary if plan_summary is None else plan_summary,
            "result_summary": task.result_summary if result_summary is None else result_summary,
            "error": task.error if error is None else error,
            "trust_rule_id": task.trust_rule_id if trust_rule_id is None else trust_rule_id,
            "autonomy_level": task.autonomy_level if autonomy_level is None else autonomy_level,
            "updated_at": time.time(),
        }
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, plan_summary = ?, result_summary = ?, error = ?,
                    trust_rule_id = ?, autonomy_level = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    values["status"],
                    values["plan_summary"],
                    values["result_summary"],
                    values["error"],
                    values["trust_rule_id"],
                    values["autonomy_level"],
                    values["updated_at"],
                    task_id,
                ),
            )
        return self.get(task_id)

    def create_approval(
        self,
        *,
        task_id: str,
        peer_id: str,
        sender_id: str,
        action: str = "execute",
        ttl_seconds: int = 900,
    ) -> Approval:
        now = time.time()
        approval = Approval(
            id=uuid.uuid4().hex,
            task_id=task_id,
            code=self._new_code(),
            action=action,
            peer_id=peer_id,
            sender_id=sender_id,
            status="pending",
            expires_at=now + ttl_seconds,
            created_at=now,
            updated_at=now,
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO approvals(
                    id, task_id, code, action, peer_id, sender_id, status,
                    expires_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval.id,
                    approval.task_id,
                    approval.code,
                    approval.action,
                    approval.peer_id,
                    approval.sender_id,
                    approval.status,
                    approval.expires_at,
                    approval.created_at,
                    approval.updated_at,
                ),
            )
        return approval

    def resolve_approval(self, code: str, sender_id: str, status: str) -> Approval | None:
        approval = self.get_approval(code)
        if approval is None or approval.sender_id != sender_id or approval.status != "pending":
            return None
        now = time.time()
        new_status = "expired" if approval.expires_at < now else status
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE approvals SET status = ?, updated_at = ? WHERE id = ?",
                (new_status, now, approval.id),
            )
        return self.get_approval(code)

    def resolve_task_approval(self, task_id: str, *, sender_id: str, status: str) -> Approval | None:
        approval = self.pending_approval_for_task(task_id, sender_id=sender_id)
        if approval is None:
            return None
        now = time.time()
        new_status = "expired" if approval.expires_at < now else status
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE approvals SET status = ?, updated_at = ? WHERE id = ?",
                (new_status, now, approval.id),
            )
        return self.get_approval(approval.code)

    def pending_approval_for_task(self, task_id: str, *, sender_id: str = "") -> Approval | None:
        now = time.time()
        with connect(self.db_path) as conn:
            if sender_id:
                row = conn.execute(
                    """
                    SELECT id, task_id, code, action, peer_id, sender_id, status,
                           expires_at, created_at, updated_at
                    FROM approvals
                    WHERE task_id = ? AND sender_id = ? AND status = 'pending'
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (task_id, sender_id),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT id, task_id, code, action, peer_id, sender_id, status,
                           expires_at, created_at, updated_at
                    FROM approvals
                    WHERE task_id = ? AND status = 'pending'
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (task_id,),
                ).fetchone()
        approval = Approval(*row) if row else None
        if approval is None:
            return None
        if approval.expires_at < now:
            with connect(self.db_path) as conn:
                conn.execute(
                    "UPDATE approvals SET status = ?, updated_at = ? WHERE id = ?",
                    ("expired", now, approval.id),
                )
            return None
        return approval

    def has_approved_execution(self, task_id: str) -> bool:
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT 1 FROM approvals
                WHERE task_id = ? AND action = 'execute' AND status = 'approved'
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
        return row is not None

    def get_approval(self, code: str) -> Approval | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT id, task_id, code, action, peer_id, sender_id, status,
                       expires_at, created_at, updated_at
                FROM approvals WHERE code = ?
                """,
                (code,),
            ).fetchone()
        return Approval(*row) if row else None

    def list_approvals(self, *, limit: int = 50) -> list[Approval]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, task_id, code, action, peer_id, sender_id, status,
                       expires_at, created_at, updated_at
                FROM approvals ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [Approval(*row) for row in rows]

    def create_watch(self, *, cron: str, prompt: str, peer_id: str, sender_id: str, next_run_at: float) -> Watch:
        now = time.time()
        watch = Watch(
            id=uuid.uuid4().hex,
            cron=cron,
            prompt=prompt,
            peer_id=peer_id,
            sender_id=sender_id,
            enabled=True,
            next_run_at=next_run_at,
            last_run_at=0.0,
            created_at=now,
            updated_at=now,
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO watches(
                    id, cron, prompt, peer_id, sender_id, enabled,
                    next_run_at, last_run_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    watch.id,
                    watch.cron,
                    watch.prompt,
                    watch.peer_id,
                    watch.sender_id,
                    int(watch.enabled),
                    watch.next_run_at,
                    watch.last_run_at,
                    watch.created_at,
                    watch.updated_at,
                ),
            )
        return watch

    def list_watches(self, *, limit: int = 50) -> list[Watch]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, cron, prompt, peer_id, sender_id, enabled,
                       next_run_at, last_run_at, created_at, updated_at
                FROM watches ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._watch_from_row(row) for row in rows]

    def get_watch(self, watch_id: str) -> Watch | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT id, cron, prompt, peer_id, sender_id, enabled,
                       next_run_at, last_run_at, created_at, updated_at
                FROM watches WHERE id = ?
                """,
                (watch_id,),
            ).fetchone()
        return self._watch_from_row(row) if row else None

    def due_watches(self, now: float) -> list[Watch]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, cron, prompt, peer_id, sender_id, enabled,
                       next_run_at, last_run_at, created_at, updated_at
                FROM watches WHERE enabled = 1 AND next_run_at <= ? ORDER BY next_run_at ASC
                """,
                (now,),
            ).fetchall()
        return [self._watch_from_row(row) for row in rows]

    def mark_watch_run(self, watch_id: str, *, last_run_at: float, next_run_at: float) -> Watch | None:
        now = time.time()
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE watches SET last_run_at = ?, next_run_at = ?, updated_at = ? WHERE id = ?",
                (last_run_at, next_run_at, now, watch_id),
            )
        return self.get_watch(watch_id)

    def delete_watch(self, watch_id: str) -> Watch | None:
        watch = self.get_watch(watch_id)
        if watch is None:
            return None
        with connect(self.db_path) as conn:
            conn.execute("DELETE FROM watches WHERE id = ?", (watch_id,))
        return watch

    def add_execution_log(
        self,
        *,
        task_id: str,
        provider: str,
        phase: str,
        command: str,
        stdout: str,
        stderr: str,
        exit_code: int,
        started_at: float,
        ended_at: float,
    ) -> ExecutionLog:
        log = ExecutionLog(
            id=uuid.uuid4().hex,
            task_id=task_id,
            provider=provider,
            phase=phase,
            command=command,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            started_at=started_at,
            ended_at=ended_at,
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO execution_logs(
                    id, task_id, provider, phase, command, stdout, stderr,
                    exit_code, started_at, ended_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    log.id,
                    log.task_id,
                    log.provider,
                    log.phase,
                    log.command,
                    log.stdout,
                    log.stderr,
                    log.exit_code,
                    log.started_at,
                    log.ended_at,
                ),
            )
        return log

    def add_tool_call_log(
        self,
        *,
        tool: str,
        args_json: str,
        ok: bool,
        facts_json: str,
        error: str,
        started_at: float,
        ended_at: float,
    ) -> ToolCallLog:
        log = ToolCallLog(
            id=uuid.uuid4().hex,
            tool=tool,
            args_json=args_json,
            ok=ok,
            facts_json=facts_json,
            error=error,
            started_at=started_at,
            ended_at=ended_at,
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO tool_call_logs(
                    id, tool, args_json, ok, facts_json, error, started_at, ended_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    log.id,
                    log.tool,
                    log.args_json,
                    int(log.ok),
                    log.facts_json,
                    log.error,
                    log.started_at,
                    log.ended_at,
                ),
            )
        return log

    def list_execution_logs(self, task_id: str | None = None, *, limit: int = 50) -> list[ExecutionLog]:
        with connect(self.db_path) as conn:
            if task_id:
                rows = conn.execute(
                    """
                    SELECT id, task_id, provider, phase, command, stdout, stderr,
                           exit_code, started_at, ended_at
                    FROM execution_logs WHERE task_id = ? ORDER BY started_at DESC LIMIT ?
                    """,
                    (task_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, task_id, provider, phase, command, stdout, stderr,
                           exit_code, started_at, ended_at
                    FROM execution_logs ORDER BY started_at DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [ExecutionLog(*row) for row in rows]

    def list_tool_call_logs(self, *, limit: int = 50) -> list[ToolCallLog]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, tool, args_json, ok, facts_json, error, started_at, ended_at
                FROM tool_call_logs ORDER BY started_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._tool_call_log_from_row(row) for row in rows]

    @staticmethod
    def _task_from_row(row: tuple) -> Task:
        return Task(*row)

    @staticmethod
    def _watch_from_row(row: tuple) -> Watch:
        values = list(row)
        values[5] = bool(values[5])
        return Watch(*values)

    @staticmethod
    def _tool_call_log_from_row(row: tuple) -> ToolCallLog:
        values = list(row)
        values[3] = bool(values[3])
        return ToolCallLog(*values)

    @staticmethod
    def _new_code() -> str:
        return f"{secrets.randbelow(1_000_000):06d}"
