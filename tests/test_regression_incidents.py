from __future__ import annotations

import sqlite3

from navi.evolution import EvolutionLedger


def test_evolution_ledger_uses_latest_run_id_schema(tmp_path):
    EvolutionLedger(tmp_path)

    with sqlite3.connect(tmp_path / "evolution.db") as conn:
        columns = {row[1] for row in conn.execute("pragma table_info(evolution_events)").fetchall()}

    assert "run_id" in columns
    assert "task_id" not in columns
