from __future__ import annotations

import hashlib
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import check_schema_version, connect, write_schema_version
from .loop_contracts import LockMode, MergeResult, MergeStatus, WorkspaceLock
from .paths import db_paths
from .schema import Column, Table, assert_schema_exact

WORKSPACE_STORE_SCHEMA_VERSION = 1
_IGNORED_WORKSPACE_NAMES = {
    ".agents",
    ".claude",
    ".coverage",
    ".git",
    ".mypy_cache",
    ".navi",
    ".opencode",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "__pycache__",
    "dist",
    "htmlcov",
    "node_modules",
    ".venv",
    "venv",
    "workspace_shadows",
    "workspace_conflicts",
}


@dataclass(frozen=True)
class WorkspaceFileDigest:
    path: str
    sha256: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True)
class WorkspaceFingerprint:
    root: str
    digest: str
    files: tuple[WorkspaceFileDigest, ...]

    def hash_for(self, path: str) -> str | None:
        for item in self.files:
            if item.path == path:
                return item.sha256
        return None

    def paths(self) -> set[str]:
        return {item.path for item in self.files}

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "digest": self.digest,
            "files": [item.to_dict() for item in self.files],
        }


@dataclass(frozen=True)
class ShadowWorkspace:
    run_id: str
    real_workspace: str
    baseline_workspace: str
    shadow_workspace: str
    baseline_fingerprint: WorkspaceFingerprint

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "real_workspace": self.real_workspace,
            "baseline_workspace": self.baseline_workspace,
            "shadow_workspace": self.shadow_workspace,
            "baseline_fingerprint": self.baseline_fingerprint.to_dict(),
        }

    def to_facts(self) -> dict[str, Any]:
        """Project durable workspace evidence without embedding every file hash."""
        return {
            "run_id": self.run_id,
            "real_workspace": self.real_workspace,
            "baseline_workspace": self.baseline_workspace,
            "shadow_workspace": self.shadow_workspace,
            "baseline_fingerprint": {
                "root": self.baseline_fingerprint.root,
                "digest": self.baseline_fingerprint.digest,
                "file_count": len(self.baseline_fingerprint.files),
            },
        }


@dataclass(frozen=True)
class ShadowWorkspaceRecord:
    run_id: str
    real_workspace: str
    baseline_workspace: str
    shadow_workspace: str
    status: str
    created_at: float
    updated_at: float

    def to_shadow(self) -> ShadowWorkspace:
        return ShadowWorkspace(
            run_id=self.run_id,
            real_workspace=self.real_workspace,
            baseline_workspace=self.baseline_workspace,
            shadow_workspace=self.shadow_workspace,
            baseline_fingerprint=fingerprint_workspace(Path(self.baseline_workspace)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "real_workspace": self.real_workspace,
            "baseline_workspace": self.baseline_workspace,
            "shadow_workspace": self.shadow_workspace,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class LockAcquireResult:
    acquired: bool
    lock: WorkspaceLock
    conflicts: tuple[WorkspaceLock, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "acquired": self.acquired,
            "lock": self.lock.to_dict(),
            "conflicts": [lock.to_dict() for lock in self.conflicts],
        }


def fingerprint_workspace(root: Path) -> WorkspaceFingerprint:
    resolved = root.expanduser().resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise ValueError("workspace root must be an existing directory")
    files = tuple(
        WorkspaceFileDigest(path=rel_path, sha256=_hash_path(path))
        for rel_path, path in _iter_workspace_files(resolved)
    )
    digest = hashlib.sha256()
    for item in files:
        digest.update(item.path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.sha256.encode("ascii"))
        digest.update(b"\0")
    return WorkspaceFingerprint(root=str(resolved), digest=digest.hexdigest(), files=files)


class ShadowWorkspaceManager:
    def __init__(self, home: Path):
        self.home = home
        self.home.mkdir(parents=True, exist_ok=True)
        self.db_path = db_paths(home).workspaces
        self.shadow_root = home / "workspace_shadows"
        self.conflict_root = home / "workspace_conflicts"
        self._init_db()

    def _init_db(self) -> None:
        with connect(self.db_path) as conn:
            check_schema_version(conn, "workspaces", WORKSPACE_STORE_SCHEMA_VERSION)
            conn.execute(SHADOW_WORKSPACES_TABLE.ddl)
            assert_schema_exact(conn, SHADOW_WORKSPACES_TABLE)
            write_schema_version(conn, "workspaces", WORKSPACE_STORE_SCHEMA_VERSION)

    def create_shadow(self, *, run_id: str, workspace: Path) -> ShadowWorkspace:
        if not run_id.strip():
            raise ValueError("run_id is required")
        real = workspace.expanduser().resolve()
        if not real.exists() or not real.is_dir():
            raise ValueError("workspace must be an existing directory")
        root = self.shadow_root / run_id
        baseline = root / "baseline"
        shadow = root / "shadow"
        if root.exists():
            existing = self.get_shadow(run_id)
            if existing is not None and existing.status == "active":
                return existing.to_shadow()
            raise FileExistsError(f"shadow workspace already exists for run {run_id}")
        root.mkdir(parents=True, exist_ok=False)
        shutil.copytree(real, baseline, symlinks=True, ignore=_copy_ignore)
        shutil.copytree(real, shadow, symlinks=True, ignore=_copy_ignore)
        now = time.time()
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO shadow_workspaces(
                    run_id, real_workspace, baseline_workspace, shadow_workspace,
                    status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    real_workspace = excluded.real_workspace,
                    baseline_workspace = excluded.baseline_workspace,
                    shadow_workspace = excluded.shadow_workspace,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (run_id, str(real), str(baseline), str(shadow), "active", now, now),
            )
        return ShadowWorkspace(
            run_id=run_id,
            real_workspace=str(real),
            baseline_workspace=str(baseline),
            shadow_workspace=str(shadow),
            baseline_fingerprint=fingerprint_workspace(baseline),
        )

    def get_shadow(self, run_id: str) -> ShadowWorkspaceRecord | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                f"SELECT {SHADOW_WORKSPACES_TABLE.select_list} FROM shadow_workspaces WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return ShadowWorkspaceRecord(*row) if row else None

    def list_shadows(self, *, status: str = "", limit: int = 100) -> tuple[ShadowWorkspaceRecord, ...]:
        if status:
            query = (
                f"SELECT {SHADOW_WORKSPACES_TABLE.select_list} FROM shadow_workspaces "
                "WHERE status = ? ORDER BY updated_at DESC LIMIT ?"
            )
            params: tuple[Any, ...] = (status, limit)
        else:
            query = (
                f"SELECT {SHADOW_WORKSPACES_TABLE.select_list} FROM shadow_workspaces "
                "ORDER BY updated_at DESC LIMIT ?"
            )
            params = (limit,)
        with connect(self.db_path) as conn:
            rows = conn.execute(query, params).fetchall()
        return tuple(ShadowWorkspaceRecord(*row) for row in rows)

    def merge_run(self, run_id: str) -> MergeResult:
        record = self.get_shadow(run_id)
        if record is None:
            raise KeyError(f"shadow workspace not found: {run_id}")
        result = self.merge_back(record.to_shadow())
        status = "conflicted" if result.status == MergeStatus.CONFLICTED else "merged"
        self._set_status(run_id, status)
        if status == "merged":
            self._remove_shadow_artifacts(record)
        return result

    def discard_run(self, run_id: str) -> bool:
        record = self.get_shadow(run_id)
        if record is None:
            return False
        self._remove_shadow_artifacts(record)
        self._set_status(run_id, "discarded")
        return True

    def purge_terminal_artifacts(self) -> dict[str, int]:
        """Remove retained files for terminal shadows while preserving their audit rows."""
        removed = 0
        missing = 0
        for record in self.list_shadows(limit=100_000):
            if record.status not in {"merged", "discarded"}:
                continue
            root = Path(record.shadow_workspace).parent
            if not root.exists():
                missing += 1
                continue
            self._remove_shadow_artifacts(record)
            removed += 1
        return {"removed": removed, "already_missing": missing}

    @staticmethod
    def _remove_shadow_artifacts(record: ShadowWorkspaceRecord) -> None:
        root = Path(record.shadow_workspace).parent
        if root.exists():
            shutil.rmtree(root)

    def merge_back(self, shadow: ShadowWorkspace) -> MergeResult:
        real = Path(shadow.real_workspace)
        baseline = Path(shadow.baseline_workspace)
        shadow_root = Path(shadow.shadow_workspace)
        baseline_fp = fingerprint_workspace(baseline)
        shadow_fp = fingerprint_workspace(shadow_root)
        current_fp = fingerprint_workspace(real)
        candidate_paths = sorted(baseline_fp.paths() | shadow_fp.paths() | current_fp.paths())
        agent_changed = [
            path
            for path in candidate_paths
            if baseline_fp.hash_for(path) != shadow_fp.hash_for(path)
        ]
        conflicts = tuple(
            path
            for path in agent_changed
            if _merge_conflicts(
                baseline_hash=baseline_fp.hash_for(path),
                shadow_hash=shadow_fp.hash_for(path),
                current_hash=current_fp.hash_for(path),
            )
        )
        if conflicts:
            artifact_dir = self._write_conflicts(
                run_id=shadow.run_id,
                conflicts=conflicts,
                real=real,
                shadow=shadow_root,
            )
            return MergeResult(
                status=MergeStatus.CONFLICTED,
                conflicts=conflicts,
                artifact_path=str(artifact_dir),
            )
        applied = False
        backup_dir = self.shadow_root / shadow.run_id / "merge_backup"
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        missing = _backup_real_files(real, agent_changed, backup_dir)
        try:
            for path in agent_changed:
                current_hash = current_fp.hash_for(path)
                shadow_hash = shadow_fp.hash_for(path)
                if current_hash == shadow_hash:
                    continue
                _apply_shadow_file(shadow_root / path, real / path)
                applied = True
        except Exception:
            _restore_real_files(real, agent_changed, backup_dir, missing=missing)
            raise
        finally:
            if backup_dir.exists():
                shutil.rmtree(backup_dir)
        return MergeResult(status=MergeStatus.CLEAN if applied else MergeStatus.NO_OP)

    def _set_status(self, run_id: str, status: str) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE shadow_workspaces SET status = ?, updated_at = ? WHERE run_id = ?",
                (status, time.time(), run_id),
            )

    def _write_conflicts(
        self,
        *,
        run_id: str,
        conflicts: tuple[str, ...],
        real: Path,
        shadow: Path,
    ) -> Path:
        artifact_dir = self.conflict_root / run_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        for rel_path in conflicts:
            artifact = artifact_dir / rel_path
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text(
                _conflict_text(real / rel_path, shadow / rel_path),
                encoding="utf-8",
            )
        return artifact_dir


class WorkspaceLockStore:
    def __init__(self, home: Path):
        self.home = home
        self.home.mkdir(parents=True, exist_ok=True)
        self.db_path = db_paths(home).workspace_locks
        self._init_db()

    def _init_db(self) -> None:
        with connect(self.db_path) as conn:
            check_schema_version(conn, "workspace_locks", WORKSPACE_STORE_SCHEMA_VERSION)
            conn.execute(WORKSPACE_LOCKS_TABLE.ddl)
            assert_schema_exact(conn, WORKSPACE_LOCKS_TABLE)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_workspace_locks_resource ON workspace_locks(resource)"
            )
            conn.execute("DROP INDEX IF EXISTS idx_workspace_locks_owner_resource")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_workspace_locks_owner ON workspace_locks(owner_run_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_workspace_locks_expiry ON workspace_locks(lease_expiry)"
            )
            write_schema_version(conn, "workspace_locks", WORKSPACE_STORE_SCHEMA_VERSION)

    def acquire(
        self,
        *,
        owner_run_id: str,
        resource: str,
        mode: LockMode | str = LockMode.WRITE,
        ttl_seconds: float = 900,
    ) -> LockAcquireResult:
        if not owner_run_id.strip():
            raise ValueError("owner_run_id is required")
        if not resource.strip():
            raise ValueError("resource is required")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        now = time.time()
        lock = WorkspaceLock(
            owner_run_id=owner_run_id,
            resource=resource,
            mode=mode,
            lease_expiry=now + ttl_seconds,
        )
        with connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM workspace_locks WHERE lease_expiry <= ?", (now,))
            rows = conn.execute(
                """
                SELECT owner_run_id, resource, mode, lease_expiry
                FROM workspace_locks
                WHERE lease_expiry > ? AND resource = ?
                ORDER BY resource, lease_expiry ASC
                """,
                (now, resource),
            ).fetchall()
            conflicts = tuple(
                existing
                for existing in (WorkspaceLock(*row) for row in rows)
                if existing.conflicts_with(lock, now=now)
            )
            if conflicts:
                return LockAcquireResult(acquired=False, lock=lock, conflicts=conflicts)
            existing = conn.execute(
                "SELECT id FROM workspace_locks WHERE owner_run_id = ? AND resource = ?",
                (owner_run_id, resource),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO workspace_locks(id, owner_run_id, resource, mode, lease_expiry, acquired_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (uuid.uuid4().hex, owner_run_id, resource, str(mode), lock.lease_expiry, now),
                )
            else:
                conn.execute(
                    """
                    UPDATE workspace_locks
                    SET mode = ?, lease_expiry = ?, acquired_at = ?
                    WHERE owner_run_id = ? AND resource = ?
                    """,
                    (str(mode), lock.lease_expiry, now, owner_run_id, resource),
                )
        return LockAcquireResult(acquired=True, lock=lock)

    def release(self, *, owner_run_id: str, resource: str = "") -> int:
        with connect(self.db_path) as conn:
            if resource:
                cursor = conn.execute(
                    "DELETE FROM workspace_locks WHERE owner_run_id = ? AND resource = ?",
                    (owner_run_id, resource),
                )
            else:
                cursor = conn.execute(
                    "DELETE FROM workspace_locks WHERE owner_run_id = ?",
                    (owner_run_id,),
                )
            return int(cursor.rowcount or 0)

    def purge_expired(self, *, now: float | None = None) -> int:
        current = time.time() if now is None else now
        with connect(self.db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM workspace_locks WHERE lease_expiry <= ?",
                (current,),
            )
            return int(cursor.rowcount or 0)

    def list_active(
        self,
        *,
        resource: str = "",
        now: float | None = None,
    ) -> tuple[WorkspaceLock, ...]:
        current = time.time() if now is None else now
        if resource:
            query = """
                SELECT owner_run_id, resource, mode, lease_expiry
                FROM workspace_locks
                WHERE lease_expiry > ? AND resource = ?
                ORDER BY resource, lease_expiry ASC
            """
            params: tuple[Any, ...] = (current, resource)
        else:
            query = """
                SELECT owner_run_id, resource, mode, lease_expiry
                FROM workspace_locks
                WHERE lease_expiry > ?
                ORDER BY resource, lease_expiry ASC
            """
            params = (current,)
        with connect(self.db_path) as conn:
            rows = conn.execute(query, params).fetchall()
        return tuple(WorkspaceLock(*row) for row in rows)


def _copy_ignore(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if _ignored_workspace_name(name)}


def _ignored_workspace_name(name: str) -> bool:
    return name in _IGNORED_WORKSPACE_NAMES or name.endswith(".egg-info")


def _iter_workspace_files(root: Path) -> tuple[tuple[str, Path], ...]:
    items: list[tuple[str, Path]] = []
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(
            name
            for name in dirs
            if not _ignored_workspace_name(name)
        )
        for name in sorted(files):
            if _ignored_workspace_name(name):
                continue
            path = Path(current) / name
            rel = path.relative_to(root).as_posix()
            items.append((rel, path))
    return tuple(items)


def _hash_path(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_symlink():
        digest.update(b"symlink\0")
        digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
        return digest.hexdigest()
    digest.update(b"file\0")
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _merge_conflicts(
    *,
    baseline_hash: str | None,
    shadow_hash: str | None,
    current_hash: str | None,
) -> bool:
    if baseline_hash == shadow_hash:
        return False
    if current_hash == baseline_hash:
        return False
    if current_hash == shadow_hash:
        return False
    return True


def _apply_shadow_file(source: Path, dest: Path) -> None:
    if not source.exists() and not source.is_symlink():
        if dest.exists() or dest.is_symlink():
            dest.unlink()
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_name(f".{dest.name}.navi-tmp-{uuid.uuid4().hex}")
    if source.is_symlink():
        target = os.readlink(source)
        os.symlink(target, temp)
    else:
        shutil.copy2(source, temp)
    temp.replace(dest)


def _backup_real_files(real: Path, paths: list[str], backup_dir: Path) -> set[str]:
    missing: set[str] = set()
    for rel_path in paths:
        source = real / rel_path
        if not source.exists() and not source.is_symlink():
            missing.add(rel_path)
            continue
        backup = backup_dir / rel_path
        backup.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            os.symlink(os.readlink(source), backup)
        else:
            shutil.copy2(source, backup)
    return missing


def _restore_real_files(
    real: Path,
    paths: list[str],
    backup_dir: Path,
    *,
    missing: set[str],
) -> None:
    for rel_path in paths:
        dest = real / rel_path
        _remove_path(dest)
        if rel_path in missing:
            continue
        backup = backup_dir / rel_path
        if backup.is_symlink():
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(os.readlink(backup), dest)
        elif backup.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, dest)


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _conflict_text(current: Path, shadow: Path) -> str:
    current_text = _read_conflict_side(current)
    shadow_text = _read_conflict_side(shadow)
    return (
        "<<<<<<< CURRENT\n"
        f"{current_text}"
        "\n=======\n"
        f"{shadow_text}"
        "\n>>>>>>> AGENT\n"
    )


def _read_conflict_side(path: Path) -> str:
    if not path.exists() and not path.is_symlink():
        return "[missing]"
    if path.is_symlink():
        return f"[symlink] {os.readlink(path)}"
    return path.read_bytes().decode("utf-8", errors="replace")


WORKSPACE_LOCKS_TABLE = Table(
    "workspace_locks",
    [
        Column("id", "TEXT", primary_key=True),
        Column("owner_run_id", "TEXT", nullable=False),
        Column("resource", "TEXT", nullable=False),
        Column("mode", "TEXT", nullable=False),
        Column("lease_expiry", "REAL", nullable=False),
        Column("acquired_at", "REAL", nullable=False),
    ],
)

SHADOW_WORKSPACES_TABLE = Table(
    "shadow_workspaces",
    [
        Column("run_id", "TEXT", primary_key=True),
        Column("real_workspace", "TEXT", nullable=False),
        Column("baseline_workspace", "TEXT", nullable=False),
        Column("shadow_workspace", "TEXT", nullable=False),
        Column("status", "TEXT", nullable=False),
        Column("created_at", "REAL", nullable=False),
        Column("updated_at", "REAL", nullable=False),
    ],
)
