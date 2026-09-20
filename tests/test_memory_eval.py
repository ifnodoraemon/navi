"""Tests for the memory regression eval gate (memory_eval.py)."""

from __future__ import annotations

import json
from pathlib import Path

from navi.memory_eval import (
    MEMORY_PARAMETER_SET,
    run_memory_regression_eval,
)


def test_memory_parameter_set_covers_incident_parameters() -> None:
    assert "tf_max_extra_boost" in MEMORY_PARAMETER_SET
    assert "recall_lexical_pool_multiplier" in MEMORY_PARAMETER_SET
    assert "context_recent_base_no_query" in MEMORY_PARAMETER_SET
    assert "memory_llm_rerank_threshold" in MEMORY_PARAMETER_SET
    assert "memory_regression_gate_threshold" not in MEMORY_PARAMETER_SET


def test_gate_passes_benign_candidate(tmp_path: Path) -> None:
    report = run_memory_regression_eval(
        tmp_path,
        target_id="cue_weight_coverage",
        candidate_value=0.65,
    )
    assert report["policy"] == "memory_regression_gate_v1"
    assert report["baseline_hit_rate"] == 1.0
    assert report["hit_rate"] >= report["threshold"]
    assert report["blocked"] is False
    assert report["failed_queries"] == []


def test_gate_blocks_the_2026_09_contamination_value(tmp_path: Path) -> None:
    report = run_memory_regression_eval(
        tmp_path,
        target_id="tf_max_extra_boost",
        candidate_value=9643190.0,
    )
    assert report["baseline_hit_rate"] == 1.0
    assert report["hit_rate"] < report["threshold"]
    assert report["blocked"] is True
    assert report["failed_queries"]
    json.dumps(report, ensure_ascii=False)


def test_gate_report_is_hermetic_to_production_home(tmp_path: Path) -> None:
    """The production home's memory plane must stay untouched by the gate."""
    from navi.dynamic_parameters import DynamicParameterRegistry
    from navi.memory.store import MemoryStore

    MemoryStore(tmp_path).add_item(
        memory_type="fact",
        content="production-side item that must not leak into the fixture",
        source="test",
        status="active",
        reason="test",
        provenance="test",
    )
    report = run_memory_regression_eval(
        tmp_path,
        target_id="cue_weight_jaccard",
        candidate_value=0.30,
    )
    assert report["blocked"] is False
    listed = MemoryStore(tmp_path).list_items()
    assert len(listed) == 1
    assert DynamicParameterRegistry(tmp_path).get("cue_weight_jaccard") != 0.30
