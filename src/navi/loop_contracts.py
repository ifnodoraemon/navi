from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from .permission_contract import normalize_permission


class LoopNode(StrEnum):
    PLAN = "plan"
    EXECUTE = "execute"
    EVALUATE = "evaluate"
    REFLECT = "reflect"
    ESCALATE = "escalate"
    PAUSE = "pause"


class LoopTerminalState(StrEnum):
    CONVERGED = "converged"
    PAUSED = "paused"
    WAITING_APPROVAL = "waiting_approval"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    CONFLICTED = "conflicted"
    TIMED_OUT = "timed_out"


class VerificationKind(StrEnum):
    SCHEMA = "schema"
    STATIC = "static"
    UNIT_TEST = "unit_test"
    INTEGRATION_TEST = "integration_test"
    COMMAND_EXIT_CODE = "command_exit_code"
    ARTIFACT_INSPECTION = "artifact_inspection"
    LLM_CHECKER = "llm_checker"
    HUMAN_APPROVAL = "human_approval"


class WorkspaceMode(StrEnum):
    READ_ONLY = "read_only"
    SHADOW = "shadow"
    SANDBOX = "sandbox"


class LockMode(StrEnum):
    READ = "read"
    WRITE = "write"


class ResourceDecision(StrEnum):
    ALLOW = "allow"
    PAUSE = "pause"
    ESCALATE = "escalate"
    BLOCK = "block"


class MergeStatus(StrEnum):
    CLEAN = "clean"
    CONFLICTED = "conflicted"
    NO_OP = "no_op"


DEFAULT_TERMINAL_STATES: tuple[LoopTerminalState, ...] = (
    LoopTerminalState.CONVERGED,
    LoopTerminalState.PAUSED,
    LoopTerminalState.WAITING_APPROVAL,
    LoopTerminalState.BLOCKED,
    LoopTerminalState.FAILED,
    LoopTerminalState.CANCELLED,
    LoopTerminalState.SUPERSEDED,
    LoopTerminalState.CONFLICTED,
    LoopTerminalState.TIMED_OUT,
)


@dataclass(frozen=True)
class GoalSpec:
    objective: str
    scope: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    permission_ceiling: str = "read"
    risk_level: str = "normal"
    owner: str = ""
    resume_policy: str = "checkpoint"
    cancel_policy: str = "mark_cancelled"
    memory_policy: str = "governed"
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.objective.strip():
            raise ValueError("GoalSpec.objective is required")
        if not self.scope:
            raise ValueError("GoalSpec.scope is required")
        normalize_permission(self.permission_ceiling)

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "scope": list(self.scope),
            "constraints": list(self.constraints),
            "acceptance_criteria": list(self.acceptance_criteria),
            "permission_ceiling": self.permission_ceiling,
            "risk_level": self.risk_level,
            "owner": self.owner,
            "resume_policy": self.resume_policy,
            "cancel_policy": self.cancel_policy,
            "memory_policy": self.memory_policy,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class StateTransition:
    source: LoopNode | str
    target: LoopNode | LoopTerminalState | str
    condition: str

    def to_dict(self) -> dict[str, str]:
        return {
            "source": str(self.source),
            "target": str(self.target),
            "condition": self.condition,
        }


@dataclass(frozen=True)
class TimeoutPolicy:
    seconds: float = 120.0
    kill_signal: str = "SIGKILL"
    stdout_tail_bytes: int = 8192
    stderr_tail_bytes: int = 8192

    def validate(self) -> None:
        if self.seconds <= 0:
            raise ValueError("TimeoutPolicy.seconds must be positive")
        if self.stdout_tail_bytes < 0 or self.stderr_tail_bytes < 0:
            raise ValueError("timeout tail sizes must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "seconds": self.seconds,
            "kill_signal": self.kill_signal,
            "stdout_tail_bytes": self.stdout_tail_bytes,
            "stderr_tail_bytes": self.stderr_tail_bytes,
        }


@dataclass(frozen=True)
class VerificationStep:
    kind: VerificationKind | str
    name: str
    required: bool = True
    command: str = ""
    evidence_key: str = ""
    timeout: TimeoutPolicy = field(default_factory=TimeoutPolicy)

    def validate(self) -> None:
        if not self.name.strip():
            raise ValueError("VerificationStep.name is required")
        self.timeout.validate()
        if self.kind in {
            VerificationKind.UNIT_TEST,
            VerificationKind.INTEGRATION_TEST,
            VerificationKind.COMMAND_EXIT_CODE,
        } and not self.command.strip():
            raise ValueError(f"{self.kind} verification requires a command")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "name": self.name,
            "required": self.required,
            "command": self.command,
            "evidence_key": self.evidence_key,
            "timeout": self.timeout.to_dict(),
        }


@dataclass(frozen=True)
class CheckpointPolicy:
    before_state_transition: bool = True
    before_side_effect: bool = True
    persist_node_inputs: bool = True
    persist_node_outputs: bool = True

    def validate(self) -> None:
        if not self.before_state_transition:
            raise ValueError("Durable StateGraph requires checkpoints before state transitions")
        if not self.before_side_effect:
            raise ValueError("side effects require a checkpoint before execution")

    def to_dict(self) -> dict[str, bool]:
        return {
            "before_state_transition": self.before_state_transition,
            "before_side_effect": self.before_side_effect,
            "persist_node_inputs": self.persist_node_inputs,
            "persist_node_outputs": self.persist_node_outputs,
        }


@dataclass(frozen=True)
class WorkspacePolicy:
    mode: WorkspaceMode | str = WorkspaceMode.SHADOW
    require_fingerprint: bool = True
    require_three_way_merge: bool = True
    require_locks: bool = True
    atomic_merge: bool = True

    def validate(self) -> None:
        if self.mode in {WorkspaceMode.SHADOW, WorkspaceMode.SANDBOX}:
            if not self.require_fingerprint:
                raise ValueError("shadow/sandbox workspace requires fingerprint checks")
            if not self.require_three_way_merge:
                raise ValueError("shadow/sandbox workspace requires 3-way merge")
            if not self.atomic_merge:
                raise ValueError("shadow/sandbox workspace requires atomic merge")

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": str(self.mode),
            "require_fingerprint": self.require_fingerprint,
            "require_three_way_merge": self.require_three_way_merge,
            "require_locks": self.require_locks,
            "atomic_merge": self.atomic_merge,
        }


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 10
    reflect_before_retry: bool = True

    def validate(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("RetryPolicy.max_attempts must be at least 1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_attempts": self.max_attempts,
            "reflect_before_retry": self.reflect_before_retry,
        }


@dataclass(frozen=True)
class BudgetPolicy:
    token_budget: int = 0
    call_budget: int = 0
    cost_budget: float = 0.0
    qps_limit: int = 0
    max_concurrent: int = 1

    def validate(self) -> None:
        if self.token_budget < 0:
            raise ValueError("BudgetPolicy.token_budget must be non-negative")
        if self.call_budget < 0:
            raise ValueError("BudgetPolicy.call_budget must be non-negative")
        if self.cost_budget < 0:
            raise ValueError("BudgetPolicy.cost_budget must be non-negative")
        if self.qps_limit < 0:
            raise ValueError("BudgetPolicy.qps_limit must be non-negative")
        if self.max_concurrent < 1:
            raise ValueError("BudgetPolicy.max_concurrent must be at least 1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "token_budget": self.token_budget,
            "call_budget": self.call_budget,
            "cost_budget": self.cost_budget,
            "qps_limit": self.qps_limit,
            "max_concurrent": self.max_concurrent,
        }


@dataclass(frozen=True)
class RollbackPolicy:
    discard_shadow_on_failure: bool = True
    rollback_to_checkpoint: bool = True
    require_human_on_uncertain_side_effect: bool = True

    def to_dict(self) -> dict[str, bool]:
        return {
            "discard_shadow_on_failure": self.discard_shadow_on_failure,
            "rollback_to_checkpoint": self.rollback_to_checkpoint,
            "require_human_on_uncertain_side_effect": self.require_human_on_uncertain_side_effect,
        }


@dataclass(frozen=True)
class EscalationPolicy:
    ask_user_on_conflict: bool = True
    ask_user_on_approval: bool = True
    ask_user_on_uncertain_side_effect: bool = True

    def to_dict(self) -> dict[str, bool]:
        return {
            "ask_user_on_conflict": self.ask_user_on_conflict,
            "ask_user_on_approval": self.ask_user_on_approval,
            "ask_user_on_uncertain_side_effect": self.ask_user_on_uncertain_side_effect,
        }


@dataclass(frozen=True)
class LoopSpec:
    id: str
    goal_id: str
    goal: GoalSpec
    state_graph: tuple[StateTransition, ...]
    allowed_capabilities: tuple[str, ...]
    verification_ladder: tuple[VerificationStep, ...]
    workspace_policy: WorkspacePolicy = field(default_factory=WorkspacePolicy)
    checkpoint_policy: CheckpointPolicy = field(default_factory=CheckpointPolicy)
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    budget_policy: BudgetPolicy = field(default_factory=BudgetPolicy)
    rollback_policy: RollbackPolicy = field(default_factory=RollbackPolicy)
    escalation_policy: EscalationPolicy = field(default_factory=EscalationPolicy)
    terminal_states: tuple[LoopTerminalState | str, ...] = DEFAULT_TERMINAL_STATES
    created_at: float = field(default_factory=time.time)

    @classmethod
    def from_goal(
        cls,
        goal: GoalSpec,
        *,
        goal_id: str,
        allowed_capabilities: tuple[str, ...],
        verification_ladder: tuple[VerificationStep, ...],
    ) -> LoopSpec:
        spec = cls(
            id=uuid.uuid4().hex,
            goal_id=goal_id,
            goal=goal,
            state_graph=default_state_graph(),
            allowed_capabilities=allowed_capabilities,
            verification_ladder=verification_ladder,
        )
        spec.validate()
        return spec

    def validate(self) -> None:
        self.goal.validate()
        if not self.goal_id.strip():
            raise ValueError("LoopSpec.goal_id is required")
        if not self.state_graph:
            raise ValueError("LoopSpec.state_graph is required")
        if not self.allowed_capabilities:
            raise ValueError("LoopSpec.allowed_capabilities is required")
        if not self.verification_ladder:
            raise ValueError("LoopSpec.verification_ladder is required")
        for step in self.verification_ladder:
            step.validate()
        self.workspace_policy.validate()
        self.checkpoint_policy.validate()
        self.retry_policy.validate()
        self.budget_policy.validate()
        missing = set(DEFAULT_TERMINAL_STATES) - {LoopTerminalState(str(item)) for item in self.terminal_states}
        if missing:
            values = ", ".join(sorted(str(item) for item in missing))
            raise ValueError(f"LoopSpec.terminal_states missing: {values}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "goal_id": self.goal_id,
            "goal": self.goal.to_dict(),
            "state_graph": [item.to_dict() for item in self.state_graph],
            "allowed_capabilities": list(self.allowed_capabilities),
            "verification_ladder": [item.to_dict() for item in self.verification_ladder],
            "workspace_policy": self.workspace_policy.to_dict(),
            "checkpoint_policy": self.checkpoint_policy.to_dict(),
            "retry_policy": self.retry_policy.to_dict(),
            "budget_policy": self.budget_policy.to_dict(),
            "rollback_policy": self.rollback_policy.to_dict(),
            "escalation_policy": self.escalation_policy.to_dict(),
            "terminal_states": [str(item) for item in self.terminal_states],
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class WorkspaceLock:
    owner_run_id: str
    resource: str
    mode: LockMode | str
    lease_expiry: float

    def active(self, *, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        return self.lease_expiry > current

    def conflicts_with(self, other: WorkspaceLock, *, now: float | None = None) -> bool:
        if not self.active(now=now) or not other.active(now=now):
            return False
        if self.resource != other.resource:
            return False
        if self.owner_run_id == other.owner_run_id:
            return False
        return self.mode == LockMode.WRITE or other.mode == LockMode.WRITE

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner_run_id": self.owner_run_id,
            "resource": self.resource,
            "mode": str(self.mode),
            "lease_expiry": self.lease_expiry,
        }


@dataclass(frozen=True)
class VaultHandle:
    uri: str
    purpose: str
    env_var: str = ""

    def validate(self) -> None:
        if not self.uri.startswith("secret://"):
            raise ValueError("VaultHandle.uri must use secret://")
        if not self.purpose.strip():
            raise ValueError("VaultHandle.purpose is required")

    def to_prompt_dict(self) -> dict[str, str]:
        self.validate()
        data = {"handle": self.uri, "purpose": self.purpose}
        if self.env_var:
            data["env_var"] = self.env_var
        return data


@dataclass(frozen=True)
class TimeoutEvidence:
    command: str
    duration_seconds: float
    timeout_seconds: float
    stdout_tail: str = ""
    stderr_tail: str = ""
    exit_status: str = "timed_out"

    def to_checker_fact(self) -> dict[str, Any]:
        return {
            "error_type": "TimeoutError",
            "command": self.command,
            "duration_seconds": self.duration_seconds,
            "timeout_seconds": self.timeout_seconds,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
            "exit_status": self.exit_status,
        }


@dataclass(frozen=True)
class MergePlan:
    baseline_revision: str
    shadow_revision: str
    current_revision: str
    changed_paths: tuple[str, ...]

    def real_workspace_changed(self) -> bool:
        return bool(self.baseline_revision and self.current_revision and self.baseline_revision != self.current_revision)

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_revision": self.baseline_revision,
            "shadow_revision": self.shadow_revision,
            "current_revision": self.current_revision,
            "changed_paths": list(self.changed_paths),
            "real_workspace_changed": self.real_workspace_changed(),
        }


@dataclass(frozen=True)
class MergeResult:
    status: MergeStatus | str
    conflicts: tuple[str, ...] = ()
    artifact_path: str = ""

    def terminal_state(self) -> LoopTerminalState | None:
        if self.status == MergeStatus.CONFLICTED:
            return LoopTerminalState.CONFLICTED
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": str(self.status),
            "conflicts": list(self.conflicts),
            "artifact_path": self.artifact_path,
        }


@dataclass(frozen=True)
class BudgetState:
    decision: ResourceDecision | str = ResourceDecision.ALLOW
    token_budget_remaining: int | None = None
    call_budget_remaining: int | None = None
    cost_budget_remaining: float | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": str(self.decision),
            "token_budget_remaining": self.token_budget_remaining,
            "call_budget_remaining": self.call_budget_remaining,
            "cost_budget_remaining": self.cost_budget_remaining,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class WorkspaceState:
    workspace: str
    baseline_revision: str = ""
    current_fingerprint: str = ""
    shadow_workspace: str = ""
    shadow_workspaces: tuple[dict[str, Any], ...] = ()
    dirty_paths: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "baseline_revision": self.baseline_revision,
            "current_fingerprint": self.current_fingerprint,
            "shadow_workspace": self.shadow_workspace,
            "shadow_workspaces": list(self.shadow_workspaces),
            "dirty_paths": list(self.dirty_paths),
        }


@dataclass(frozen=True)
class LoopRunState:
    run_id: str
    goal_id: str
    loop_spec_id: str
    node: LoopNode | str = LoopNode.PLAN
    terminal_state: LoopTerminalState | str = ""
    checkpoint_id: str = ""
    attempt: int = 1
    parent_run_id: str = ""
    child_run_ids: tuple[str, ...] = ()
    locked_resources: tuple[str, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)
    version: int = 0
    lease_owner: str = ""
    lease_expires_at: float = 0.0

    def is_terminal(self) -> bool:
        return bool(str(self.terminal_state).strip())

    def transition(
        self,
        *,
        node: LoopNode | str,
        checkpoint_id: str,
        terminal_state: LoopTerminalState | str = "",
        evidence: dict[str, Any] | None = None,
    ) -> LoopRunState:
        if self.is_terminal():
            raise ValueError("terminal LoopRunState cannot transition")
        if not checkpoint_id.strip():
            raise ValueError("LoopRunState transition requires a checkpoint_id")
        merged_evidence = dict(self.evidence)
        if evidence:
            merged_evidence.update(evidence)
        next_attempt = self.attempt
        if str(self.node) == str(LoopNode.REFLECT) and str(node) == str(LoopNode.PLAN):
            next_attempt += 1
        if (
            str(self.node) == str(LoopNode.EVALUATE)
            and str(node) == str(LoopNode.PLAN)
        ):
            # Each EVALUATE -> PLAN iteration consumes one attempt so retries
            # remain bounded independently of semantic-checker output.
            next_attempt += 1
        return replace(
            self,
            node=node,
            checkpoint_id=checkpoint_id,
            terminal_state=terminal_state,
            evidence=merged_evidence,
            attempt=next_attempt,
            updated_at=time.time(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "goal_id": self.goal_id,
            "loop_spec_id": self.loop_spec_id,
            "node": str(self.node),
            "terminal_state": str(self.terminal_state),
            "checkpoint_id": self.checkpoint_id,
            "attempt": self.attempt,
            "parent_run_id": self.parent_run_id,
            "child_run_ids": list(self.child_run_ids),
            "locked_resources": list(self.locked_resources),
            "evidence": dict(self.evidence),
            "updated_at": self.updated_at,
            "version": self.version,
            "lease_owner": self.lease_owner,
            "lease_expires_at": self.lease_expires_at,
        }


@dataclass(frozen=True)
class CurrentStateSnapshot:
    goal_state: dict[str, Any]
    loop_run_state: LoopRunState | None = None
    approval_state: dict[str, Any] = field(default_factory=dict)
    budget_state: BudgetState = field(default_factory=BudgetState)
    workspace_state: WorkspaceState | None = None
    locks: tuple[WorkspaceLock, ...] = ()
    provider_state: dict[str, Any] = field(default_factory=dict)
    delegation_state: dict[str, Any] = field(default_factory=dict)
    vault_handles: tuple[VaultHandle, ...] = ()
    connector_state: dict[str, Any] = field(default_factory=dict)

    def control_facts(self) -> dict[str, Any]:
        return {
            "goal_state": dict(self.goal_state),
            "loop_run_state": self.loop_run_state.to_dict() if self.loop_run_state else {},
            "approval_state": dict(self.approval_state),
            "budget_state": self.budget_state.to_dict(),
            "workspace_state": self.workspace_state.to_dict() if self.workspace_state else {},
            "lock_state": [lock.to_dict() for lock in self.locks],
            "provider_state": dict(self.provider_state),
            "delegation_state": dict(self.delegation_state),
            "vault_handle_state": [handle.to_prompt_dict() for handle in self.vault_handles],
            "connector_state": dict(self.connector_state),
        }


def default_state_graph() -> tuple[StateTransition, ...]:
    base_edges = (
        StateTransition(LoopNode.PLAN, LoopNode.EXECUTE, "plan_ready"),
        StateTransition(LoopNode.PLAN, LoopNode.ESCALATE, "needs_user_or_approval"),
        StateTransition(LoopNode.PLAN, LoopNode.PAUSE, "resource_pause"),
        StateTransition(LoopNode.PLAN, LoopNode.ESCALATE, "resource_escalate"),
        StateTransition(LoopNode.PLAN, LoopTerminalState.BLOCKED, "resource_blocked"),
        StateTransition(LoopNode.PLAN, LoopNode.REFLECT, "planner_failed"),
        StateTransition(LoopNode.PLAN, LoopTerminalState.FAILED, "planner_failed"),
        StateTransition(LoopNode.EXECUTE, LoopNode.EVALUATE, "side_effect_recorded"),
        StateTransition(LoopNode.EXECUTE, LoopNode.REFLECT, "capability_failed"),
        StateTransition(LoopNode.EXECUTE, LoopTerminalState.TIMED_OUT, "hard_timeout"),
        StateTransition(LoopNode.EXECUTE, LoopNode.PAUSE, "resource_pause"),
        StateTransition(LoopNode.EXECUTE, LoopNode.ESCALATE, "resource_escalate"),
        StateTransition(LoopNode.EXECUTE, LoopNode.ESCALATE, "approval_required"),
        StateTransition(LoopNode.EXECUTE, LoopTerminalState.BLOCKED, "repeated_progress_signature"),
        StateTransition(LoopNode.EXECUTE, LoopTerminalState.BLOCKED, "resource_blocked"),
        StateTransition(LoopNode.EVALUATE, LoopTerminalState.CONVERGED, "checker_passed"),
        StateTransition(LoopNode.EVALUATE, LoopNode.PLAN, "continue_iteration"),
        StateTransition(LoopNode.EVALUATE, LoopNode.REFLECT, "checker_failed"),
        StateTransition(LoopNode.EVALUATE, LoopTerminalState.CONFLICTED, "merge_conflict"),
        StateTransition(LoopNode.EVALUATE, LoopTerminalState.TIMED_OUT, "hard_timeout"),
        StateTransition(LoopNode.EVALUATE, LoopNode.PAUSE, "resource_pause"),
        StateTransition(LoopNode.EVALUATE, LoopNode.ESCALATE, "resource_escalate"),
        StateTransition(LoopNode.EVALUATE, LoopNode.ESCALATE, "side_effect_commit_required"),
        StateTransition(LoopNode.EVALUATE, LoopTerminalState.BLOCKED, "resource_blocked"),
        StateTransition(LoopNode.EVALUATE, LoopTerminalState.BLOCKED, "no_route_available"),
        StateTransition(LoopNode.REFLECT, LoopNode.PLAN, "new_route_available"),
        StateTransition(LoopNode.REFLECT, LoopTerminalState.BLOCKED, "no_route_available"),
        StateTransition(LoopNode.REFLECT, LoopTerminalState.FAILED, "checker_rejected"),
        StateTransition(LoopNode.ESCALATE, LoopTerminalState.WAITING_APPROVAL, "approval_required"),
        StateTransition(LoopNode.ESCALATE, LoopTerminalState.WAITING_APPROVAL, "resource_escalate"),
        StateTransition(LoopNode.PAUSE, LoopTerminalState.PAUSED, "resource_or_user_pause"),
    )
    control_edges = tuple(
        edge
        for node in LoopNode
        for edge in (
            StateTransition(node, LoopTerminalState.CANCELLED, "cancel_requested"),
            StateTransition(node, LoopTerminalState.SUPERSEDED, "superseded"),
        )
    )
    return base_edges + control_edges
