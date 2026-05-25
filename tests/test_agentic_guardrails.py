from __future__ import annotations

import ast
import inspect
import textwrap

from navi.daemon import ProactiveEvent, SystemDaemon
from navi.engine import HernessEngine
from navi.memory import MemoryStore
from navi.trust import TrustStore


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


def test_engine_exposes_graceful_background_shutdown():
    source = _source(HernessEngine.shutdown)

    assert "self._background_tasks" in source
    assert "asyncio.gather" in source
    assert "return_exceptions=True" in source


def test_asyncio_primitives_are_lazily_initialized():
    engine_init = _source(HernessEngine.__init__)
    memory_init = _source(MemoryStore.__init__)
    trust_init = _source(TrustStore.__init__)

    assert "asyncio.Semaphore(" not in engine_init
    assert "asyncio.Lock(" not in memory_init
    assert "asyncio.Semaphore(" not in trust_init
    assert "def _memory_semaphore" in _source(HernessEngine)
    assert "def _session_lock_guard" in _source(MemoryStore)
    assert "def _semantic_semaphore" in _source(TrustStore)


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


def test_memory_session_lock_acquisition_is_inside_finally_guard():
    source = _source(MemoryStore.extract_and_consolidate_memories)
    acquire_pos = source.index("lock = await self._acquire_session_lock")
    try_pos = source.index("try:")
    finally_pos = source.index("finally:")

    assert try_pos < acquire_pos < finally_pos
    assert "if lock is not None:" in source


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


def test_daemon_port_probe_uses_explicit_dual_stack_without_runtime_address_literal():
    source = _source(SystemDaemon._detect_port_events)

    assert "socket.AF_INET" in source
    assert "socket.AF_INET6" in source
    assert '"127.0.0.1"' not in source


def test_daemon_project_detector_gather_is_failure_isolated():
    source = _source(SystemDaemon._process_project_events)

    assert "return_exceptions=True" in source
    assert "await asyncio.to_thread(self.graph.upsert" in source


def test_trust_async_match_offloads_rule_listing():
    source = _source(TrustStore.match)

    assert "await asyncio.to_thread(self.list" in source


def test_daemon_detectors_emit_facts_not_repair_workflows():
    source = _source(SystemDaemon)

    workflow_tokens = (
        "Analyze the error",
        "propose a fix",
        "restart the service",
        "run tests if applicable",
        "verify code correctness",
    )
    offenders = [token for token in workflow_tokens if token in source]

    assert offenders == []
    assert "Observation facts" in _source(SystemDaemon._event_policy_prompt)


def test_execution_follow_up_is_explicit_capability_not_hidden_loop():
    import yaml
    from pathlib import Path
    from navi.execution import ExecutionService

    source = _source(ExecutionService.execute_task)
    action_specs = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "src" / "navi" / "specs" / "action_tools.yaml").read_text(encoding="utf-8")
    )
    action_names = {item["name"] for item in action_specs}

    assert "SELF-HEALING" not in source
    assert "while result.exit_code" not in source
    assert "execution.retry" in action_names


def test_execution_protocol_has_no_free_form_compatibility_path():
    from navi.execution import ExecutionProtocol, NaviExecutionProvider

    source = _source(ExecutionProtocol) + _source(NaviExecutionProvider._result)

    assert "fallback" not in source
    assert "free-form" not in source
    assert "model_response" not in source
    assert "provider output violated the required execution protocol" in source
