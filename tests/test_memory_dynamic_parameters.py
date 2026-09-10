from __future__ import annotations

import json
from pathlib import Path
import time

from navi.evolution_targets import EvolutionTargetAdapterRegistry
from navi.memory.models import MemoryItem
from navi.memory.provider import SQLiteMemoryProvider
from navi.memory.store import DEFAULT_MEMORY_PARAMETERS, MemoryStore
from navi.core_tools.memory import _memory_parameters, _memory_record_activation


def test_dynamic_parameters_default_initialization(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    params = store.list_parameters()
    for name, default_val in DEFAULT_MEMORY_PARAMETERS.items():
        assert name in params
        assert params[name]["value"] == default_val
        assert store.get_parameter(name) == default_val


def test_dynamic_parameter_runtime_modification(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    initial_cov = store.get_parameter("cue_weight_coverage")
    assert initial_cov == 0.60

    store.set_parameter("cue_weight_coverage", 0.80, reason="test_boost")
    assert store.get_parameter("cue_weight_coverage") == 0.80

    entry = store.get_parameter_entry("cue_weight_coverage")
    assert entry is not None
    assert entry["value"] == 0.80
    assert entry["metadata"]["reason"] == "test_boost"

    # Reload store from disk to ensure SQLite persistence
    reloaded_store = MemoryStore(tmp_path)
    assert reloaded_store.get_parameter("cue_weight_coverage") == 0.80


def test_dynamic_hebbian_plasticity_on_activation(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    item = store.add_item(
        "fact",
        "Python is dynamically typed and supports functional programming",
        source="test",
        status="proposed",
        confidence=0.5,
        reason="initial",
        provenance="test",
    )

    query = "Python is dynamically typed"
    recalls = store.recall(query, limit=5)
    assert any(r.item.id == item.id for r in recalls)

    initial_seq = store.get_parameter("cue_weight_sequence")
    # Activate with the query
    store.record_activation(
        item.id,
        reason="used in coding task",
        provenance="planner",
        query=query,
    )

    # Hebbian adaptation updates cue weights based on the matching signals
    updated_cov = store.get_parameter("cue_weight_coverage")
    updated_jac = store.get_parameter("cue_weight_jaccard")
    updated_seq = store.get_parameter("cue_weight_sequence")
    # Weights sum to 1.0 continuously
    assert abs((updated_cov + updated_jac + updated_seq) - 1.0) < 1e-3


def test_zero_else_in_memory_store() -> None:
    store_file = Path("src/navi/memory/store.py").resolve()
    content = store_file.read_text(encoding="utf-8")
    lines = content.splitlines()
    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        assert not stripped.startswith("else:"), f"Found 'else:' at {store_file}:{idx}: {line}"
        assert not stripped.startswith("elif "), f"Found 'elif' at {store_file}:{idx}: {line}"


def test_memory_parameters_core_tool(tmp_path: Path) -> None:
    list_res = _memory_parameters(tmp_path, {"action": "list"})
    assert list_res.ok
    assert "parameters" in list_res.facts

    get_res = _memory_parameters(tmp_path, {"action": "get", "name": "cue_weight_coverage"})
    assert get_res.ok
    assert get_res.facts["parameter"]["value"] == 0.60

    set_res = _memory_parameters(
        tmp_path,
        {"action": "set", "name": "cue_weight_coverage", "value": 0.75, "reason": "operator_tune"},
    )
    assert set_res.ok
    assert set_res.facts["parameter"]["value"] == 0.75

    get_after = _memory_parameters(tmp_path, {"action": "get", "name": "cue_weight_coverage"})
    assert get_after.ok
    assert get_after.facts["parameter"]["value"] == 0.75


def test_evolution_target_memory_parameter(tmp_path: Path) -> None:
    registry = EvolutionTargetAdapterRegistry(tmp_path)
    adapter = registry.get("memory_parameter")
    assert adapter is not None
    assert adapter.descriptor.target_type == "memory_parameter"

    before_json = adapter.read("cue_weight_jaccard")
    assert before_json != ""
    before_data = json.loads(before_json)
    assert before_data["value"] == 0.25

    candidate = json.dumps({"value": 0.40, "reason": "evolution_experiment"})
    adapter.apply("cue_weight_jaccard", candidate)

    after_json = adapter.read("cue_weight_jaccard")
    after_data = json.loads(after_json)
    assert after_data["value"] == 0.40

    adapter.rollback("cue_weight_jaccard", before_json)
    restored_json = adapter.read("cue_weight_jaccard")
    restored_data = json.loads(restored_json)
    assert restored_data["value"] == 0.25


def test_dynamic_parameter_registry_standalone(tmp_path: Path) -> None:
    from navi.dynamic_parameters import DynamicParameterRegistry, SYSTEM_DYNAMIC_PARAMETERS

    registry = DynamicParameterRegistry(tmp_path)
    # Default lookup
    assert registry.get("cue_weight_coverage") == 0.60
    assert registry.get("provider_retry_after_seconds") == 15.0
    assert registry.get("nonexistent_param", 42.0) == 42.0

    # Set and cache
    registry.set("provider_retry_after_seconds", 30.0, reason="network_congestion")
    assert registry.get("provider_retry_after_seconds") == 30.0

    # Persists across instances
    reloaded = DynamicParameterRegistry(tmp_path)
    assert reloaded.get("provider_retry_after_seconds") == 30.0

    # List all
    all_params = registry.list_all()
    assert "cue_weight_coverage" in all_params
    assert all_params["provider_retry_after_seconds"]["value"] == 30.0
    assert all_params["provider_retry_after_seconds"]["metadata"]["reason"] == "network_congestion"


def test_evolution_target_dynamic_and_system_parameter(tmp_path: Path) -> None:
    registry = EvolutionTargetAdapterRegistry(tmp_path)
    for target_type in ("dynamic_parameter", "system_parameter"):
        adapter = registry.get(target_type)
        assert adapter is not None
        assert adapter.descriptor.target_type == "memory_parameter"

        before = adapter.read("provider_retry_after_seconds")
        candidate = json.dumps({"value": 25.0, "reason": "evo_tune"})
        adapter.apply("provider_retry_after_seconds", candidate)
        after = json.loads(adapter.read("provider_retry_after_seconds"))
        assert after["value"] == 25.0
        adapter.rollback("provider_retry_after_seconds", before)

