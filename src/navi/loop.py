from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class LoopPhase(StrEnum):
    DECISION = "loop.decision"
    CHECK = "loop.check"
    RECOVERY = "loop.recovery"
    RUNTIME = "runtime"
    PLANNER = "planner"
    CAPABILITY = "capability"
    WORKFLOW_STEP = "workflow.step"
    WORKFLOW_VERIFY = "workflow.verify"


class LoopDecisionKind(StrEnum):
    CONTINUE = "continue"
    RECOVER = "recover"
    CONVERGED = "converged"
    FINALIZE = "finalize"
    BLOCKED = "blocked"
    FAILED = "failed"
    REFLECT_AND_REPLAN = "reflect_and_replan"
    PAUSE_FOR_APPROVAL = "pause_for_approval"


class LoopReason(StrEnum):
    CAPABILITY_FAILURE = "capability_failure"
    CAPABILITY_OBSERVATION_APPENDED = "capability_observation_appended"
    COMPLETION_CHECKER_BLOCKED = "completion_checker_blocked"
    COMPLETION_EVIDENCE_TRUE = "completion_evidence_true"
    PLANNER_OR_PARSER_FAILURE = "planner_or_parser_failure"
    PROVIDER_NO_RESPONSE = "provider_no_response"
    REPEATED_PROGRESS_SIGNATURE = "repeated_progress_signature"
    REPEATED_RECOVERY_SIGNATURE = "repeated_recovery_signature"
    TERMINAL_RESULT = "terminal_result"
    WORKFLOW_STEP_CAPABILITY_FAILURE = "workflow_step_capability_failure"
    WORKFLOW_STEP_COMPLETED = "workflow_step_completed"
    WORKFLOW_STEP_REQUESTED_USER_INPUT = "workflow_step_requested_user_input"
    WORKFLOW_VERIFIER_BLOCKED = "workflow_verifier_blocked"
    WORKFLOW_VERIFIER_PASSED = "workflow_verifier_passed"
    APPROVAL_REQUIRED = "approval_required"


class LoopCheckName(StrEnum):
    CAPABILITY_RESULT = "capability_result"
    COMPLETION_CHECKER = "completion_checker"
    COMPLETION_EVIDENCE = "completion_evidence"
    NO_PROGRESS_GATE = "no_progress_gate"
    PLANNER_RESULT = "planner_result"
    TERMINAL_RESULT = "terminal_result"
    WORKFLOW_CAPABILITY_EVIDENCE_PRESENT = "workflow_capability_evidence_present"
    WORKFLOW_RESOLUTION_SUCCESS = "workflow_resolution_success"
    WORKFLOW_STEP_CHECKER = "workflow_step_checker"
    WORKFLOW_STEP_EVIDENCE_PRESENT = "workflow_step_evidence_present"
    WORKFLOW_STEPS_COMPLETED = "workflow_steps_completed"
    APPROVAL_GATE = "approval_gate"


class TracePhase(StrEnum):
    AGENT_ROLE_RESULT = "agent.role_result"
    CAPABILITY_RESULT = "capability.result"
    CHANNEL_INGRESS = "channel.ingress"
    CHANNEL_EGRESS = "channel.egress"
    PLANNER_CALL_ERROR = "planner.call.error"
    PLANNER_CALL_START = "planner.call.start"
    PLANNER_PARSE_ERROR = "planner.parse_error"
    PLANNER_SYSCALL = "planner.syscall"
    RUNTIME_CONVERGED = "runtime.converged"
    TURN_FINAL = "turn.final"
    TURN_START = "turn.start"


class TraceOutcome(StrEnum):
    SUCCESS = "success"
    DEGRADED = "degraded"
    FAILURE = "failure"
    UNKNOWN = "unknown"


class TraceRunType(StrEnum):
    CHAIN = "chain"
    LLM = "llm"
    TOOL = "tool"


class TraceRunStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"


class TraceFailureDomain(StrEnum):
    CAPABILITY_FAILURE = "capability_failure"
    CHECKER_BLOCKED = "checker_blocked"
    LOOP_NO_PROGRESS = "loop_no_progress"
    MISSING_COMPLETION_CHECK = "missing_completion_check"
    NONE = "none"
    PLANNER_OR_PARSER = "planner_or_parser"
    PROVIDER_NO_RESPONSE = "provider_no_response"
    RUNTIME = "runtime"
    SAFEGUARD_POLICY = "safeguard_policy"
    TRACE_MISSING = "trace_missing"


class LoopSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class LoopCheckResult:
    name: LoopCheckName | str
    passed: bool
    severity: LoopSeverity | str = LoopSeverity.INFO
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": str(self.name),
            "passed": self.passed,
            "severity": str(self.severity),
            "reason": self.reason,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class LoopDecision:
    decision: LoopDecisionKind | str
    reason: LoopReason | str
    phase: LoopPhase | str = ""
    failure_domain: TraceFailureDomain | str = ""
    tool: str = ""
    run_id: str = ""
    workflow_id: str = ""
    step_id: str = ""
    goal_ids: tuple[str, ...] = ()
    progress_signature: str = ""
    checker_results: tuple[LoopCheckResult, ...] = ()
    gate_results: tuple[LoopCheckResult, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": str(self.decision),
            "reason": str(self.reason),
            "phase": str(self.phase),
            "failure_domain": str(self.failure_domain),
            "tool": self.tool,
            "run_id": self.run_id,
            "workflow_id": self.workflow_id,
            "step_id": self.step_id,
            "goal_ids": list(self.goal_ids),
            "progress_signature": self.progress_signature,
            "checker_results": [item.to_dict() for item in self.checker_results],
            "gate_results": [item.to_dict() for item in self.gate_results],
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class TraceRunView:
    id: str
    trace_id: str
    parent_run_id: str
    name: str
    run_type: TraceRunType | str
    status: TraceRunStatus | str
    start_time: float
    end_time: float
    thread_id: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    feedback: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "trace_id": self.trace_id,
            "parent_run_id": self.parent_run_id,
            "name": self.name,
            "run_type": str(self.run_type),
            "status": str(self.status),
            "start_time": self.start_time,
            "end_time": self.end_time,
            "thread_id": self.thread_id,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "tags": list(self.tags),
            "metadata": self.metadata,
            "feedback": self.feedback,
        }


@dataclass(frozen=True)
class LoopDecisionSummary:
    decision: str
    reason: str
    phase: str
    failure_domain: str
    tool: str
    run_id: str
    workflow_id: str
    step_id: str
    failed_checkers: tuple[str, ...] = ()
    failed_gates: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "phase": self.phase,
            "failure_domain": self.failure_domain,
            "tool": self.tool,
            "run_id": self.run_id,
            "workflow_id": self.workflow_id,
            "step_id": self.step_id,
            "failed_checkers": list(self.failed_checkers),
            "failed_gates": list(self.failed_gates),
        }


@dataclass(frozen=True)
class LoopProgressObservation:
    signature: str
    repeated: bool
    count: int = 1
    reason: str = ""


@dataclass
class LoopProgressGate:
    seen_signatures: dict[str, int] = field(default_factory=dict)
    history: list[tuple[str, str]] = field(default_factory=list)

    def observe(self, signature: str, tool: str = "") -> LoopProgressObservation:
        normalized = signature.strip()
        if not normalized:
            return LoopProgressObservation(signature="", repeated=False, count=1)
            
        self.seen_signatures[normalized] = self.seen_signatures.get(normalized, 0) + 1
        self.history.append((tool, normalized))

        # 1. Tool consecutive check (ignoring interleaving of other tools)
        if tool:
            tool_history = [s for t, s in self.history if t == tool]
            if len(tool_history) >= 3:
                consecutive_count = 1
                for i in range(len(tool_history) - 2, -1, -1):
                    if tool_history[i] == tool_history[-1]:
                        consecutive_count += 1
                    else:
                        break
                if consecutive_count >= 3:
                    return LoopProgressObservation(signature=normalized, repeated=True, count=consecutive_count, reason="tool_repeated")

        # 2. Chain cycle detection (A B A B A B)
        n = len(self.history)
        for L in range(1, n // 3 + 1):
            if self.history[-L:] == self.history[-2*L:-L] == self.history[-3*L:-2*L]:
                chain_count = 1
                for i in range(1, n // L + 1):
                    if self.history[-i*L:n if i==1 else -(i-1)*L] == self.history[-L:]:
                        chain_count += 1
                    else:
                        break
                return LoopProgressObservation(signature=normalized, repeated=True, count=chain_count, reason="chain_repeated")

        count = self.seen_signatures[normalized]
        repeated = count >= 5
        
        return LoopProgressObservation(signature=normalized, repeated=repeated, count=count)


NON_OK_LOOP_DECISIONS = frozenset(
    {LoopDecisionKind.BLOCKED, LoopDecisionKind.FAILED}
)
NON_OK_LOOP_DECISION_VALUES = frozenset(item.value for item in NON_OK_LOOP_DECISIONS)
TRACE_FAILURE_DOMAIN_VALUES = frozenset(item.value for item in TraceFailureDomain)


def loop_decision_ok(decision: LoopDecision) -> bool:
    return str(decision.decision) not in NON_OK_LOOP_DECISION_VALUES


def failed_loop_result_names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        if not isinstance(item, dict) or item.get("passed") is not False:
            continue
        name = str(item.get("name") or "").strip()
        if name:
            names.append(name)
    return names


def loop_decision_summary(
    output: dict[str, Any],
    *,
    event_tool: str = "",
    event_run_id: str = "",
) -> LoopDecisionSummary:
    return LoopDecisionSummary(
        decision=str(output.get("decision") or ""),
        reason=str(output.get("reason") or ""),
        phase=str(output.get("phase") or ""),
        failure_domain=trace_failure_domain(output.get("failure_domain")),
        tool=event_tool or str(output.get("tool") or ""),
        run_id=event_run_id or str(output.get("run_id") or ""),
        workflow_id=str(output.get("workflow_id") or ""),
        step_id=str(output.get("step_id") or ""),
        failed_checkers=tuple(failed_loop_result_names(output.get("checker_results"))),
        failed_gates=tuple(failed_loop_result_names(output.get("gate_results"))),
    )


def classify_loop_failure(output: dict[str, Any]) -> TraceFailureDomain:
    summary = loop_decision_summary(output)
    if summary.failure_domain and summary.failure_domain != str(TraceFailureDomain.NONE):
        return TraceFailureDomain(summary.failure_domain)
    failed = {*summary.failed_checkers, *summary.failed_gates}
    if str(LoopCheckName.PLANNER_RESULT) in failed:
        return TraceFailureDomain.PLANNER_OR_PARSER
    if str(LoopCheckName.CAPABILITY_RESULT) in failed or str(LoopCheckName.WORKFLOW_STEP_CHECKER) in failed:
        return TraceFailureDomain.CAPABILITY_FAILURE
    if str(LoopCheckName.COMPLETION_CHECKER) in failed:
        return TraceFailureDomain.CHECKER_BLOCKED
    return TraceFailureDomain.RUNTIME


def classify_loop_blocked(output: dict[str, Any]) -> TraceFailureDomain:
    summary = loop_decision_summary(output)
    if summary.failure_domain and summary.failure_domain != str(TraceFailureDomain.NONE):
        return TraceFailureDomain(summary.failure_domain)
    if str(LoopCheckName.NO_PROGRESS_GATE) in summary.failed_gates:
        return TraceFailureDomain.LOOP_NO_PROGRESS
    return TraceFailureDomain.CHECKER_BLOCKED





def trace_failure_domain(value: Any) -> str:
    text = str(value or "").strip()
    return text if text in TRACE_FAILURE_DOMAIN_VALUES else ""
