from __future__ import annotations

import ast
import inspect
import textwrap

from navi.daemon import ProactiveEvent, SystemDaemon
from navi.engine import HernessEngine
from navi.memory import MemoryStore


def _source(obj) -> str:
    return textwrap.dedent(inspect.getsource(obj))


def test_daemon_process_events_stays_as_detector_orchestrator():
    source = _source(SystemDaemon.process_events_once)

    assert "asyncio.Semaphore(MAX_PROJECT_EVENT_CONCURRENCY)" in source
    assert "_process_project_events" in source

    embedded_detectors = (
        "git status",
        ".glob(",
        "_read_log_diff",
        "open_connection",
    )
    offenders = [token for token in embedded_detectors if token in source]

    assert offenders == []


def test_project_event_detectors_are_registered_explicitly(tmp_path):
    daemon = SystemDaemon(tmp_path)

    assert [detector.__name__ for detector in daemon._project_event_detectors()] == [
        "_detect_git_mutations",
        "_detect_service_log_events",
        "_detect_port_events",
    ]


def test_all_proactive_events_advance_state_when_suppressed():
    source = _source(SystemDaemon)
    tree = ast.parse(source)
    offenders: list[int] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_event = (
            isinstance(func, ast.Name) and func.id == ProactiveEvent.__name__
        ) or (
            isinstance(func, ast.Attribute) and func.attr == ProactiveEvent.__name__
        )
        if not is_event:
            continue
        keyword_names = {keyword.arg for keyword in node.keywords}
        if "state_updates" not in keyword_names or "suppressed_state_updates" not in keyword_names:
            offenders.append(node.lineno)

    assert offenders == []


def test_daemon_log_reader_stays_bounded_and_incremental():
    source = _source(SystemDaemon._read_log_diff)

    assert "readlines(" not in source
    assert "read(" not in source
    assert "f.readline(64_000)" in source
    assert "getincrementaldecoder" in source


def test_background_memory_tasks_are_tracked_bounded_and_observable():
    source = _source(HernessEngine._trigger_background_memory)

    expected_tokens = (
        "self._memory_sem",
        "asyncio.shield",
        "asyncio.create_task",
        "self._background_tasks.add(task)",
        "self._background_tasks.discard(t)",
        "task.add_done_callback(handle_done)",
        "logger.error",
        "exc_info=True",
    )
    missing = [token for token in expected_tokens if token not in source]

    assert missing == []


def test_memory_provider_failures_are_logged_before_fallback():
    for method in (
        MemoryStore.extract_and_consolidate_memories,
        MemoryStore.extract_memories_from_task,
    ):
        source = _source(method)

        assert "except Exception as e:" in source
        assert "logger.warning(" in source
        assert "exc_info=True" in source
        assert "return []" in source


def test_memory_async_extractors_offload_database_writes():
    for method in (
        MemoryStore.extract_and_consolidate_memories,
        MemoryStore.extract_memories_from_task,
    ):
        source = _source(method)

        assert "await asyncio.to_thread(" in source
        assert "self.add_item" in source
        assert "self.set_status" in source
        assert "ledger.record" in source


def test_daemon_port_probe_uses_ipv4_family_without_runtime_address_literal():
    source = _source(SystemDaemon._detect_port_events)

    assert "family=socket.AF_INET" in source
    assert '"127.0.0.1"' not in source
