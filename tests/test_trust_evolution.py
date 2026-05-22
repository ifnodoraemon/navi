from __future__ import annotations

import asyncio
import json
import pytest
import shutil
import socket
import subprocess
import time

from navi.provider import ChatMessage, MockProvider, ModelPool
from navi.trust import TrustStore
from navi.execution import ExecutionService, ExecutionResult
from navi.evolution import EvolutionEngine, EvolutionLedger
from navi.tasks import TaskStore
from navi.daemon import ProjectEventContext, SystemDaemon
from navi.graph import GraphStore


class ScriptedProvider(MockProvider):
    def __init__(self, responses: list[str]):
        self.responses = responses
        self.messages: list[list[ChatMessage]] = []

    async def complete(self, messages: list[ChatMessage]) -> str:
        self.messages.append(messages)
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_semantic_trust_matching(tmp_path):
    # 1. Setup TrustStore
    store = TrustStore(tmp_path)
    
    # Create a rule with pattern "compile code"
    store.upsert(
        name="test-rule",
        pattern="compile code",
        project_path=str(tmp_path),
        sender_id="user123",
        autonomy_level="L3",
    )
    
    # Case A: Strict subset matching (matches immediately, no LLM required)
    decision_strict = await store.decide(
        prompt="compile code now",
        sender_id="user123",
        workspace=str(tmp_path),
    )
    assert decision_strict.level == "L3"
    
    # Case B: Semantic intent matching (requires LLM)
    # Mock LLM returns {"matches": true, "reason": "Semantically related"}
    provider = ScriptedProvider([json.dumps({"matches": True, "reason": "Semantically related"})])
    pool = ModelPool(default=provider)
    
    decision_semantic = await store.decide(
        prompt="build the project files",
        sender_id="user123",
        workspace=str(tmp_path),
        provider=pool,
    )
    assert decision_semantic.level == "L3"
    
    # Case C: Semantic intent mismatch (LLM returns matches: false)
    provider_false = ScriptedProvider([json.dumps({"matches": False, "reason": "Mismatch"})])
    pool_false = ModelPool(default=provider_false)
    
    decision_mismatch = await store.decide(
        prompt="delete all databases",
        sender_id="user123",
        workspace=str(tmp_path),
        provider=pool_false,
    )
    assert decision_mismatch.level == "L2"  # Fallback to L2 approval


@pytest.mark.asyncio
async def test_execution_failure_waits_for_explicit_follow_up_and_rolls_back(tmp_path):
    # Setup execution and tasks
    tasks = TaskStore(tmp_path)
    execution = ExecutionService(tmp_path)
    
    # Create task
    task = tasks.create(
        title="Heal test",
        prompt="Fix the broken code",
        kind="task",
        workspace=str(tmp_path),
        autonomy_level="L3",
    )
    
    failed_result = ExecutionResult(
        provider="mock",
        phase="execute",
        command=["mock", "exec"],
        stdout="Failed compiling main.py",
        stderr="NameError: name 'x' is not defined",
        exit_code=1,
        started_at=time.time(),
        ended_at=time.time(),
    )
    async def mock_provider_call(t, phase):
        return failed_result
        
    execution._provider_call_with_timeout = mock_provider_call
    
    # Run task execution
    updated_task = await execution.execute_task(task)
    
    assert updated_task.status == "failed"
    assert updated_task.result_summary == "Failed compiling main.py"
    assert "NameError" in updated_task.error
    
    # Verify evolution ledger entries for task_execution target type
    ledger = EvolutionLedger(tmp_path)
    events = ledger.list()
    assert len(events) == 1
    assert events[0].target_type == "task_execution"
    assert events[0].target_id == task.id
    
    # Verify rollback reverts task state in database
    evolution = EvolutionEngine(tmp_path)
    rolled_back_event = evolution.rollback(events[0].id)
    assert rolled_back_event.rolled_back_at > 0
    
    rolled_task = tasks.get(task.id)
    assert rolled_task.status == "pending"
    assert rolled_task.result_summary == ""


@pytest.mark.asyncio
async def test_proactive_event_watchers(tmp_path):
    # Setup daemon
    daemon = SystemDaemon(tmp_path)
    graph = GraphStore(tmp_path)
    
    # Create project in graph database
    project_path = tmp_path / "my_project"
    project_path.mkdir(parents=True, exist_ok=True)
    
    graph.upsert(
        "Project",
        str(project_path),
        {"last_git_status_hash": "", "log_size_logs/dev.log": 0},
    )
    
    # Write mock exception logs
    log_dir = project_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "dev.log"
    log_file.write_text("Exception: Python crashed in utils.py\nTraceback (most recent call last):\n  File 'utils.py', line 12")
    
    # Run daemon process events check
    # Mock capabilities invoke to just return a dummy result
    from navi.capabilities import CapabilityResult
    mock_invoke_calls = []
    async def mock_invoke(name, args, permission=None, context=None):
        mock_invoke_calls.append((name, args, context))
        return CapabilityResult(ok=True, action="task", observation="Proactive task created", message="Created proactively", task_id="t-1")
        
    daemon.capabilities.invoke = mock_invoke
    
    created_events = await daemon.process_events_once()
    
    # Verify event logs triggered a proactive task creation
    assert len(created_events) == 1
    assert "Exception detected in log" in created_events[0]["message"]
    
    assert [call[0] for call in mock_invoke_calls] == ["task.record", "task.prepare", "approval.request"]
    assert "proactive runtime detector produced observation facts" in mock_invoke_calls[0][1]["prompt"]
    assert "log_error_detected" in mock_invoke_calls[0][1]["prompt"]
    assert mock_invoke_calls[0][2].source == "event_log"


@pytest.mark.asyncio
async def test_concurrent_trust_matching(tmp_path):
    store = TrustStore(tmp_path)
    store.upsert(name="rule1", pattern="compile code", project_path=str(tmp_path), sender_id="user123", autonomy_level="L3")
    store.upsert(name="rule2", pattern="run tests", project_path=str(tmp_path), sender_id="user123", autonomy_level="L3")
    
    provider = ScriptedProvider([
        json.dumps({"matches": False, "reason": "no"}),
        json.dumps({"matches": True, "reason": "yes"})
    ])
    pool = ModelPool(default=provider)
    
    matched = await store.match(prompt="execute test scripts", sender_id="user123", workspace=str(tmp_path), provider=pool)
    assert matched is not None
    assert len(provider.messages) == 2


@pytest.mark.asyncio
async def test_log_rotation_and_chunked_reads(tmp_path):
    daemon = SystemDaemon(tmp_path)
    graph = GraphStore(tmp_path)
    
    project_path = tmp_path / "rotation_project"
    project_path.mkdir(parents=True, exist_ok=True)
    graph.upsert("Project", str(project_path), {"log_size_logs/dev.log": 500})
    
    log_dir = project_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "dev.log"
    
    log_file.write_text("FATAL Error on rotated log!\n")
    current_size = len(log_file.read_bytes())
    assert current_size < 500
    
    from navi.capabilities import CapabilityResult
    mock_invoke_calls = []
    async def mock_invoke(name, args, permission=None, context=None):
        mock_invoke_calls.append((name, args, context))
        return CapabilityResult(ok=True, action="task", observation="Proactive task created", message="Created proactively", task_id="t-1")
    daemon.capabilities.invoke = mock_invoke
    
    await daemon.process_events_once()
    assert [call[0] for call in mock_invoke_calls] == ["task.record", "task.prepare", "approval.request"]
    assert "FATAL" in mock_invoke_calls[0][1]["prompt"]
    assert "log_error_detected" in mock_invoke_calls[0][1]["prompt"]
    
    graph.upsert("Project", str(project_path), {"log_size_logs/dev.log": 0})
    huge_log = "Ok log lines...\n" * 1000 + "Exception: Crashed after huge output!\n"
    log_file.write_text(huge_log)
    
    mock_invoke_calls.clear()
    await daemon.process_events_once()
    assert [call[0] for call in mock_invoke_calls] == ["task.record", "task.prepare", "approval.request"]
    assert "Exception" in mock_invoke_calls[0][1]["prompt"]
    assert "Observation facts" in mock_invoke_calls[0][1]["prompt"]


def test_log_reader_preserves_utf8_across_chunk_boundary(tmp_path):
    from navi.daemon import SystemDaemon

    log_file = tmp_path / "utf8.log"
    prefix = b"a" * 63_999
    log_file.write_bytes(prefix + "界 Exception: boundary failure\n".encode())

    new_content, error_lines, new_offset = SystemDaemon._read_log_diff(
        log_file,
        0,
        len(log_file.read_bytes()),
    )

    assert "界 Exception: boundary failure" in new_content
    assert "�" not in new_content
    assert len(error_lines) == 1
    assert error_lines[0].endswith("界 Exception: boundary failure")
    assert new_offset == len(log_file.read_bytes())


def test_evolution_proposals_are_reviewable_before_apply(tmp_path):
    from navi.evolution import EvolutionLedger, list_evolution_targets

    targets = {target["target_type"] for target in list_evolution_targets()}
    assert {"prompt_layer", "skill", "memory_item", "trust_policy", "workflow_policy"} <= targets

    ledger = EvolutionLedger(tmp_path)
    proposal = ledger.propose(
        target_type="prompt_layer",
        target_id="authorization",
        reason="tighten local action wording",
        expected_benefit="fewer false claims about local access",
        risk="over-constraining responses",
        before="old prompt",
        after="new prompt",
        rollback_plan="restore previous prompt layer content",
        evidence="review finding A04",
        source_task_id="task-123",
        eval_cases=["prompt_style_regression"],
    )

    assert proposal.status == "proposed"
    assert proposal.diff
    assert ledger.list() == []
    assert ledger.get_proposal(proposal.id).target_type == "prompt_layer"

    event = ledger.apply_proposal(proposal.id)

    assert event is not None
    assert event.target_type == "prompt_layer"
    assert event.target_id == "authorization"
    assert event.task_id == "task-123"
    applied = ledger.get_proposal(proposal.id)
    assert applied.status == "applied"
    assert applied.applied_event_id == event.id
    assert json.loads(applied.eval_cases) == ["prompt_style_regression"]

    evaluated = ledger.record_proposal_evaluation(proposal.id, "prompt_style_regression: pass")
    assert evaluated.evaluation_result == "prompt_style_regression: pass"


def test_evolution_proposals_reject_unknown_targets(tmp_path):
    ledger = EvolutionLedger(tmp_path)

    with pytest.raises(ValueError, match="unknown evolution target type"):
        ledger.propose(
            target_type="hidden_runtime_magic",
            target_id="x",
            reason="invalid",
            expected_benefit="",
            risk="",
            before="",
            after="",
            rollback_plan="",
        )


def test_trust_policy_is_declared_and_used():
    from navi.trust import PROMOTION_SUCCESSES, trust_policy_facts

    policy = trust_policy_facts()

    assert policy["default_level"] == "L2"
    assert policy["auto_execute_level"] == "L3"
    assert policy["promotion_successes"] == PROMOTION_SUCCESSES == 3
    assert policy["labels"]["L2"] == "approve_execute"


@pytest.mark.asyncio
async def test_self_healing_retry_accumulation(tmp_path):
    from navi.capabilities import CapabilityContext, CapabilityRegistry
    import navi.capabilities as capabilities_module

    tasks = TaskStore(tmp_path)
    execution = ExecutionService(tmp_path)
    
    task = tasks.create(
        title="Accumulate test",
        prompt="Execute python script",
        kind="task",
        workspace=str(tmp_path),
        autonomy_level="L3",
    )
    
    res1 = ExecutionResult(
        provider="mock", phase="execute", command=["python", "run.py"],
        stdout="Output1", stderr="SyntaxError: invalid syntax", exit_code=1,
        started_at=time.time(), ended_at=time.time()
    )
    res3 = ExecutionResult(
        provider="mock", phase="execute", command=["python", "run.py"],
        stdout="Success!", stderr="", exit_code=0,
        started_at=time.time(), ended_at=time.time()
    )
    
    calls = [res1, res3]
    prompt_history = []
    async def mock_provider_call(t, phase):
        prompt_history.append(t.prompt)
        return calls.pop(0)
        
    execution._provider_call_with_timeout = mock_provider_call
    
    first = await execution.execute_task(task)
    assert first.status == "failed"
    approval = tasks.create_approval(task_id=task.id, peer_id="peer", sender_id="sender")
    tasks.resolve_approval(approval.code, "sender", "approved")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(capabilities_module, "ExecutionService", lambda home: execution)
    try:
        retry = await CapabilityRegistry(home=tmp_path, project_dir=tmp_path).invoke(
            "execution.retry",
            {"task_id": task.id, "follow_up_prompt": "Use the observed SyntaxError as data and try one corrected run."},
            permission="write",
            context=CapabilityContext(home=tmp_path),
        )
    finally:
        monkeypatch.undo()

    assert retry.ok is True
    assert retry.facts["status"] == "completed"
    assert len(prompt_history) == 2
    assert "Output1" not in prompt_history[1]
    assert "SELF-HEALING" not in prompt_history[1]
    assert "Follow-up execution instruction" in prompt_history[1]
    assert "corrected run" in prompt_history[1]
    prompt_logs = [
        log for log in TaskStore(tmp_path).list_execution_logs(task.id)
        if log.phase == "self_heal_prompt"
    ]
    assert prompt_logs == []


def test_daemon_primary_project_selection_is_stable(tmp_path):
    daemon = SystemDaemon(tmp_path)
    older = GraphStore(tmp_path).upsert("Project", str(tmp_path / "z-project"), {})
    newer = GraphStore(tmp_path).upsert("Project", str(tmp_path / "a-project"), {})

    assert daemon._primary_project_name([older, newer]) == str(tmp_path / "a-project")

    marked = GraphStore(tmp_path).upsert("Project", str(tmp_path / "m-project"), {"primary": True})
    assert daemon._primary_project_name([older, newer, marked]) == str(tmp_path / "m-project")


@pytest.mark.asyncio
async def test_daemon_port_probe_checks_ipv4_and_ipv6(tmp_path, monkeypatch):
    daemon = SystemDaemon(tmp_path)
    open_calls = []

    class FakeWriter:
        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def fake_open_connection(host, port, **kwargs):
        open_calls.append((host, port, kwargs))
        return object(), FakeWriter()

    import navi.daemon as daemon_module

    monkeypatch.setattr(daemon_module.asyncio, "open_connection", fake_open_connection)

    await daemon._detect_port_events(
        ProjectEventContext(
            project_path=str(tmp_path),
            project_data={"dev_ports": [54321]},
            has_active_task=False,
            use_default_ports=False,
        )
    )

    assert open_calls == [
        ("localhost", 54321, {"family": socket.AF_INET}),
        ("localhost", 54321, {"family": socket.AF_INET6}),
    ]


@pytest.mark.asyncio
async def test_daemon_project_detectors_isolate_failures(tmp_path, caplog):
    daemon = SystemDaemon(tmp_path)
    graph = GraphStore(tmp_path)
    project_path = tmp_path / "detector_project"
    project_path.mkdir()
    graph.upsert("Project", str(project_path), {})

    async def failing_detector(context):
        raise RuntimeError("detector failed")

    async def healthy_detector(context):
        return [], {"healthy_detector_ran": True}

    daemon._project_event_detectors = lambda: (failing_detector, healthy_detector)
    caplog.set_level("WARNING", logger="navi.daemon")

    events = await daemon.process_events_once()

    assert events == []
    assert "detector failed" in caplog.text
    project = graph.get_by_name("Project", str(project_path))
    assert project.data["healthy_detector_ran"] is True


def test_daemon_port_probe_timeout_is_configurable_and_bounded():
    assert SystemDaemon._port_probe_timeout({}) == 1.0
    assert SystemDaemon._port_probe_timeout({"port_probe_timeout_seconds": 2}) == 2.0
    assert SystemDaemon._port_probe_timeout({"port_probe_timeout_seconds": 0.1}) == 0.5
    assert SystemDaemon._port_probe_timeout({"port_probe_timeout_seconds": 30}) == 10.0
    assert SystemDaemon._port_probe_timeout({"port_probe_timeout_seconds": "bad"}) == 1.0


@pytest.mark.asyncio
async def test_daemon_resolves_relative_project_paths(tmp_path, monkeypatch):
    from navi.capabilities import CapabilityResult

    monkeypatch.chdir(tmp_path)
    daemon = SystemDaemon(tmp_path)
    graph = GraphStore(tmp_path)
    project_path = tmp_path / "relative_project"
    project_path.mkdir()
    log_file = project_path / "app.log"
    log_file.write_text("Exception: relative path failure\n")
    graph.upsert("Project", "relative_project", {})
    mock_invokes = []

    async def mock_invoke(name, args, permission=None, context=None):
        mock_invokes.append(args)
        return CapabilityResult(ok=True, action="task", observation="ok", message="ok", task_id="t-1")

    daemon.capabilities.invoke = mock_invoke

    events = await daemon.process_events_once()

    assert len(events) == 1
    assert mock_invokes
    project = graph.get_by_name("Project", "relative_project")
    assert project is not None
    assert project.data["log_size_app.log"] == len(log_file.read_bytes())


@pytest.mark.asyncio
async def test_daemon_git_detector_skips_when_git_binary_missing(tmp_path, monkeypatch, caplog):
    daemon = SystemDaemon(tmp_path)
    project_path = tmp_path / "gitless"
    (project_path / ".git").mkdir(parents=True)

    import navi.daemon as daemon_module

    monkeypatch.setattr(daemon_module.shutil, "which", lambda binary: None)
    caplog.set_level("WARNING", logger="navi.daemon")

    events, updates = await daemon._detect_git_mutations(
        ProjectEventContext(
            project_path=str(project_path),
            project_data={},
            has_active_task=False,
            use_default_ports=False,
        )
    )

    assert events == []
    assert updates == {}
    assert "git is not on PATH" in caplog.text


@pytest.mark.asyncio
async def test_semantic_trust_matching_reaches_later_batches(tmp_path):
    store = TrustStore(tmp_path)
    assert store._semantic_sem is None
    store.upsert(
        name="relevant",
        pattern="deploy database",
        project_path=str(tmp_path),
        sender_id="user123",
        autonomy_level="L3",
    )
    for idx in range(5):
        store.upsert(
            name=f"irrelevant-{idx}",
            pattern=f"unrelated task {idx}",
            project_path=str(tmp_path),
            sender_id="user123",
            autonomy_level="L3",
        )

    class PatternAwareProvider(ScriptedProvider):
        async def complete(self, messages):
            self.messages.append(messages)
            content = messages[-1].content
            return json.dumps({"matches": "deploy database" in content})

    provider = PatternAwareProvider([])
    matched = await store.match(
        prompt="ship the database service",
        sender_id="user123",
        workspace=str(tmp_path),
        provider=ModelPool(default=provider),
    )

    assert matched is not None
    assert matched.name == "relevant"
    assert store._semantic_sem is not None
    assert len(provider.messages) == 6


def test_read_only_skills_store(tmp_path):
    from navi.skills import SkillStore
    
    store = SkillStore(tmp_path)
    assert store.skills_dir.is_dir()
    store.builtin_skills_dir = tmp_path / "nonexistent_builtins"
    skills = store.list_skills()
    assert isinstance(skills, list)


def test_evolution_skill_rollback_rejects_paths_outside_skills_dir(tmp_path):
    from navi.evolution import EvolutionEngine, EvolutionLedger

    event = EvolutionLedger(tmp_path).record(
        task_id="task",
        target_type="skill",
        target_id=str(tmp_path / "outside" / "SKILL.md"),
        reason="malformed skill path",
        before="old",
        after="new",
    )

    with pytest.raises(ValueError, match="home skills directory"):
        EvolutionEngine(tmp_path).rollback(event.id)


@pytest.mark.asyncio
async def test_session_locks_memory_cleanup(tmp_path):
    from navi.memory import MemoryStore
    from navi.provider import ModelPool
    
    store = MemoryStore(tmp_path)
    assert len(store._session_locks) == 0
    
    # Trigger consolidator with no messages, should exit and clean up lock
    res = await store.extract_and_consolidate_memories("session-1", ModelPool(default=None))
    assert res == []
    assert len(store._session_locks) == 0


@pytest.mark.asyncio
async def test_engine_strong_task_references(tmp_path):
    from navi.engine import HernessEngine, AgentTurnResult
    from navi.provider import ModelPool
    
    # Mock runtime and memory
    class DummyRuntime:
        def __init__(self):
            self.provider = ModelPool(default=None)
            self.memory = None
            
    class DummyMemory:
        async def extract_and_consolidate_memories(self, session_id, provider, task_id):
            await asyncio.sleep(0.01)
            
    runtime = DummyRuntime()
    runtime.memory = DummyMemory()
    
    engine = HernessEngine(home=tmp_path, runtime=runtime)
    assert len(engine._background_tasks) == 0
    
    # Trigger background memory
    engine._trigger_background_memory(AgentTurnResult(text="hello", session_id="sess-1"))
    assert len(engine._background_tasks) == 1
    
    # Wait for completion and verify it was discarded
    await asyncio.sleep(0.05)
    assert len(engine._background_tasks) == 0


@pytest.mark.asyncio
async def test_engine_background_memory_uses_cancellation_shield(tmp_path, monkeypatch):
    from navi.engine import HernessEngine, AgentTurnResult
    from navi.provider import ModelPool
    import navi.engine as engine_module

    shield_calls = 0
    original_shield = engine_module.asyncio.shield

    def counted_shield(awaitable):
        nonlocal shield_calls
        shield_calls += 1
        return original_shield(awaitable)

    class DummyRuntime:
        def __init__(self):
            self.provider = ModelPool(default=None)
            self.memory = None

    class DummyMemory:
        async def extract_and_consolidate_memories(self, session_id, provider, task_id):
            await asyncio.sleep(0.01)

    runtime = DummyRuntime()
    runtime.memory = DummyMemory()
    monkeypatch.setattr(engine_module.asyncio, "shield", counted_shield)

    engine = HernessEngine(home=tmp_path, runtime=runtime)
    engine._trigger_background_memory(AgentTurnResult(text="hello", session_id="sess-1"))
    await asyncio.sleep(0.05)

    assert shield_calls == 1


@pytest.mark.asyncio
async def test_engine_shutdown_waits_for_background_memory(tmp_path):
    from navi.engine import HernessEngine, AgentTurnResult
    from navi.provider import ModelPool

    completed = False

    class DummyRuntime:
        def __init__(self):
            self.provider = ModelPool(default=None)
            self.memory = None

    class DummyMemory:
        async def extract_and_consolidate_memories(self, session_id, provider, task_id):
            nonlocal completed
            await asyncio.sleep(0.02)
            completed = True

    runtime = DummyRuntime()
    runtime.memory = DummyMemory()
    engine = HernessEngine(home=tmp_path, runtime=runtime)
    assert engine._memory_sem is None

    engine._trigger_background_memory(AgentTurnResult(text="hello", session_id="sess-1"))
    await engine.shutdown()

    assert completed is True
    assert engine._memory_sem is not None
    assert len(engine._background_tasks) == 0


@pytest.mark.asyncio
async def test_daemon_active_task_suppression(tmp_path):
    from navi.daemon import SystemDaemon
    from navi.graph import GraphStore
    from navi.tasks import TaskStore
    from navi.capabilities import CapabilityResult
    
    daemon = SystemDaemon(tmp_path)
    graph = GraphStore(tmp_path)
    tasks = TaskStore(tmp_path)
    
    project_path = tmp_path / "active_project"
    project_path.mkdir(parents=True, exist_ok=True)
    
    graph.upsert("Project", str(project_path), {"last_git_status_hash": ""})
    
    # Write mock exception logs
    log_dir = project_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "dev.log"
    log_file.write_text("Exception: crashes in main\nTraceback (most recent call last):\n  File 'main.py'")
    
    # Mock capability invoke
    mock_invokes = []
    async def mock_invoke(name, args, permission=None, context=None):
        mock_invokes.append(args)
        return CapabilityResult(ok=True, action="task", observation="ok", message="ok", task_id="t-1")
    daemon.capabilities.invoke = mock_invoke
    
    # Case 1: No active tasks, process_events_once should trigger a proactive alert
    events = await daemon.process_events_once()
    assert len(events) == 1
    assert len(mock_invokes) == 3
    
    # Reset offsets
    graph.upsert("Project", str(project_path), {"log_size_logs/dev.log": 0, "last_err_fp_logs/dev.log": ""})
    mock_invokes.clear()
    
    # Case 2: Active task in progress for this workspace, should suppress proactive alert!
    tasks.create(title="Active task", prompt="Fix something", kind="task", workspace=str(project_path), status="running")
    
    events_suppressed = await daemon.process_events_once()
    assert len(events_suppressed) == 0
    assert len(mock_invokes) == 0
    project = graph.get_by_name("Project", str(project_path))
    assert project.data["log_size_logs/dev.log"] == len(log_file.read_bytes())
    assert project.data["last_err_fp_logs/dev.log"]


@pytest.mark.asyncio
async def test_daemon_git_suppression_advances_hash(tmp_path):
    if not shutil.which("git"):
        pytest.skip("git binary is required for daemon git status coverage")

    from navi.daemon import SystemDaemon
    from navi.graph import GraphStore
    from navi.tasks import TaskStore
    from navi.capabilities import CapabilityResult

    daemon = SystemDaemon(tmp_path)
    graph = GraphStore(tmp_path)
    tasks = TaskStore(tmp_path)

    project_path = tmp_path / "git_active_project"
    project_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=project_path, check=True, capture_output=True)
    (project_path / "changed.py").write_text("print('changed')\n")

    graph.upsert("Project", str(project_path), {"last_git_status_hash": ""})
    tasks.create(
        title="Active git task",
        prompt="Fix git project",
        kind="task",
        workspace=str(project_path),
        status="running",
    )

    mock_invokes = []

    async def mock_invoke(name, args, permission=None, context=None):
        mock_invokes.append(args)
        return CapabilityResult(ok=True, action="task", observation="ok", message="ok", task_id="t-1")

    daemon.capabilities.invoke = mock_invoke

    events = await daemon.process_events_once()

    assert events == []
    assert mock_invokes == []
    project = graph.get_by_name("Project", str(project_path))
    assert project.data["last_git_status_hash"]


@pytest.mark.asyncio
async def test_daemon_log_keys_include_relative_path(tmp_path):
    from navi.daemon import SystemDaemon
    from navi.graph import GraphStore
    from navi.capabilities import CapabilityResult

    daemon = SystemDaemon(tmp_path)
    graph = GraphStore(tmp_path)

    project_path = tmp_path / "collision_project"
    project_path.mkdir(parents=True, exist_ok=True)
    nested_log_dir = project_path / "logs"
    nested_log_dir.mkdir(parents=True, exist_ok=True)
    root_log = project_path / "app.log"
    nested_log = nested_log_dir / "app.log"
    root_log.write_text("Exception: root app failed\n")
    nested_log.write_text("FATAL: nested app failed\n")
    graph.upsert("Project", str(project_path), {})

    mock_invokes = []

    async def mock_invoke(name, args, permission=None, context=None):
        mock_invokes.append(args)
        return CapabilityResult(ok=True, action="task", observation="ok", message="ok", task_id="t-1")

    daemon.capabilities.invoke = mock_invoke

    events = await daemon.process_events_once()

    assert len(events) == 2
    assert len(mock_invokes) == 6
    project = graph.get_by_name("Project", str(project_path))
    assert project.data["log_size_app.log"] == len(root_log.read_bytes())
    assert project.data["log_size_logs/app.log"] == len(nested_log.read_bytes())
    assert project.data["last_err_fp_app.log"]
    assert project.data["last_err_fp_logs/app.log"]


@pytest.mark.asyncio
async def test_daemon_fingerprint_spam_protection(tmp_path):
    from navi.daemon import SystemDaemon
    from navi.graph import GraphStore
    from navi.capabilities import CapabilityResult
    
    daemon = SystemDaemon(tmp_path)
    graph = GraphStore(tmp_path)
    
    project_path = tmp_path / "fingerprint_project"
    project_path.mkdir(parents=True, exist_ok=True)
    graph.upsert("Project", str(project_path), {"log_size_logs/dev.log": 0})
    
    log_dir = project_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "dev.log"
    
    # Mock capability invoke
    mock_invokes = []
    async def mock_invoke(name, args, permission=None, context=None):
        mock_invokes.append(args)
        return CapabilityResult(ok=True, action="task", observation="ok", message="ok", task_id="t-1")
    daemon.capabilities.invoke = mock_invoke
    
    # Write first exception
    log_file.write_text("Exception: syntax error in compiler.py\nTraceback (most recent call last):\n  File 'compiler.py'")
    events1 = await daemon.process_events_once()
    assert len(events1) == 1
    assert len(mock_invokes) == 3
    
    # Reset log_size but keep same exception contents
    project = graph.get_by_name("Project", str(project_path))
    project.data["log_size_logs/dev.log"] = 0
    graph.upsert("Project", str(project_path), project.data)
    mock_invokes.clear()
    
    # Write same exception again, should be ignored by fingerprint!
    events2 = await daemon.process_events_once()
    assert len(events2) == 0
    assert len(mock_invokes) == 0
    
    # Write different exception, should trigger new proactive alert
    log_file.write_text("FATAL: Out of memory in database connections\n")
    # Reset log size so it parses
    project = graph.get_by_name("Project", str(project_path))
    project.data["log_size_logs/dev.log"] = 0
    graph.upsert("Project", str(project_path), project.data)
    
    events3 = await daemon.process_events_once()
    assert len(events3) == 1
    assert len(mock_invokes) == 3


@pytest.mark.asyncio
async def test_trust_success_consecutive_upgrades_and_failure_resets(tmp_path):
    from navi.trust import TrustStore
    from navi.tasks import Task
    import time

    store = TrustStore(tmp_path)
    # 1. Upsert a rule at L2
    rule = store.upsert(
        name="test rule",
        pattern="deploy application to server",
        project_path="",
        sender_id="user1",
        autonomy_level="L2",
        data={"auto_created": True},
    )

    # 2. Record 3 successful runs
    task = Task(
        id="task-123",
        title="test task",
        status="success",
        created_at=time.time(),
        updated_at=time.time(),
        prompt="deploy application to server",
        workspace="/tmp/workspace",
        sender_id="user1",
        autonomy_level="L2",
        trust_rule_id=rule.id,
    )

    # First success
    rule = store.record_success(task)
    assert rule.success_count == 1
    assert rule.autonomy_level == "L2"
    assert rule.data.get("consecutive_successes") == 1

    # Second success
    rule = store.record_success(task)
    assert rule.success_count == 2
    assert rule.autonomy_level == "L2"
    assert rule.data.get("consecutive_successes") == 2

    # Third success -> Promotes to L3, resets consecutive successes to 0, sets project_path
    rule = store.record_success(task)
    assert rule.success_count == 3
    assert rule.autonomy_level == "L3"
    assert rule.data.get("consecutive_successes") == 0
    assert rule.project_path == "/tmp/workspace"

    # 3. Record a failure -> Downgrades to L2, resets consecutive successes to 0
    rule = await store.record_failure(task)
    assert rule.failure_count == 1
    assert rule.autonomy_level == "L2"
    assert rule.data.get("consecutive_successes") == 0
