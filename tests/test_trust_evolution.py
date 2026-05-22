from __future__ import annotations

import asyncio
import json
import pytest
import shutil
import subprocess
import time

from navi.provider import ChatMessage, MockProvider, ModelPool
from navi.trust import TrustStore
from navi.execution import ExecutionService, ExecutionResult
from navi.evolution import EvolutionEngine, EvolutionLedger
from navi.tasks import TaskStore
from navi.daemon import SystemDaemon
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
async def test_self_healing_execution_and_rollback(tmp_path):
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
    
    # Mock execution providers to return a failure on first execute, and success on second execution
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
    success_result = ExecutionResult(
        provider="mock",
        phase="execute",
        command=["mock", "exec"],
        stdout="Compiled successfully",
        stderr="",
        exit_code=0,
        started_at=time.time(),
        ended_at=time.time(),
    )
    
    # Mock _provider_call_with_timeout to yield failed then success
    calls = [failed_result, success_result]
    async def mock_provider_call(t, phase):
        return calls.pop(0)
        
    execution._provider_call_with_timeout = mock_provider_call
    
    # Run task execution
    updated_task = await execution.execute_task(task)
    
    # Verify self-healing succeeded after retry
    assert updated_task.status == "completed"
    assert updated_task.result_summary == "Compiled successfully"
    assert updated_task.error == ""
    
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
    
    assert len(mock_invoke_calls) == 1
    assert mock_invoke_calls[0][0] == "task.create"
    assert "Proactive Alert: I detected an exception/error" in mock_invoke_calls[0][1]["prompt"]
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
    assert len(mock_invoke_calls) == 1
    assert "FATAL" in mock_invoke_calls[0][1]["prompt"]
    
    graph.upsert("Project", str(project_path), {"log_size_logs/dev.log": 0})
    huge_log = "Ok log lines...\n" * 1000 + "Exception: Crashed after huge output!\n"
    log_file.write_text(huge_log)
    
    mock_invoke_calls.clear()
    await daemon.process_events_once()
    assert len(mock_invoke_calls) == 1
    assert "Exception" in mock_invoke_calls[0][1]["prompt"]


@pytest.mark.asyncio
async def test_self_healing_retry_accumulation(tmp_path):
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
    res2 = ExecutionResult(
        provider="mock", phase="execute", command=["python", "run.py"],
        stdout="Output2", stderr="ImportError: module missing", exit_code=1,
        started_at=time.time(), ended_at=time.time()
    )
    res3 = ExecutionResult(
        provider="mock", phase="execute", command=["python", "run.py"],
        stdout="Success!", stderr="", exit_code=0,
        started_at=time.time(), ended_at=time.time()
    )
    
    calls = [res1, res2, res3]
    prompt_history = []
    async def mock_provider_call(t, phase):
        prompt_history.append(t.prompt)
        return calls.pop(0)
        
    execution._provider_call_with_timeout = mock_provider_call
    
    updated_task = await execution.execute_task(task)
    assert updated_task.status == "completed"
    
    assert len(prompt_history) == 3
    assert "SyntaxError: invalid syntax" in prompt_history[2]
    assert "ImportError: module missing" in prompt_history[2]


def test_read_only_skills_store(tmp_path):
    from navi.skills import SkillStore
    
    store = SkillStore(tmp_path)
    assert store.skills_dir.is_dir()
    store.builtin_skills_dir = tmp_path / "nonexistent_builtins"
    skills = store.list_skills()
    assert isinstance(skills, list)


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
    assert len(mock_invokes) == 1
    
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
    assert len(mock_invokes) == 2
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
    assert len(mock_invokes) == 1
    
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
    assert len(mock_invokes) == 1
