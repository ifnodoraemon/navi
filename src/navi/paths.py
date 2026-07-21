"""Centralized path management for Navi.

All filesystem paths used by Navi are derived from :func:`navi_home`, which
respects the ``NAVI_HOME`` environment variable (default: ``./.navi``).

The :func:`db_paths` helper returns a :class:`DbPaths` dataclass with every
SQLite database path, eliminating the scattered ``home / "runs.db"`` pattern
found across 13 files.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def navi_home() -> Path:
    raw = os.environ.get("NAVI_HOME")
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.cwd() / ".navi").resolve()


def ensure_home() -> Path:
    home = navi_home()
    home.mkdir(parents=True, exist_ok=True)
    return home


@dataclass(frozen=True, slots=True)
class DbPaths:
    """All SQLite database paths, derived from the Navi home directory."""

    runs: Path
    goals: Path
    traces: Path
    evolution: Path
    memory: Path
    graph: Path
    loop_runs: Path
    workspace_locks: Path
    workspaces: Path
    vault: Path
    resource_ledger: Path
    personal_resources: Path


def db_paths(home: Path) -> DbPaths:
    """Return all database paths for the given Navi home directory."""
    return DbPaths(
        runs=home / "runs.db",
        goals=home / "goals.db",
        traces=home / "traces.db",
        evolution=home / "evolution.db",
        memory=home / "memory.db",
        graph=home / "graph.db",
        loop_runs=home / "loop_runs.db",
        workspace_locks=home / "workspace_locks.db",
        workspaces=home / "workspaces.db",
        vault=home / "vault.db",
        resource_ledger=home / "resource_ledger.db",
        personal_resources=home / "personal_resources.db",
    )
