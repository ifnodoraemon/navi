from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import yaml

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
        MemoryStore.extract_memories_from_run,
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


def test_memory_async_extractors_avoid_default_threadpool_shutdown_hangs():
    for method in (
        MemoryStore.extract_and_consolidate_memories,
        MemoryStore.extract_memories_from_run,
    ):
        source = _source(method)

        assert "asyncio.to_thread(" not in source
        assert "self.add_item" in source
        assert "self.set_status" in source
        assert "ledger.record" in source


def test_memory_store_has_no_flat_text_memory_api():
    memory_source = (Path(__file__).resolve().parents[1] / "src" / "navi" / "memory.py").read_text(encoding="utf-8")
    api_source = (Path(__file__).resolve().parents[1] / "src" / "navi" / "api.py").read_text(encoding="utf-8")

    forbidden = ("def read_memory", "def append_memory", "runtime.memory.read_memory", "runtime.memory.append_memory")
    offenders = {
        "memory.py": [token for token in forbidden if token in memory_source],
        "api.py": [token for token in forbidden if token in api_source],
    }

    assert offenders == {"memory.py": [], "api.py": []}


def test_daemon_port_probe_uses_explicit_dual_stack_without_runtime_address_literal():
    source = _source(SystemDaemon._detect_port_events)

    assert "socket.AF_INET" in source
    assert "socket.AF_INET6" in source
    assert '"127.0.0.1"' not in source


def test_daemon_project_detector_gather_is_failure_isolated():
    source = _source(SystemDaemon._process_project_events)

    assert "return_exceptions=True" in source
    assert "await asyncio.to_thread(self.graph.upsert" in source


def test_trust_async_match_avoids_default_threadpool_shutdown_hangs():
    source = _source(TrustStore.match)

    assert "asyncio.to_thread(self.list" not in source
    assert "self.list(sender_id=sender_id)" in source


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
    assert "delegate.retry" in action_names


def test_execution_protocol_has_no_free_form_compatibility_path():
    from navi.execution import ExecutionProtocol, NaviExecutionProvider

    source = _source(ExecutionProtocol) + _source(NaviExecutionProvider._result)

    assert "fallback" not in source
    assert "free-form" not in source
    assert "model_response" not in source
    assert "provider output violated the required execution protocol" in source


def test_tool_descriptions_do_not_carry_routing_policy(tmp_path):
    from navi.capabilities import build_capability_registry

    action_specs = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "src" / "navi" / "specs" / "action_tools.yaml").read_text(encoding="utf-8")
    )
    capability_specs = build_capability_registry(tmp_path, project_dir=tmp_path).list_specs()
    descriptions = {
        item["name"]: str(item.get("description") or "")
        for item in action_specs
    }
    descriptions.update({spec.name: spec.description for spec in capability_specs})

    routing_tokens = (
        "Use ",
        "use ",
        "when ",
        "When ",
        "unless ",
        "Unless ",
        "only when",
        "Do not",
        "do not",
        "Ask for",
    )
    offenders = {
        name: description
        for name, description in descriptions.items()
        if any(token in description for token in routing_tokens)
    }

    assert offenders == {}


def test_planner_prompt_uses_generic_state_transition_invariants():
    from navi.prompt_os import assemble_planner_system_prompt

    system = assemble_planner_system_prompt().render()

    assert "state_transition" in system
    assert "turn_scope=current" in system
    assert "created_this_turn" not in system
    assert "After a successful watch.create" not in system


def test_principles_require_global_design_before_patch():
    principles = (Path(__file__).resolve().parents[1] / "docs" / "principles.md").read_text(encoding="utf-8")

    assert "Global Design Before Patch" in principles
    assert "failing layer" in principles
    assert "Prefer structured facts and state transitions" in principles
    assert "Do not patch a tool description to change routing behavior" in principles


def test_principles_reject_historical_compatibility_debt():
    principles = (Path(__file__).resolve().parents[1] / "docs" / "principles.md").read_text(encoding="utf-8")

    assert "No Historical Compatibility Debt" in principles
    assert "Do not preserve historical prompt formats" in principles
    assert "Reject schema drift" in principles
    assert "require reinitialization" in principles


def test_durable_stores_do_not_keep_schema_compatibility_paths():
    goals_source = (Path(__file__).resolve().parents[1] / "src" / "navi" / "goals.py").read_text(encoding="utf-8")
    runs_source = (Path(__file__).resolve().parents[1] / "src" / "navi" / "runs.py").read_text(encoding="utf-8")
    trace_source = (Path(__file__).resolve().parents[1] / "src" / "navi" / "trace.py").read_text(encoding="utf-8")

    forbidden = ("_migrate", "_ensure_columns", "ALTER TABLE", "task_id")
    offenders = {
        "goals.py": [token for token in forbidden if token in goals_source],
        "runs.py": [token for token in forbidden if token in runs_source],
        "trace.py": [token for token in forbidden if token in trace_source],
    }

    assert offenders == {"goals.py": [], "runs.py": [], "trace.py": []}


def test_run_workspace_is_explicit_not_cwd_fallback():
    runs_source = (Path(__file__).resolve().parents[1] / "src" / "navi" / "runs.py").read_text(encoding="utf-8")
    execution_source = (Path(__file__).resolve().parents[1] / "src" / "navi" / "execution.py").read_text(encoding="utf-8")
    evolution_source = (Path(__file__).resolve().parents[1] / "src" / "navi" / "evolution.py").read_text(encoding="utf-8")

    forbidden = (
        "workspace or str(Path.cwd()",
        "task.workspace or Path.cwd()",
        "Path(task.workspace or",
        "workspace or str(Path.home())",
        "project_dir=project_dir or Path.cwd()",
    )
    offenders = {
        "runs.py": [token for token in forbidden if token in runs_source],
        "execution.py": [token for token in forbidden if token in execution_source],
        "evolution.py": [token for token in forbidden if token in evolution_source],
    }

    assert offenders == {"runs.py": [], "execution.py": [], "evolution.py": []}


def test_prompt_os_keeps_policy_manifest_and_turn_data_separate(tmp_path):
    from navi.capabilities import build_capability_registry
    from navi.prompt_os import assemble_planner_system_prompt, assemble_planner_turn_input

    tools = build_capability_registry(tmp_path, project_dir=tmp_path).list_specs()
    system = assemble_planner_system_prompt()
    turn = assemble_planner_turn_input("hello", tools=tools, observations=['{"ok":true}'])

    system_text = system.render()
    turn_manifest = turn.manifest()

    assert "[TOOL MANIFEST]" not in system_text
    assert "hello" not in system_text
    assert all(block["tier"] == "stable" for block in system.manifest()["blocks"])
    assert "[TOOL MANIFEST]" in turn.render()
    assert "<observed_facts>" in turn.render()
    assert any(block["tier"] == "manifest" and block["source"] == "capability_registry" for block in turn_manifest["blocks"])
    assert all(block["digest"] for block in system.manifest()["blocks"])


def test_prompt_os_documentation_declares_audit_contract():
    doc = (Path(__file__).resolve().parents[1] / "docs" / "prompt-architecture.md").read_text(encoding="utf-8")

    assert "Prompt Operating System" in doc
    assert "PromptBlock" in doc
    assert "PromptAssembly" in doc
    assert "Audit Contract" in doc
    assert "state_transition" in doc
