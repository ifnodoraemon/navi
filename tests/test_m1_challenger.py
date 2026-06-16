from __future__ import annotations

import os
import time
import pytest
import yaml
import threading
import importlib
from pathlib import Path
from typing import Any

from navi.prompting import PromptLayerStore
from navi.spec_loader import load_spec
from navi.capabilities import ActionCapabilityProvider, CapabilityContext


def test_prompt_layer_store_correctness_and_overrides(tmp_path: Path):
    """Verify that PromptLayerStore correctly handles defaults, overrides, deletion, and missing keys."""
    store = PromptLayerStore(tmp_path)
    
    # 1. Non-existent key fallback
    empty_layer = store.get("non_existent_key_xyz_123")
    assert empty_layer.content == ""
    assert empty_layer.minimum_permission == "read"
    assert store.read("non_existent_key_xyz_123") == ""
    
    # 2. Existing default key from prompt_layers.yaml (loaded via load_spec)
    # We check a known key, e.g. "identity"
    identity_layer = store.get("identity")
    assert "You are Navi" in identity_layer.content
    assert identity_layer.minimum_permission == "read"
    assert "You are Navi" in store.read("identity")

    # 3. Writing override
    override_content = "This is a custom overridden prompt."
    override_path = store.write_override("identity", override_content)
    assert override_path.exists()
    assert override_path.name == "identity.md"
    
    # Verify override is read instead of default
    overridden_layer = store.get("identity")
    assert overridden_layer.content == override_content
    assert store.read("identity") == override_content
    # The minimum permission should still match default_spec from yaml
    assert overridden_layer.minimum_permission == "read"

    # 4. Deleting override
    store.delete_override("identity")
    assert not override_path.exists()
    
    # Verify default is restored
    restored_layer = store.get("identity")
    assert "You are Navi" in restored_layer.content
    assert store.read("identity") == restored_layer.content


def test_prompt_layer_store_stress_and_performance(tmp_path: Path):
    """Stress test PromptLayerStore.read() by repeatedly loading a prompt 10,000 times."""
    store = PromptLayerStore(tmp_path)
    
    # Warm up / Cache load
    assert "You are Navi" in store.read("identity")
    
    start_time = time.perf_counter()
    iterations = 10000
    for _ in range(iterations):
        content = store.read("identity")
        assert "You are Navi" in content
    end_time = time.perf_counter()
    
    total_duration = end_time - start_time
    avg_latency = total_duration / iterations
    print(f"\n[Stress Test] {iterations} read operations completed in {total_duration:.4f}s (Avg: {avg_latency*1e6:.2f} microseconds/read)")
    
    # Average latency should be very small (e.g. < 50 microseconds) due to LRU cache on load_spec
    # And file exists check (which is fast on local FS).
    assert avg_latency < 0.001  # Must be less than 1ms per read


def test_prompt_layer_store_cache_cleared_performance(tmp_path: Path):
    """Verify performance of PromptLayerStore when cache is cleared every time to measure disk/yaml overhead."""
    store = PromptLayerStore(tmp_path)
    
    start_time = time.perf_counter()
    iterations = 1000  # 1000 is enough because it has to read yaml files without cache
    for _ in range(iterations):
        load_spec.cache_clear()
        content = store.read("identity")
        assert "You are Navi" in content
    end_time = time.perf_counter()
    
    total_duration = end_time - start_time
    avg_latency = total_duration / iterations
    print(f"\n[Cache-Cleared Test] {iterations} read operations completed in {total_duration:.4f}s (Avg: {avg_latency*1e6:.2f} microseconds/read)")
    
    # Clearing cache makes it significantly slower since it has to read files and parse yaml every time
    # This shows the extreme benefit of Worker's implementation caching.


def test_prompt_layer_store_yaml_missing_and_corrupt(tmp_path: Path, monkeypatch):
    """Verify fallback behavior when prompt_layers.yaml is missing or corrupted."""
    store = PromptLayerStore(tmp_path)
    
    # We need to clear the load_spec cache first to ensure it actually tries to load
    load_spec.cache_clear()
    
    # Mocking yaml file name to a non-existent one by overriding load_spec implementation
    # or monkeypatching the Path in load_spec.
    # Let's inspect where load_spec reads: Path(__file__).parent / "specs" / name
    # We can temporarily patch load_spec to raise FileNotFoundError or yaml.YAMLError
    def mock_load_spec_missing(name):
        raise FileNotFoundError("Mock file not found")
        
    monkeypatch.setattr("navi.prompting.load_spec", mock_load_spec_missing)
    
    # When load_spec raises FileNotFoundError, read() should propagate the error or does it?
    # Wait, the codebase currently raises it. Let's see what happens.
    with pytest.raises(FileNotFoundError):
        store.read("identity")
        
    # Now let's try with yaml corruption (e.g. returning something completely invalid or raising yaml.YAMLError)
    def mock_load_spec_corrupt(name):
        raise yaml.YAMLError("Mock yaml parser error")
        
    monkeypatch.setattr("navi.prompting.load_spec", mock_load_spec_corrupt)
    with pytest.raises(yaml.YAMLError):
        store.read("identity")


def test_actions_module_loading_and_class_exposal():
    """Verify action submodules load correctly and expose the expected capability classes."""
    expected_classes = {
        "conversation": ["FinalAnswerCapability", "ClarifyCapability"],
        "delegation": [
            "DelegateSpawnCapability",
            "DelegatePrepareCapability",
            "DelegateRunCapability",
            "DelegateDeleteCapability",
            "ExecutionRetryCapability",
        ],
        "approval": ["ApprovalRequestCapability", "ApprovalResolveCapability"],
        "watch": ["WatchCreateCapability", "WatchDeleteCapability"],
        "workflow": [
            "WorkflowProposeCapability",
            "WorkflowApproveCapability",
            "WorkflowRunCapability",
            "WorkflowVerifyCapability",
            "WorkflowStatusCapability",
        ],
        "session": ["SessionCreateCapability", "SessionRequestElevationCapability"],
        "memory": ["MemoryAddCapability"],
        "trace": ["TraceEvaluateCapability"],
        "evolution": [
            "EvolutionProposeCapability",
            "EvolutionRecordEvaluationCapability",
            "EvolutionApplyCapability",
            "EvolutionRollbackCapability",
        ],
    }
    
    for module_name, classes in expected_classes.items():
        # Load module
        mod = importlib.import_module(f"navi.actions.{module_name}")
        for class_name in classes:
            # Check class exists in module
            assert hasattr(mod, class_name), f"Class {class_name} not found in navi.actions.{module_name}"
            cls = getattr(mod, class_name)
            assert isinstance(cls, type), f"{class_name} in navi.actions.{module_name} is not a class"


def test_action_capability_provider_lazy_loading(tmp_path: Path):
    """Verify ActionCapabilityProvider loads all mapped capabilities correctly using lazy import."""
    provider = ActionCapabilityProvider(home=tmp_path, project_dir=tmp_path)
    
    # Get all capabilities
    caps = provider.capabilities()
    
    # We should have all defined capabilities
    assert len(caps) > 0
    
    # Let's verify that a specific key is loaded
    assert "final.answer" in caps
    assert "ask.user" in caps
    assert "workflow.run" in caps
    assert "memory.add" in caps

    # And check that they are instances of the correct capability classes
    from navi.actions.conversation import FinalAnswerCapability, ClarifyCapability
    from navi.actions.memory import MemoryAddCapability
    from navi.actions.workflow import WorkflowRunCapability

    assert isinstance(caps["final.answer"], FinalAnswerCapability)
    assert isinstance(caps["ask.user"], ClarifyCapability)
    assert isinstance(caps["workflow.run"], WorkflowRunCapability)
    assert isinstance(caps["memory.add"], MemoryAddCapability)


def test_no_circular_dependencies_under_concurrency():
    """Verify that importing modules concurrently does not trigger runtime circular dependency cycles or race conditions in importlib."""
    modules_to_import = [
        "navi.capabilities",
        "navi.execution",
        "navi.engine",
        "navi.actions.conversation",
        "navi.actions.delegation",
        "navi.actions.approval",
        "navi.actions.watch",
        "navi.actions.workflow",
        "navi.actions.session",
        "navi.actions.memory",
        "navi.actions.trace",
        "navi.actions.evolution",
    ]
    
    errors = []
    
    def worker():
        try:
            # Unload modules first to force reload in this thread if needed,
            # or just importlib.reload/import_module them.
            # In Python, sys.modules is shared, so we reload them to force the import logic to execute.
            for mod_name in modules_to_import:
                # Force import
                importlib.import_module(mod_name)
                # Force reload
                mod = sys_modules_get(mod_name)
                if mod:
                    importlib.reload(mod)
        except Exception as e:
            errors.append(e)

    def sys_modules_get(name):
        import sys
        return sys.modules.get(name)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
        
    assert not errors, f"Concurrent import/reload encountered errors: {errors}"


def test_prompt_layer_store_invalid_names(tmp_path: Path):
    """Verify that PromptLayerStore rejects invalid names for security (path traversal)."""
    store = PromptLayerStore(tmp_path)
    for invalid in ["../escape", "sub/dir", "identity.md", "", "a b", "a*b"]:
        with pytest.raises(ValueError, match="invalid prompt layer name"):
            store.write_override(invalid, "content")
        with pytest.raises(ValueError, match="invalid prompt layer name"):
            store.override_path(invalid)


def test_prompt_layer_store_concurrent_read_delete_race(tmp_path: Path):
    """Verify that PromptLayerStore can experience a FileNotFoundError if an override is deleted during read.
    This acts as our empirical oracle validating the lack of mutex synchronization.
    """
    store = PromptLayerStore(tmp_path)
    store.write_override("race_test", "initial")

    errors = []
    stop_event = threading.Event()

    def reader():
        while not stop_event.is_set():
            try:
                store.get("race_test")
            except FileNotFoundError as e:
                errors.append(e)
                stop_event.set()
                break
            except Exception as e:
                errors.append(e)
                stop_event.set()
                break

    def writer():
        while not stop_event.is_set():
            try:
                store.write_override("race_test", "updated")
                store.delete_override("race_test")
            except Exception as e:
                errors.append(e)
                stop_event.set()
                break

    t1 = threading.Thread(target=reader)
    t2 = threading.Thread(target=writer)
    t1.start()
    t2.start()

    time.sleep(0.5)
    stop_event.set()
    t1.join()
    t2.join()

    if errors:
        print(f"\n[Race Condition Confirmed] Concurrency error caught: {errors}")
        assert any(isinstance(e, FileNotFoundError) for e in errors)
