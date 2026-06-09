import json
import time
from pathlib import Path
from typing import Any

from .capabilities import CapabilityContext, CapabilityRegistry
from .config import load_config
from .execution import ExecutionResult, ExecutionProtocol, INTERNAL_EXECUTION_PROVIDER
from .provider import build_provider, ModelPool
from .runs import Run
from .syscalls import ModelSyscallPlanner
from .tools import REACT_CONTEXT

MODEL_TERMINAL_SYSCALLS = frozenset({"completion", "chat"})

class ReActRunner:
    def __init__(self, *, home: Path, provider: ModelPool | None = None):
        self.home = home
        self.config = load_config(home)
        self.provider = provider if provider is not None else build_provider(self.config.model)
        self.planner = ModelSyscallPlanner(self.provider)

    async def run_task(self, task: Run) -> ExecutionResult:
        from navi.execution import _task_workspace
        workspace = _task_workspace(task)
        started_at = time.time()
        
        registry = CapabilityRegistry(
            home=self.home,
            project_dir=workspace,
            permission_ceiling="write",
            execution_context=REACT_CONTEXT,
        )
        context = CapabilityContext(
            home=self.home,
            peer_id=task.peer_id,
            sender_id=task.sender_id,
            source=task.source,
            permission_ceiling="write",
            workspace=str(workspace),
        )
        
        observations: list[str] = []
        steps_taken: list[dict[str, Any]] = []
        final_summary = ""
        exit_code = 1
        
        while True:
            planner_specs = registry.planner_specs(permission_ceiling=context.permission_ceiling)
            # Fake a conversation context with the task prompt
            conv_context = f"Task objective:\n{task.prompt}\n\nPreparation summary:\n{task.plan_summary or '(none)'}"
            
            syscall = await self.planner.plan(
                task.prompt,
                tools=planner_specs,
                conversation_context=conv_context,
                observations=observations,
                permission_ceiling=context.permission_ceiling,
                model_roles=[],
            )
            
            if syscall.tool == "system.planner_error":
                final_summary = f"Planner error: {syscall.reason}"
                break
                
            if syscall.tool in MODEL_TERMINAL_SYSCALLS:
                final_summary = syscall.reason or str(syscall.args)
                exit_code = 0
                break
                
            invoked = await registry.invoke(
                syscall.tool,
                syscall.args,
                permission=syscall.permission,
                context=context,
            )
            
            step_record = {
                "tool": syscall.tool,
                "permission": syscall.permission,
                "args": syscall.args,
                "ok": invoked.ok,
                "observation": invoked.observation,
                "message": invoked.message,
                "facts": invoked.facts or {},
                "terminal": invoked.terminal,
            }
            steps_taken.append(step_record)
            
            obs_text = f"Action: {syscall.tool}({json.dumps(syscall.args)})\nResult: {'SUCCESS' if invoked.ok else 'FAILED'}\nObservation: {invoked.observation}"
            observations.append(obs_text)
            
            if invoked.terminal:
                final_summary = invoked.observation
                exit_code = 0 if invoked.ok else 1
                break
        protocol = ExecutionProtocol.internal_status(
            run_id=task.id,
            phase="execute",
            status="completed" if exit_code == 0 else "failed",
            summary=final_summary,
            reason="ReAct execution finished",
            action_kind="react_loop",
        )
        evidence = _react_evidence(steps_taken)
        if not evidence and exit_code == 0:
            evidence.append(
                {
                    "kind": "capability_result",
                    "tool": "final.answer",
                    "ok": True,
                    "summary": final_summary,
                    "attempt": 1,
                }
            )
        verified = exit_code == 0
        protocol = ExecutionProtocol(
            version=protocol.version,
            phase=protocol.phase,
            run_id=protocol.run_id,
            plan_id=protocol.plan_id,
            steps=[{"actions": steps_taken}],
            completion={
                "status": "completed" if verified else "failed",
                "summary": final_summary,
            },
            evidence=evidence,
            verification={
                "status": "verified" if verified else "failed",
                "reason": (
                    "ReAct execution completed"
                    if verified
                    else "ReAct execution has failed capability evidence"
                ),
                "checks": ["ReAct loop completed"] if verified else [],
            }
        )
            
        return ExecutionResult(
            provider=INTERNAL_EXECUTION_PROVIDER,
            phase="execute",
            command=["navi", "react", task.id],
            stdout=final_summary,
            stderr="" if verified else final_summary,
            exit_code=0 if verified else 1,
            started_at=started_at,
            ended_at=time.time(),
            protocol=protocol,
        )


def _react_evidence(steps_taken: list[dict[str, Any]]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for index, step in enumerate(steps_taken):
        evidence.append(
            {
                "kind": "capability_result",
                "tool": step.get("tool", ""),
                "ok": bool(step.get("ok")),
                "summary": str(step.get("observation") or step.get("message") or "")[:1600],
                "attempt": index + 1,
                "facts": step.get("facts") if isinstance(step.get("facts"), dict) else {},
            }
        )
    return evidence
