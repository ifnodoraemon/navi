from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .capabilities import CapabilityRegistry
from .capabilities_types import CapabilityContext
from .checker import CheckerReport, DeterministicChecker
from .harness import Harness, HarnessCommand, HarnessResult
from .loop import (
    LoopCheckName,
    LoopCheckResult,
    LoopDecision,
    LoopDecisionKind,
    LoopPhase,
    LoopReason,
    TraceFailureDomain,
    TracePhase,
)
from .loop_contracts import (
    LoopNode,
    LoopRunState,
    LoopSpec,
    LoopTerminalState,
    LockMode,
    MergeStatus,
    ResourceDecision,
    VerificationKind,
    VerificationStep,
    WorkspaceMode,
)
from .loop_runs import LoopCheckpoint, LoopRunStore

from .memory import ACTIVE_MEMORY_CONTEXT_LIMIT
from .provider import ChatMessage, ModelPool
from .runtime import AgentRuntime
from .syscalls import ModelSyscallPlanner
from .resource_gateway import GlobalResourceGateway, ResourceGrant, ResourceLimits, ResourceRequest
from .text_utils import truncate_middle
from .trace import TraceStore
from .workspaces import LockAcquireResult

PLANNER_CONTEXT_MESSAGE_LIMIT = 200
PLANNER_CONTEXT_RECENT_MESSAGES = 12
PLANNER_CONTEXT_MAX_CHARS = 12_000
PLANNER_CONTEXT_OLDER_PREVIEW_MESSAGES = 8
PLANNER_CONTEXT_OLDER_PREVIEW_CHARS = 220
PLANNER_CONTEXT_RECENT_MESSAGE_MAX_CHARS = 2_000
PLANNER_MEMORY_ITEM_MAX_CHARS = 800


@dataclass(frozen=True)
class StateGraphRunResult:
    run_state: LoopRunState
    checker_report: CheckerReport | None = None
    resource_grants: tuple[ResourceGrant, ...] = ()
    harness_results: tuple[HarnessResult, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def terminal_state(self) -> str:
        return str(self.run_state.terminal_state)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_state": self.run_state.to_dict(),
            "checker_report": self.checker_report.to_dict() if self.checker_report else {},
            "resource_grants": [grant.to_dict() for grant in self.resource_grants],
            "harness_results": [result.to_facts() for result in self.harness_results],
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class PlannedCapabilityStep:
    tool: str
    args: dict[str, Any]
    permission: str
    model_role: str = "executor"
    reason: str = ""
    used_memory_ids: tuple[str, ...] = ()
    memory_activation: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "args": dict(self.args),
            "permission": self.permission,
            "model_role": self.model_role,
            "reason": self.reason,
            "used_memory_ids": list(self.used_memory_ids),
            "memory_activation": dict(self.memory_activation),
        }


@dataclass(frozen=True)
class ExecutedCapabilityStep:
    ok: bool
    action: str
    facts: dict[str, Any]
    message: str = ""
    error_reason: str = ""
    terminal: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "action": self.action,
            "facts": dict(self.facts),
            "message": self.message,
            "error_reason": self.error_reason,
            "terminal": self.terminal,
        }


@dataclass(frozen=True)
class ReflectionDecision:
    retry: bool
    reason_code: str
    facts: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "retry": self.retry,
            "reason_code": self.reason_code,
            "facts": dict(self.facts),
        }


@dataclass(frozen=True)
class SemanticCheckDecision:
    passed: bool
    should_continue: bool
    next_step_hint: str
    user_message: str
    evidence_summary: str = ""

    def to_facts(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "ok": self.passed,
            "should_continue": self.should_continue,
            "next_step_hint": self.next_step_hint,
            "user_message": self.user_message,
            "evidence_summary": self.evidence_summary,
            "evaluator_role": "checker",
            "isolated_context": True,
        }



class CapabilityRecoveryPort:
    """Recovery node port that turns failed capability executions into retry facts."""

    def recover(
        self,
        spec: LoopSpec,
        state: LoopRunState,
        *,
        executed: ExecutedCapabilityStep,
    ) -> ReflectionDecision:
        recovery_facts = {
            "trigger": "capability.failed",
            "reason_code": "execution_failed",
            "blocked": False,
            "failure_domain": "executor",
            "loop_run_id": state.run_id,
            "attempt": state.attempt,
            "goal_id": spec.goal_id,
            "error_reason": executed.error_reason,
            "message": executed.message,
            "facts": executed.facts,
        }
        return ReflectionDecision(
            retry=state.attempt < spec.retry_policy.max_attempts,
            reason_code="execution_failed",
            facts={
                "recovery": recovery_facts,
                "recovery_fact": json.dumps(
                    {
                        "fact_type": "capability_execution_failed",
                        "facts": recovery_facts,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ),
            },
        )

class RecoveryReflectorPort:
    """Reflector node port that turns failed evidence into retry facts."""

    def reflect(
        self,
        spec: LoopSpec,
        state: LoopRunState,
        *,
        checker_report: CheckerReport,
        evidence: dict[str, Any],
        harness_results: tuple[HarnessResult, ...],
    ) -> ReflectionDecision:
        reason_code = _checker_reason_code(checker_report)
        recovery_facts = {
            "trigger": "loop.check",
            "reason_code": reason_code,
            "blocked": True,
            "failure_domain": "checker_blocked",
            "loop_run_id": state.run_id,
            "attempt": state.attempt,
            "goal_id": spec.goal_id,
            "checker_report": checker_report.to_dict(),
            "harness_results": [item.to_facts() for item in harness_results],
        }
        return ReflectionDecision(
            retry=reason_code != "no_route_available"
            and state.attempt < spec.retry_policy.max_attempts,
            reason_code=reason_code,
            facts={
                "recovery": recovery_facts,
                "recovery_fact": json.dumps(
                    {
                        "fact_type": "loop_checker_fact",
                        "facts": recovery_facts,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ),
            },
        )


class LLMSemanticCheckerPort:
    """Isolated semantic checker for LLM_CHECKER verification steps."""

    def __init__(self, *, runtime: AgentRuntime):
        self.runtime = runtime

    _OUTPUT_SCHEMA = {
        "name": "semantic_check_result",
        "strict": False,
        "schema": {
            "type": "object",
            "properties": {
                "passed": {"type": "boolean"},
                "should_continue": {"type": "boolean"},
                "next_step_hint": {"type": "string"},
                "user_message": {"type": "string"},
                "evidence_summary": {"type": "string"},
            },
            "required": [
                "passed",
                "should_continue",
                "next_step_hint",
                "user_message",
                "evidence_summary",
            ],
            "additionalProperties": False,
        },
    }

    async def assess(
        self,
        spec: LoopSpec,
        state: LoopRunState,
        *,
        executed: ExecutedCapabilityStep,
    ) -> SemanticCheckDecision:
        response = await self.runtime.provider.complete_for(
            "checker",
            [
                ChatMessage(
                    "system",
                    (
                        "You are Navi's isolated semantic checker. Judge the final "
                        "capability evidence against the objective and acceptance "
                        "criteria. You are not the maker: do not rely on planner "
                        "reasoning, attempt history, or prior self-assessment. "
                        "Use only the objective, criteria, attempt number, and "
                        "last capability result provided."
                    ),
                ),
                ChatMessage(
                    "user",
                    json.dumps(
                        {
                            "objective": spec.goal.objective,
                            "acceptance_criteria": list(spec.goal.acceptance_criteria),
                            "attempt": state.attempt,
                            "max_attempts": spec.retry_policy.max_attempts,
                            "last_capability": executed.to_dict(),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ),
            ],
            output_schema=self._OUTPUT_SCHEMA,
        )
        return self._parse(response)

    @staticmethod
    def _parse(response: str) -> SemanticCheckDecision:
        try:
            data = json.loads(response)
        except json.JSONDecodeError:
            return SemanticCheckDecision(
                passed=False,
                should_continue=False,
                next_step_hint="",
                user_message="",
                evidence_summary="checker returned invalid JSON",
            )
        return SemanticCheckDecision(
            passed=bool(data.get("passed", False)),
            should_continue=bool(data.get("should_continue", False)),
            next_step_hint=str(data.get("next_step_hint", "")),
            user_message=str(data.get("user_message", "")),
            evidence_summary=str(data.get("evidence_summary", "")),
        )


class LLMReflectorPort:
    """Agentic reflector: asks the LLM whether the goal is achieved.

    This replaces the deterministic ``retry = attempt < max`` rule with an
    LLM judgement. The LLM sees:
      - the user's objective
      - the last capability execution (tool, args, ok, facts, message)
      - the attempt count and remaining budget
    and returns a structured decision:
      - ``goal_achieved`` (bool) — has the user's objective been satisfied?
      - ``should_continue`` (bool) — is it worth trying another capability?
      - ``next_step_hint`` (str) — a concrete suggestion for the next plan
        (which tool to try, what to search for, what to ask the user)
      - ``user_message`` (str) — what to tell the user right now
    """

    def __init__(self, *, runtime: AgentRuntime):
        self.runtime = runtime

    _OUTPUT_SCHEMA = {
        "name": "reflection_decision",
        "strict": False,
        "schema": {
            "type": "object",
            "properties": {
                "goal_achieved": {"type": "boolean"},
                "should_continue": {"type": "boolean"},
                "next_step_hint": {"type": "string"},
                "user_message": {"type": "string"},
            },
            "required": [
                "goal_achieved",
                "should_continue",
                "next_step_hint",
                "user_message",
            ],
            "additionalProperties": False,
        },
    }

    async def assess(
        self,
        spec: LoopSpec,
        state: LoopRunState,
        *,
        executed: ExecutedCapabilityStep,
        evidence: dict[str, Any],
    ) -> ReflectionDecision:
        """Judge whether the goal is achieved after a capability execution."""
        decision = await self._ask_llm(spec, state, executed=executed)
        max_attempts = spec.retry_policy.max_attempts
        if decision.goal_achieved:
            return ReflectionDecision(
                retry=False,
                reason_code="goal_achieved",
                facts={
                    "goal_achieved": True,
                    "user_message": decision.user_message,
                    "next_step_hint": decision.next_step_hint,
                },
            )
        if not decision.should_continue or state.attempt >= max_attempts:
            return ReflectionDecision(
                retry=False,
                reason_code="no_route_available",
                facts={
                    "goal_achieved": False,
                    "should_continue": decision.should_continue,
                    "attempt": state.attempt,
                    "max_attempts": max_attempts,
                    "user_message": decision.user_message,
                    "next_step_hint": decision.next_step_hint,
                },
            )
        return ReflectionDecision(
            retry=True,
            reason_code="new_route_available",
            facts={
                "goal_achieved": False,
                "should_continue": True,
                "attempt": state.attempt,
                "max_attempts": max_attempts,
                "user_message": decision.user_message,
                "next_step_hint": decision.next_step_hint,
            },
        )

    async def _ask_llm(
        self,
        spec: LoopSpec,
        state: LoopRunState,
        *,
        executed: ExecutedCapabilityStep,
    ) -> "_LLMReflection":
        system_prompt = (
            "You are Navi's reflection agent. After each capability execution, "
            "judge whether the user's objective has been achieved.\n\n"
            "Rules:\n"
            "- goal_achieved=true ONLY when the last capability result concretely "
            "satisfies the objective (e.g. the file was actually sent).\n"
            "- If the last capability returned empty/no-op results, the goal is "
            "NOT achieved — set should_continue=true and propose a DIFFERENT "
            "capability in next_step_hint (e.g. search the filesystem, ask the "
            "user for the file name).\n"
            "- If you have exhausted reasonable approaches, set "
            "should_continue=false and explain what the user should do.\n"
            "- user_message is what Navi sends to the user right now. Be concise, "
            "honest, and actionable. Never say 'I cannot do this' if another tool "
            "could help.\n"
        )
        user_prompt = json.dumps(
            {
                "objective": spec.goal.objective,
                "attempt": state.attempt,
                "max_attempts": spec.retry_policy.max_attempts,
                "last_capability": {
                    "tool": "n/a",
                    "ok": executed.ok,
                    "action": executed.action,
                    "facts": executed.facts,
                    "message": executed.message,
                    "error_reason": executed.error_reason,
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        response = await self.runtime.provider.complete_for(
            "reflector",
            [
                ChatMessage("system", system_prompt),
                ChatMessage("user", user_prompt),
            ],
            output_schema=self._OUTPUT_SCHEMA,
        )
        return self._parse(response)

    @staticmethod
    def _parse(response: str) -> "_LLMReflection":
        try:
            data = json.loads(response)
        except json.JSONDecodeError:
            return _LLMReflection(
                goal_achieved=False,
                should_continue=False,
                next_step_hint="",
                user_message="",
            )
        return _LLMReflection(
            goal_achieved=bool(data.get("goal_achieved", False)),
            should_continue=bool(data.get("should_continue", False)),
            next_step_hint=str(data.get("next_step_hint", "")),
            user_message=str(data.get("user_message", "")),
        )


@dataclass(frozen=True)
class _LLMReflection:
    goal_achieved: bool
    should_continue: bool
    next_step_hint: str
    user_message: str


@dataclass(frozen=True)
class PlannerConversationContext:
    text: str
    facts: dict[str, Any]


@dataclass(frozen=True)
class PlannerMemoryContext:
    text: str
    facts: dict[str, Any]
    candidate_ids: tuple[str, ...]


@dataclass(frozen=True)
class SideEffectSagaResult:
    action: str
    ok: bool
    scope: str
    state: str
    artifact: str = ""
    commit: str = ""
    compensate: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "ok": self.ok,
            "scope": self.scope,
            "state": self.state,
            "artifact": self.artifact,
            "commit": self.commit,
            "compensate": self.compensate,
            "reason": self.reason,
        }


class SideEffectSagaPort:
    def __init__(self, *, home: Path):
        self.home = home

    def commit(self, side_effect: dict[str, Any]) -> SideEffectSagaResult:
        scope = str(side_effect.get("scope") or "")
        state = str(side_effect.get("state") or "")
        artifact = str(side_effect.get("artifact") or "")
        commit = str(side_effect.get("commit") or "")
        compensate = str(side_effect.get("compensate") or "")
        if state not in {"staged", "prepared"}:
            return SideEffectSagaResult(
                action="commit",
                ok=True,
                scope=scope,
                state=state or "no_staged_side_effect",
                artifact=artifact,
                commit=commit,
                compensate=compensate,
                reason="side_effect_not_staged",
            )
        if commit and commit.endswith(".dispatch_outbox"):
            return SideEffectSagaResult(
                action="commit",
                ok=True,
                scope=scope,
                state="released_for_connector_commit",
                artifact=artifact,
                commit=commit,
                compensate=compensate,
                reason="connector_runtime_dispatch_required",
            )
        return SideEffectSagaResult(
            action="commit",
            ok=False,
            scope=scope,
            state="commit_handler_missing",
            artifact=artifact,
            commit=commit,
            compensate=compensate,
            reason="no_state_graph_commit_handler",
        )

    def compensate(self, side_effect: dict[str, Any]) -> SideEffectSagaResult:
        scope = str(side_effect.get("scope") or "")
        state = str(side_effect.get("state") or "")
        artifact = str(side_effect.get("artifact") or "")
        commit = str(side_effect.get("commit") or "")
        compensate = str(side_effect.get("compensate") or "")
        if state not in {"staged", "prepared"}:
            return SideEffectSagaResult(
                action="compensate",
                ok=True,
                scope=scope,
                state=state or "no_staged_side_effect",
                artifact=artifact,
                commit=commit,
                compensate=compensate,
                reason="side_effect_not_staged",
            )
        if compensate != "filesystem.remove_staged_outbound":
            return SideEffectSagaResult(
                action="compensate",
                ok=False,
                scope=scope,
                state="compensate_handler_missing",
                artifact=artifact,
                commit=commit,
                compensate=compensate,
                reason="no_state_graph_compensate_handler",
            )
        artifact_path = Path(artifact).expanduser()
        allowed_root = self.home.resolve()
        try:
            resolved = artifact_path.resolve()
        except OSError as exc:
            return SideEffectSagaResult(
                action="compensate",
                ok=False,
                scope=scope,
                state="compensate_path_error",
                artifact=artifact,
                commit=commit,
                compensate=compensate,
                reason=str(exc),
            )
        if not resolved.is_relative_to(allowed_root) or "outbox" not in resolved.parts:
            return SideEffectSagaResult(
                action="compensate",
                ok=False,
                scope=scope,
                state="compensate_path_blocked",
                artifact=str(resolved),
                commit=commit,
                compensate=compensate,
                reason="artifact_outside_connector_outbox",
            )
        if not resolved.exists():
            return SideEffectSagaResult(
                action="compensate",
                ok=True,
                scope=scope,
                state="compensated_missing",
                artifact=str(resolved),
                commit=commit,
                compensate=compensate,
                reason="artifact_already_absent",
            )
        if not resolved.is_file():
            return SideEffectSagaResult(
                action="compensate",
                ok=False,
                scope=scope,
                state="compensate_path_blocked",
                artifact=str(resolved),
                commit=commit,
                compensate=compensate,
                reason="artifact_not_file",
            )
        resolved.unlink()
        return SideEffectSagaResult(
            action="compensate",
            ok=True,
            scope=scope,
            state="compensated",
            artifact=str(resolved),
            commit=commit,
            compensate=compensate,
            reason="staged_artifact_removed",
        )


def _planner_conversation_context(
    *,
    session_id: str,
    messages: list[Any],
) -> PlannerConversationContext:
    relevant = [msg for msg in messages if str(getattr(msg, "role", "")) in {"user", "assistant"}]
    raw_chars = sum(len(str(getattr(msg, "content", "") or "")) for msg in relevant)
    base_facts: dict[str, Any] = {
        "session_id": session_id,
        "message_count": len(relevant),
        "raw_character_count": raw_chars,
        "max_character_count": PLANNER_CONTEXT_MAX_CHARS,
        "recent_message_limit": PLANNER_CONTEXT_RECENT_MESSAGES,
        "message_fetch_limit": PLANNER_CONTEXT_MESSAGE_LIMIT,
        "policy": "planner_conversation_context_v1",
    }
    if not relevant:
        return PlannerConversationContext(text="", facts={**base_facts, "compacted": False})

    raw_text = "\n\n".join(_format_conversation_message(msg) for msg in relevant)
    should_compact = (
        len(relevant) > PLANNER_CONTEXT_RECENT_MESSAGES
        or len(raw_text) > PLANNER_CONTEXT_MAX_CHARS
    )
    if not should_compact:
        return PlannerConversationContext(
            text=raw_text,
            facts={
                **base_facts,
                "compacted": False,
                "retained_message_count": len(relevant),
                "omitted_message_count": 0,
                "truncated_recent_message_count": 0,
            },
        )

    recent = relevant[-PLANNER_CONTEXT_RECENT_MESSAGES:]
    older = relevant[: -PLANNER_CONTEXT_RECENT_MESSAGES]
    preview_items = older[-PLANNER_CONTEXT_OLDER_PREVIEW_MESSAGES:]
    preview_lines = [
        "Conversation context was compacted before planner intake.",
        f"Older messages omitted: {len(older)}.",
        "Older-message provenance preview:",
    ]
    for index, msg in enumerate(preview_items, start=max(1, len(older) - len(preview_items) + 1)):
        preview = _head_preview(str(getattr(msg, "content", "") or ""))
        preview_lines.append(
            f"- older_index={index} role={str(getattr(msg, 'role', '') or '')} "
            f"created_at={float(getattr(msg, 'created_at', 0.0) or 0.0):.3f} "
            f"chars={len(str(getattr(msg, 'content', '') or ''))} preview={preview}"
        )
    recent_lines = ["Recent messages preserved for planner:"]
    truncated_recent = 0
    for msg in recent:
        content = str(getattr(msg, "content", "") or "")
        if len(content) > PLANNER_CONTEXT_RECENT_MESSAGE_MAX_CHARS:
            truncated_recent += 1
            content = truncate_middle(content, PLANNER_CONTEXT_RECENT_MESSAGE_MAX_CHARS)
        recent_lines.append(_format_conversation_message(msg, content=content))
    compacted_text = "\n".join(preview_lines + [""] + recent_lines)
    if len(compacted_text) > PLANNER_CONTEXT_MAX_CHARS:
        compacted_text = truncate_middle(compacted_text, PLANNER_CONTEXT_MAX_CHARS)
    return PlannerConversationContext(
        text=compacted_text,
        facts={
            **base_facts,
            "compacted": True,
            "retained_recent_message_count": len(recent),
            "omitted_message_count": len(older),
            "older_preview_count": len(preview_items),
            "truncated_recent_message_count": truncated_recent,
            "compacted_character_count": len(compacted_text),
        },
    )


def _planner_memory_context(*, memory: Any, spec: LoopSpec) -> PlannerMemoryContext:
    query = spec.goal.objective
    recalls = memory.recall(query, limit=ACTIVE_MEMORY_CONTEXT_LIMIT)
    candidate_ids = tuple(recall.item.id for recall in recalls)
    facts = {
        "policy": "planner_memory_context_v1",
        "query": query,
        "candidate_ids": list(candidate_ids),
        "count": len(candidate_ids),
        "limit": ACTIVE_MEMORY_CONTEXT_LIMIT,
    }
    if not recalls:
        return PlannerMemoryContext(text="", facts=facts, candidate_ids=())
    return PlannerMemoryContext(
        text=_render_planner_memory_context(recalls),
        facts=facts,
        candidate_ids=candidate_ids,
    )


def _render_planner_memory_context(recalls: list[Any]) -> str:
    lines = [
        "Governed memory recall for planner context.",
        "Each record has an id; planner syscall schema includes used_memory_ids.",
    ]
    for recall in recalls:
        item = recall.item
        lines.append(
            f"- [id={item.id} type={item.type} scope={item.scope} "
            f"confidence={item.confidence:.2f} score={recall.score:.4f}] "
            f"{truncate_middle(item.content, PLANNER_MEMORY_ITEM_MAX_CHARS)}"
        )
        if recall.reasons:
            lines.append(f"  reasons: {', '.join(recall.reasons)}")
    return "\n".join(lines)


def _selected_memory_ids(
    requested_ids: tuple[str, ...] | list[str],
    candidate_ids: tuple[str, ...],
) -> tuple[str, ...]:
    candidates = set(candidate_ids)
    selected: list[str] = []
    for item_id in requested_ids:
        item_id = str(item_id).strip()
        if not item_id or item_id not in candidates or item_id in selected:
            continue
        selected.append(item_id)
    return tuple(selected)


def _record_planner_memory_activation(
    *,
    memory: Any,
    state: LoopRunState,
    selected_tool: str,
    used_memory_ids: tuple[str, ...],
) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "policy": "planner_memory_activation_v1",
        "requested_ids": list(used_memory_ids),
        "activated_ids": [],
        "missing_ids": [],
        "activated_count": 0,
        "missing_count": 0,
        "ok": True,
    }
    if not used_memory_ids:
        return facts
    reason = f"planner selected {selected_tool} using recalled memory"
    provenance = f"state_graph:{state.run_id}:attempt:{state.attempt}:planner"
    try:
        for item_id in used_memory_ids:
            item = memory.record_activation(
                item_id,
                reason=reason,
                provenance=provenance,
            )
            if item is None:
                facts["missing_ids"].append(item_id)
            else:
                facts["activated_ids"].append(item_id)
    except ValueError as exc:
        facts["ok"] = False
        facts["error"] = str(exc)
    facts["activated_count"] = len(facts["activated_ids"])
    facts["missing_count"] = len(facts["missing_ids"])
    return facts


def _format_conversation_message(message: Any, *, content: str | None = None) -> str:
    role = str(getattr(message, "role", "") or "").upper()
    created_at = float(getattr(message, "created_at", 0.0) or 0.0)
    body = str(getattr(message, "content", "") if content is None else content)
    return f"{role} [created_at={created_at:.3f}]:\n{body}"


def _head_preview(text: str) -> str:
    clean = " ".join(text.split())
    if len(clean) <= PLANNER_CONTEXT_OLDER_PREVIEW_CHARS:
        return clean
    omitted = len(clean) - PLANNER_CONTEXT_OLDER_PREVIEW_CHARS
    return f"{clean[:PLANNER_CONTEXT_OLDER_PREVIEW_CHARS]} ... [truncated tail {omitted} chars]"


class ModelCapabilityPlannerPort:
    """Planner node port for the durable StateGraph.

    It reuses the existing model syscall planner but constrains the exposed
    tool manifest to the LoopSpec's allowed capabilities.
    """

    def __init__(self, *, runtime: AgentRuntime, capabilities: CapabilityRegistry):
        self.runtime = runtime
        self.capabilities = capabilities
        self.planner = ModelSyscallPlanner(runtime.provider)

    async def plan(
        self,
        spec: LoopSpec,
        state: LoopRunState,
        *,
        workspace: Path,
        evidence: dict[str, Any],
    ) -> PlannedCapabilityStep:
        allowed = set(spec.allowed_capabilities)
        context = CapabilityContext(
            home=self.capabilities.home,
            source="state_graph",
            peer_id="state_graph",
            sender_id=spec.goal.owner or "state_graph",
            permission_ceiling=spec.goal.permission_ceiling,
            workspace=str(workspace),
        )
        tools = [
            item
            for item in self.capabilities.planner_specs(
                permission_ceiling=spec.goal.permission_ceiling,
                context=context,
            )
            if "*" in allowed or item.name in allowed
        ]
        if not tools:
            return PlannedCapabilityStep(
                tool="system.planner_error",
                permission="read",
                args={"reason": "no_allowed_capabilities", "allowed_capabilities": sorted(allowed)},
                model_role="planner",
                reason="LoopSpec allowed no planner-visible capabilities",
            )
        session_id = spec.goal.metadata.get("session_id") or ""
        conversation_context = ""
        conversation_facts: dict[str, Any] = {}
        if session_id:
            context = _planner_conversation_context(
                session_id=session_id,
                messages=self.runtime.memory.get_messages(
                    session_id,
                    limit=PLANNER_CONTEXT_MESSAGE_LIMIT,
                ),
            )
            conversation_context = context.text
            conversation_facts = context.facts
        memory_context = _planner_memory_context(memory=self.runtime.memory, spec=spec)

        syscalls = await self.planner.plan(
            spec.goal.objective,
            tools=tools,
            conversation_context=conversation_context,
            runtime_facts={
                "loop_spec": spec.to_dict(),
                "loop_run_state": state.to_dict(),
                "objective_evidence": dict(evidence),
                "attempt_history": list(evidence.get("attempt_history") or []),
                "conversation_compaction": conversation_facts,
                "memory_context": memory_context.facts,
            },
            permission_ceiling=spec.goal.permission_ceiling,
            model_roles=self.runtime.model_roles(),
            durable_constraints=_goal_constraints(spec),
            memory_context=memory_context.text,
        )
        selected = syscalls[0]
        used_memory_ids = _selected_memory_ids(
            selected.used_memory_ids,
            memory_context.candidate_ids,
        )
        memory_activation = _record_planner_memory_activation(
            memory=self.runtime.memory,
            state=state,
            selected_tool=selected.tool,
            used_memory_ids=used_memory_ids,
        )
        return PlannedCapabilityStep(
            tool=selected.tool,
            args=dict(selected.args),
            permission=selected.permission,
            model_role=selected.model_role,
            reason=selected.reason,
            used_memory_ids=used_memory_ids,
            memory_activation=memory_activation,
        )


class CapabilityExecutorPort:
    """Executor node port that invokes the capability registry in the node workspace."""

    def __init__(self, *, home: Path, context: CapabilityContext):
        self.home = home
        self.context = context

    async def execute(
        self,
        step: PlannedCapabilityStep,
        spec: LoopSpec,
        state: LoopRunState,
        *,
        workspace: Path,
    ) -> ExecutedCapabilityStep:
        registry = CapabilityRegistry(
            home=self.home,
            project_dir=workspace,
            permission_ceiling=spec.goal.permission_ceiling,
            enforce_connector_source_policy=False,
            governed_run_id=state.run_id,
            sensitive_approval_mode="skip",
        )
        context = replace(
            self.context,
            workspace=str(workspace),
            permission_ceiling=spec.goal.permission_ceiling,
            source=self.context.source or "state_graph",
        )
        result = await registry.invoke(
            step.tool,
            step.args,
            permission=step.permission,
            context=context,
        )
        return ExecutedCapabilityStep(
            ok=result.ok,
            action=result.action,
            facts=result.facts or {},
            message=result.message,
            error_reason=result.error_reason,
            terminal=result.terminal,
        )


class DurableStateGraphRunner:
    """Durable PLAN -> EXECUTE -> EVALUATE runner for the Navi 2.0 control plane."""

    def __init__(
        self,
        *,
        home: Path,
        gateway: GlobalResourceGateway | None = None,
        harness: Harness | None = None,
        checker: DeterministicChecker | None = None,
        planner_port: ModelCapabilityPlannerPort | None = None,
        executor_port: CapabilityExecutorPort | None = None,
        reflector_port: RecoveryReflectorPort | None = None,
        recovery_port: CapabilityRecoveryPort | None = None,
        llm_reflector_port: LLMReflectorPort | None = None,
        semantic_checker_port: LLMSemanticCheckerPort | None = None,
        side_effect_saga_port: SideEffectSagaPort | None = None,
        trace_store: TraceStore | None = None,
        trace_context: CapabilityContext | None = None,
    ):
        self.home = home
        self.store = LoopRunStore(home)
        self.gateway = gateway
        self.harness = harness or Harness(home=home)
        self.checker = checker or DeterministicChecker()
        self.planner_port = planner_port
        self.executor_port = executor_port
        self.reflector_port = reflector_port or RecoveryReflectorPort()
        self.recovery_port = recovery_port or CapabilityRecoveryPort()
        self.llm_reflector_port = llm_reflector_port
        self.semantic_checker_port = semantic_checker_port
        self.side_effect_saga_port = side_effect_saga_port or SideEffectSagaPort(home=home)
        self.trace_store = trace_store
        self.trace_context = trace_context

    def run(
        self,
        spec: LoopSpec,
        *,
        workspace: Path,
        run_id: str = "",
        evidence: dict[str, Any] | None = None,
    ) -> StateGraphRunResult:
        raise RuntimeError(
            "DurableStateGraphRunner.run() is disabled. "
            "Use run_async() with explicit planner_port and executor_port."
        )

    async def run_async(
        self,
        spec: LoopSpec,
        *,
        workspace: Path,
        run_id: str = "",
        evidence: dict[str, Any] | None = None,
    ) -> StateGraphRunResult:
        if self.planner_port is None or self.executor_port is None:
            raise RuntimeError(
                "Durable StateGraph requires explicit planner_port and executor_port."
            )
        spec.validate()
        if self.gateway is None:
            self.gateway = GlobalResourceGateway(_resource_limits_for_spec(spec))
        state = self.store.get_run(run_id) if run_id else None
        if state is None:
            state = self.store.create_run(spec)
        if state.is_terminal():
            return StateGraphRunResult(run_state=state, evidence=evidence or {})

        collected_evidence: dict[str, Any] = dict(evidence or {})
        attempt_history: list[dict[str, Any]] = list(
            collected_evidence.get("attempt_history") or []
        )
        grants: list[ResourceGrant] = []
        harness_results: list[HarnessResult] = []
        checker_report: CheckerReport | None = None
        planned_step: PlannedCapabilityStep | None = None
        execution_workspace = workspace
        shadow_workspace = None
        if str(spec.workspace_policy.mode) == str(WorkspaceMode.SHADOW):
            shadow_workspace = self.harness.create_shadow_workspace(
                run_id=state.run_id,
                workspace=workspace,
            )
            execution_workspace = Path(shadow_workspace.shadow_workspace)
            collected_evidence["shadow_workspace"] = shadow_workspace.to_dict()

        if state.node == LoopNode.PLAN:
            state, stopped, grant = self._gate_or_stop(state, kind="state_graph.plan")
            grants.append(grant)
            if stopped:
                return StateGraphRunResult(
                    run_state=state,
                    resource_grants=tuple(grants),
                    evidence=collected_evidence,
                )
            if self.planner_port is not None:
                self.store.write_checkpoint(
                    state.run_id,
                    node=LoopNode.PLAN,
                    inputs={"planner_node": "start"},
                    state=state.to_dict(),
                )
                if self._has_trace_context():
                    self.trace_store.add_event(
                        trace_id=self.trace_context.trace_id,
                        session_id=self.trace_context.session_id or "",
                        run_id=state.run_id,
                        phase=str(TracePhase.PLANNER_CALL_START),
                        source=self.trace_context.source,
                        peer_id=self.trace_context.peer_id,
                        sender_id=self.trace_context.sender_id,
                        input_data={"objective": spec.goal.objective, "attempt": state.attempt},
                    )

                planned_step = await self.planner_port.plan(
                    spec,
                    state,
                    workspace=execution_workspace,
                    evidence=collected_evidence,
                )

                if self._has_trace_context():
                    phase = str(TracePhase.PLANNER_SYSCALL)
                    if planned_step.tool == "system.planner_error":
                        phase = str(TracePhase.PLANNER_CALL_ERROR)

                    output_data = planned_step.to_dict()
                    usage_data = self._usage_for_role("planner")
                    prompt_messages = usage_data.pop("messages", [])
                    llm_response = usage_data.pop("response", "")
                    output_data["usage"] = usage_data
                    output_data["llm_response"] = llm_response

                    self.trace_store.add_event(
                        trace_id=self.trace_context.trace_id,
                        session_id=self.trace_context.session_id or "",
                        run_id=state.run_id,
                        phase=phase,
                        source=self.trace_context.source,
                        peer_id=self.trace_context.peer_id,
                        sender_id=self.trace_context.sender_id,
                        tool=planned_step.tool,
                        model_role="planner",
                        ok=(planned_step.tool != "system.planner_error"),
                        input_data={"prompt": prompt_messages},
                        output_data=output_data,
                    )

                collected_evidence["planned_capability"] = planned_step.to_dict()
                if planned_step.tool == "system.planner_error":
                    self._discard_shadow_if_needed(spec, state.run_id, shadow_workspace)
                    state = self._transition(
                        state,
                        node=LoopNode.PLAN,
                        condition="planner_failed",
                        terminal_state=LoopTerminalState.FAILED,
                        evidence=planned_step.to_dict(),
                    )
                    self.gateway.release()
                    return StateGraphRunResult(
                        run_state=state,
                        resource_grants=tuple(grants),
                        evidence=collected_evidence,
                    )
            state = self._transition(
                state,
                node=LoopNode.EXECUTE,
                condition="plan_ready",
                evidence={"planned_capability": planned_step.to_dict()},
            )
            self.gateway.release()

        if state.node == LoopNode.EXECUTE:
            state, stopped, grant = self._gate_or_stop(state, kind="state_graph.execute")
            grants.append(grant)
            if stopped:
                return StateGraphRunResult(
                    run_state=state,
                    resource_grants=tuple(grants),
                    evidence=collected_evidence,
                )
            if planned_step is None:
                self._discard_shadow_if_needed(spec, state.run_id, shadow_workspace)
                state = self._transition(
                    state,
                    node=LoopNode.REFLECT,
                    condition="missing_planned_capability",
                    evidence={"reason": "execute_without_planned_capability"},
                )
                state = self._transition(
                    state,
                    node=LoopNode.REFLECT,
                    condition="checker_rejected",
                    terminal_state=LoopTerminalState.FAILED,
                    evidence={"reason": "state_graph_resume_missing_plan"},
                )
                self.gateway.release()
                return StateGraphRunResult(
                    run_state=state,
                    resource_grants=tuple(grants),
                    evidence=collected_evidence,
                )
            self.store.write_checkpoint(
                state.run_id,
                node=LoopNode.EXECUTE,
                inputs={"planned_capability": planned_step.to_dict()},
                state=state.to_dict(),
            )
            executed = await self.executor_port.execute(
                planned_step,
                spec,
                state,
                workspace=execution_workspace,
            )
            collected_evidence["capability_result"] = executed.to_dict()
            attempt_history.append(
                {
                    "attempt": state.attempt,
                    "tool": planned_step.tool,
                    "args": dict(planned_step.args),
                    "ok": executed.ok,
                    "action": executed.action,
                    "facts": dict(executed.facts),
                    "message": executed.message,
                    "error_reason": executed.error_reason,
                    "terminal": executed.terminal,
                }
            )
            collected_evidence["attempt_history"] = attempt_history
            if not executed.ok:
                self._discard_shadow_if_needed(spec, state.run_id, shadow_workspace)
                state = self._transition(
                    state,
                    node=LoopNode.REFLECT,
                    condition="capability_failed",
                    evidence=executed.to_dict(),
                )
                decision = self.recovery_port.recover(spec, state, executed=executed)
                collected_evidence["reflection"] = decision.to_dict()
                if decision.retry:
                    state = self._transition(
                        state,
                        node=LoopNode.PLAN,
                        condition="new_route_available",
                        evidence=decision.to_dict(),
                    )
                    self.gateway.release()
                    return await self.run_async(
                        spec,
                        workspace=workspace,
                        run_id=state.run_id,
                        evidence=collected_evidence,
                    )
                else:
                    state = self._transition(
                        state,
                        node=LoopNode.REFLECT,
                        condition="no_route_available",
                        terminal_state=LoopTerminalState.BLOCKED,
                        evidence=decision.to_dict(),
                    )
                    self.gateway.release()
                    return StateGraphRunResult(
                        run_state=state,
                        resource_grants=tuple(grants),
                        evidence=collected_evidence,
                    )
            state = self._transition(
                state,
                node=LoopNode.EVALUATE,
                condition="side_effect_recorded",
                evidence={"executor": collected_evidence["capability_result"]},
            )
            self.gateway.release()

        if state.node == LoopNode.EVALUATE:
            result = await self._evaluate_state(
                spec,
                state,
                workspace=workspace,
                execution_workspace=execution_workspace,
                shadow_workspace=shadow_workspace,
                collected_evidence=collected_evidence,
                grants=grants,
                harness_results=harness_results,
                checker_report=checker_report,
            )
            if result.run_state.node == LoopNode.PLAN and not result.run_state.is_terminal():
                return await self.run_async(
                    spec,
                    workspace=workspace,
                    run_id=result.run_state.run_id,
                    evidence=result.evidence,
                )
            return result

        return StateGraphRunResult(
            run_state=state,
            checker_report=checker_report,
            resource_grants=tuple(grants),
            harness_results=tuple(harness_results),
            evidence=collected_evidence,
        )

    def _discard_shadow_if_needed(
        self,
        spec: LoopSpec,
        run_id: str,
        shadow_workspace: object | None,
    ) -> None:
        if shadow_workspace is not None and spec.rollback_policy.discard_shadow_on_failure:
            self.harness.discard_shadow_run(run_id)

    async def _evaluate_state(
        self,
        spec: LoopSpec,
        state: LoopRunState,
        *,
        workspace: Path,
        execution_workspace: Path,
        shadow_workspace: object | None,
        collected_evidence: dict[str, Any],
        grants: list[ResourceGrant],
        harness_results: list[HarnessResult],
        checker_report: CheckerReport | None,
    ) -> StateGraphRunResult:
        state, stopped, grant = self._gate_or_stop(state, kind="state_graph.evaluate")
        grants.append(grant)
        if stopped:
            return StateGraphRunResult(
                run_state=state,
                resource_grants=tuple(grants),
                evidence=collected_evidence,
            )
        self.gateway.release()
        for step in spec.verification_ladder:
            if not _step_runs_command(step):
                continue
            state, stopped, command_grant = self._gate_or_stop(
                state,
                kind="harness.command",
                request=ResourceRequest(kind="harness.command", units=1),
            )
            grants.append(command_grant)
            if stopped:
                return StateGraphRunResult(
                    run_state=state,
                    resource_grants=tuple(grants),
                    evidence=collected_evidence,
                )
            self.store.write_checkpoint(
                state.run_id,
                node=LoopNode.EVALUATE,
                inputs={"verification_step": step.to_dict()},
                state=state.to_dict(),
            )
            lock_result: LockAcquireResult | None = None
            lock_resource = _workspace_lock_resource(workspace)
            if spec.workspace_policy.require_locks:
                lock_result = self.harness.acquire_workspace_lock(
                    owner_run_id=state.run_id,
                    resource=lock_resource,
                    mode=LockMode.READ,
                )
                collected_evidence["workspace_lock"] = lock_result.to_dict()
                if not lock_result.acquired:
                    state = self._pause_for_lock_conflict(state, lock_result)
                    self.gateway.release()
                    return StateGraphRunResult(
                        run_state=state,
                        resource_grants=tuple(grants),
                        harness_results=tuple(harness_results),
                        evidence=collected_evidence,
                    )
            try:
                result = self.harness.run_command(
                    HarnessCommand(
                        command=tuple(shlex.split(step.command)),
                        cwd=execution_workspace,
                        timeout=step.timeout,
                    )
                )
            finally:
                if lock_result is not None and lock_result.acquired:
                    self.harness.release_workspace_locks(
                        owner_run_id=state.run_id,
                        resource=lock_resource,
                    )
                self.gateway.release()
            harness_results.append(result)
            collected_evidence[step.evidence_key or step.name] = result.to_facts()

        await self._populate_semantic_checker_evidence(
            spec,
            state,
            collected_evidence=collected_evidence,
        )
        checker_report = self.checker.evaluate(spec, collected_evidence)
        if checker_report.accepted:
            has_required_steps = any(step.required for step in spec.verification_ladder)
            cap_result = collected_evidence.get("capability_result", {})
            is_terminal = isinstance(cap_result, dict) and cap_result.get("terminal", False)
            # Agentic reflection: for goals without required verification steps,
            # ask the LLM whether the objective is achieved. The LLM decides
            # whether to converge, retry with a new plan, or block with a user
            # message. This is the "never give up" loop — the LLM keeps trying
            # different capabilities (search filesystem, ask user, send file)
            # until the objective is concretely satisfied or the attempt budget
            # is exhausted.
            if not has_required_steps and not is_terminal and self.llm_reflector_port is not None:
                executed_step = ExecutedCapabilityStep(
                    ok=cap_result.get("ok", False) if isinstance(cap_result, dict) else False,
                    action=cap_result.get("action", "") if isinstance(cap_result, dict) else "",
                    facts=cap_result.get("facts", {}) if isinstance(cap_result, dict) else {},
                    message=cap_result.get("message", "") if isinstance(cap_result, dict) else "",
                    error_reason=cap_result.get("error_reason", "") if isinstance(cap_result, dict) else "",
                    terminal=bool(cap_result.get("terminal", False)) if isinstance(cap_result, dict) else False,
                )
                if self._has_trace_context():
                    self.trace_store.add_event(
                        trace_id=self.trace_context.trace_id,
                        session_id=self.trace_context.session_id or "",
                        run_id=state.run_id,
                        phase=str(TracePhase.PLANNER_CALL_START),
                        source=self.trace_context.source,
                        peer_id=self.trace_context.peer_id,
                        sender_id=self.trace_context.sender_id,
                        input_data={"objective": spec.goal.objective, "attempt": state.attempt},
                    )

                decision = await self.llm_reflector_port.assess(
                    spec, state, executed=executed_step, evidence=collected_evidence
                )

                if self._has_trace_context():
                    output_data = decision.to_dict()
                    usage_data = self._usage_for_role("reflector")
                    prompt_messages = usage_data.pop("messages", [])
                    llm_response = usage_data.pop("response", "")
                    output_data["usage"] = usage_data
                    output_data["llm_response"] = llm_response
                    self.trace_store.add_event(
                        trace_id=self.trace_context.trace_id,
                        session_id=self.trace_context.session_id or "",
                        run_id=state.run_id,
                        phase=str(TracePhase.PLANNER_SYSCALL),
                        source=self.trace_context.source,
                        peer_id=self.trace_context.peer_id,
                        sender_id=self.trace_context.sender_id,
                        tool="reflection",
                        model_role="reflector",
                        input_data={"prompt": prompt_messages},
                        output_data=output_data,
                    )

                collected_evidence["reflection"] = decision.to_dict()
                if decision.retry:
                    state = self._transition(
                        state,
                        node=LoopNode.PLAN,
                        condition="continue_iteration",
                        evidence={"reason": "non_terminal_capability", "attempt": state.attempt},
                    )
                    return StateGraphRunResult(
                        run_state=state,
                        checker_report=checker_report,
                        resource_grants=tuple(grants),
                        harness_results=tuple(harness_results),
                        evidence=collected_evidence,
                    )
                # Goal achieved or no more routes — converge or block.
                if decision.facts.get("goal_achieved"):
                    # Merge the shadow workspace into the real workspace before
                    # converging, so the capability's side effects land on disk.
                    if shadow_workspace is not None:
                        merge_lock = self.harness.acquire_workspace_lock(
                            owner_run_id=state.run_id,
                            resource=_workspace_lock_resource(workspace),
                            mode=LockMode.WRITE,
                        )
                        try:
                            self.harness.merge_shadow_run(state.run_id)
                        finally:
                            if merge_lock.acquired:
                                self.harness.release_workspace_locks(
                                    owner_run_id=state.run_id,
                                    resource=_workspace_lock_resource(workspace),
                                )
                    commit_result = self._commit_side_effects(state, collected_evidence)
                    if commit_result is not None and not commit_result.ok:
                        state = self._side_effect_commit_required(state, commit_result)
                        return StateGraphRunResult(
                            run_state=state,
                            checker_report=checker_report,
                            resource_grants=tuple(grants),
                            harness_results=tuple(harness_results),
                            evidence=collected_evidence,
                        )
                    state = self._transition(
                        state,
                        node=LoopNode.EVALUATE,
                        condition="checker_passed",
                        terminal_state=LoopTerminalState.CONVERGED,
                        evidence=decision.to_dict(),
                    )
                else:
                    state = self._transition(
                        state,
                        node=LoopNode.EVALUATE,
                        condition="no_route_available",
                        terminal_state=LoopTerminalState.BLOCKED,
                        evidence=decision.to_dict(),
                    )
                return StateGraphRunResult(
                    run_state=state,
                    checker_report=checker_report,
                    resource_grants=tuple(grants),
                    harness_results=tuple(harness_results),
                    evidence=collected_evidence,
                )
            if not has_required_steps and not is_terminal and state.attempt < spec.retry_policy.max_attempts:
                state = self._transition(
                    state,
                    node=LoopNode.PLAN,
                    condition="continue_iteration",
                    evidence={"reason": "non_terminal_capability", "attempt": state.attempt},
                )
                return StateGraphRunResult(
                    run_state=state,
                    checker_report=checker_report,
                    resource_grants=tuple(grants),
                    harness_results=tuple(harness_results),
                    evidence=collected_evidence,
                )
            if shadow_workspace is not None:
                merge_lock: LockAcquireResult | None = None
                lock_resource = _workspace_lock_resource(workspace)
                if spec.workspace_policy.require_locks:
                    merge_lock = self.harness.acquire_workspace_lock(
                        owner_run_id=state.run_id,
                        resource=lock_resource,
                        mode=LockMode.WRITE,
                    )
                    collected_evidence["merge_workspace_lock"] = merge_lock.to_dict()
                    if not merge_lock.acquired:
                        return StateGraphRunResult(
                            run_state=self._pause_for_lock_conflict(state, merge_lock),
                            checker_report=checker_report,
                            resource_grants=tuple(grants),
                            harness_results=tuple(harness_results),
                            evidence=collected_evidence,
                        )
                try:
                    merge_result = self.harness.merge_shadow_run(state.run_id)
                    collected_evidence["merge_result"] = merge_result.to_dict()
                finally:
                    if merge_lock is not None and merge_lock.acquired:
                        self.harness.release_workspace_locks(
                            owner_run_id=state.run_id,
                            resource=lock_resource,
                    )
                if merge_result.status == MergeStatus.CONFLICTED:
                    self._compensate_side_effects(state, collected_evidence)
                    state = self._transition(
                        state,
                        node=LoopNode.EVALUATE,
                        condition="merge_conflict",
                        terminal_state=LoopTerminalState.CONFLICTED,
                        evidence=merge_result.to_dict(),
                    )
                    return StateGraphRunResult(
                        run_state=state,
                        checker_report=checker_report,
                        resource_grants=tuple(grants),
                        harness_results=tuple(harness_results),
                        evidence=collected_evidence,
                    )
            commit_result = self._commit_side_effects(state, collected_evidence)
            if commit_result is not None and not commit_result.ok:
                state = self._side_effect_commit_required(state, commit_result)
                return StateGraphRunResult(
                    run_state=state,
                    checker_report=checker_report,
                    resource_grants=tuple(grants),
                    harness_results=tuple(harness_results),
                    evidence=collected_evidence,
                )
            state = self._transition(
                state,
                node=LoopNode.EVALUATE,
                condition="checker_passed",
                terminal_state=LoopTerminalState.CONVERGED,
                evidence=checker_report.to_dict(),
            )
        elif checker_report.timed_out:
            self._compensate_side_effects(state, collected_evidence)
            self._discard_shadow_if_needed(spec, state.run_id, shadow_workspace)
            state = self._transition(
                state,
                node=LoopNode.EVALUATE,
                condition="hard_timeout",
                terminal_state=LoopTerminalState.TIMED_OUT,
                evidence=checker_report.to_dict(),
            )
        elif checker_report.blocked:
            state = self._reflect_state(
                spec,
                state,
                checker_report=checker_report,
                shadow_workspace=shadow_workspace,
                collected_evidence=collected_evidence,
                harness_results=harness_results,
            )
        else:
            state = self._reflect_state(
                spec,
                state,
                checker_report=checker_report,
                shadow_workspace=shadow_workspace,
                collected_evidence=collected_evidence,
                harness_results=harness_results,
            )

        return StateGraphRunResult(
            run_state=state,
            checker_report=checker_report,
            resource_grants=tuple(grants),
            harness_results=tuple(harness_results),
            evidence=collected_evidence,
        )

    def _commit_side_effects(
        self,
        state: LoopRunState,
        collected_evidence: dict[str, Any],
    ) -> SideEffectSagaResult | None:
        side_effect = _side_effect_from_collected_evidence(collected_evidence)
        if not side_effect:
            return None
        result = self.side_effect_saga_port.commit(side_effect)
        collected_evidence["side_effect_commit_result"] = result.to_dict()
        self._record_side_effect_decision(state=state, result=result)
        return result

    def _compensate_side_effects(
        self,
        state: LoopRunState,
        collected_evidence: dict[str, Any],
    ) -> SideEffectSagaResult | None:
        side_effect = _side_effect_from_collected_evidence(collected_evidence)
        if not side_effect:
            return None
        result = self.side_effect_saga_port.compensate(side_effect)
        results = list(collected_evidence.get("side_effect_compensation_results") or [])
        results.append(result.to_dict())
        collected_evidence["side_effect_compensation_results"] = results
        self._record_side_effect_decision(state=state, result=result)
        return result

    def _side_effect_commit_required(
        self,
        state: LoopRunState,
        result: SideEffectSagaResult,
    ) -> LoopRunState:
        escalated = self._transition(
            state,
            node=LoopNode.ESCALATE,
            condition="side_effect_commit_required",
            evidence=result.to_dict(),
        )
        return self._transition(
            escalated,
            node=LoopNode.ESCALATE,
            condition="approval_required",
            terminal_state=LoopTerminalState.WAITING_APPROVAL,
            evidence=result.to_dict(),
        )

    def _record_side_effect_decision(
        self,
        *,
        state: LoopRunState,
        result: SideEffectSagaResult,
    ) -> None:
        if self.trace_store is None or self.trace_context is None:
            return
        if not self.trace_context.trace_id:
            return
        check = LoopCheckResult(
            name=LoopCheckName.CAPABILITY_RESULT,
            passed=result.ok,
            reason=result.reason,
            evidence={"side_effect": result.to_dict()},
        )
        self.trace_store.add_loop_decision(
            trace_id=self.trace_context.trace_id,
            session_id=self.trace_context.session_id or "",
            run_id=state.run_id,
            source=self.trace_context.source,
            peer_id=self.trace_context.peer_id,
            sender_id=self.trace_context.sender_id,
            decision=LoopDecision(
                decision=LoopDecisionKind.CONTINUE if result.ok else LoopDecisionKind.BLOCKED,
                reason=LoopReason.CAPABILITY_FACT_RECORDED
                if result.ok
                else LoopReason.APPROVAL_REQUIRED,
                phase=LoopPhase.DECISION,
                failure_domain=TraceFailureDomain.NONE
                if result.ok
                else TraceFailureDomain.SAFEGUARD_POLICY,
                tool=f"state_graph.side_effect.{result.action}",
                run_id=state.run_id,
                goal_ids=(state.goal_id,),
                checker_results=(check,),
                evidence={
                    "condition": f"side_effect_{result.action}",
                    "loop_run_id": state.run_id,
                    "loop_spec_id": state.loop_spec_id,
                    "attempt": state.attempt,
                    "side_effect": result.to_dict(),
                },
            ),
        )

    def _reflect_state(
        self,
        spec: LoopSpec,
        state: LoopRunState,
        *,
        checker_report: CheckerReport,
        shadow_workspace: object | None,
        collected_evidence: dict[str, Any],
        harness_results: list[HarnessResult],
    ) -> LoopRunState:
        self._compensate_side_effects(state, collected_evidence)
        self._discard_shadow_if_needed(spec, state.run_id, shadow_workspace)
        reflected = self._transition(
            state,
            node=LoopNode.REFLECT,
            condition="checker_failed",
            evidence=checker_report.to_dict(),
        )
        decision = self.reflector_port.reflect(
            spec,
            reflected,
            checker_report=checker_report,
            evidence=collected_evidence,
            harness_results=tuple(harness_results),
        )
        collected_evidence["reflection"] = decision.to_dict()
        if decision.retry:
            return self._transition(
                reflected,
                node=LoopNode.PLAN,
                condition="new_route_available",
                evidence=decision.to_dict(),
            )
        terminal = LoopTerminalState.BLOCKED if checker_report.blocked else LoopTerminalState.FAILED
        condition = "no_route_available" if checker_report.blocked else "checker_rejected"
        return self._transition(
            reflected,
            node=LoopNode.REFLECT,
            condition=condition,
            terminal_state=terminal,
            evidence=decision.to_dict(),
        )

    async def _populate_semantic_checker_evidence(
        self,
        spec: LoopSpec,
        state: LoopRunState,
        *,
        collected_evidence: dict[str, Any],
    ) -> None:
        if self.semantic_checker_port is None:
            return
        cap_result = collected_evidence.get("capability_result")
        if not isinstance(cap_result, dict):
            return
        executed_step = ExecutedCapabilityStep(
            ok=cap_result.get("ok", False),
            action=str(cap_result.get("action") or ""),
            facts=cap_result.get("facts", {}) if isinstance(cap_result.get("facts"), dict) else {},
            message=str(cap_result.get("message") or ""),
            error_reason=str(cap_result.get("error_reason") or ""),
            terminal=bool(cap_result.get("terminal", False)),
        )
        for step in spec.verification_ladder:
            if step.kind != VerificationKind.LLM_CHECKER:
                continue
            key = step.evidence_key or step.name
            if self._has_trace_context():
                self.trace_store.add_event(
                    trace_id=self.trace_context.trace_id,
                    session_id=self.trace_context.session_id or "",
                    run_id=state.run_id,
                    phase=str(TracePhase.PLANNER_CALL_START),
                    source=self.trace_context.source,
                    peer_id=self.trace_context.peer_id,
                    sender_id=self.trace_context.sender_id,
                    input_data={"objective": spec.goal.objective, "attempt": state.attempt},
                )

            decision = await self.semantic_checker_port.assess(
                spec,
                state,
                executed=executed_step,
            )

            if self._has_trace_context():
                output_data = decision.to_dict()
                usage_data = self._usage_for_role("checker")
                prompt_messages = usage_data.pop("messages", [])
                llm_response = usage_data.pop("response", "")
                output_data["usage"] = usage_data
                output_data["llm_response"] = llm_response
                self.trace_store.add_event(
                    trace_id=self.trace_context.trace_id,
                    session_id=self.trace_context.session_id or "",
                    run_id=state.run_id,
                    phase=str(TracePhase.PLANNER_SYSCALL),
                    source=self.trace_context.source,
                    peer_id=self.trace_context.peer_id,
                    sender_id=self.trace_context.sender_id,
                    tool="checker",
                    model_role="checker",
                    input_data={"prompt": prompt_messages},
                    output_data=output_data,
                )

            facts = {**decision.to_facts(), "attempt": state.attempt}
            collected_evidence[key] = facts
            collected_evidence["semantic_checker_result"] = facts

    def _usage_for_role(self, role: str) -> dict[str, Any]:
        for port in (
            self.planner_port,
            self.llm_reflector_port,
            self.semantic_checker_port,
        ):
            runtime = getattr(port, "runtime", None)
            provider = getattr(runtime, "provider", None)
            usage_for = getattr(provider, "usage_for", None)
            if not callable(usage_for):
                continue
            usage = usage_for(role)
            if usage:
                return dict(usage)
        return {}

    def _has_trace_context(self) -> bool:
        return (
            self.trace_store is not None
            and self.trace_context is not None
            and bool(self.trace_context.trace_id)
        )

    def _gate_or_stop(
        self,
        state: LoopRunState,
        *,
        kind: str,
        request: ResourceRequest | None = None,
    ) -> tuple[LoopRunState, bool, ResourceGrant]:
        if self.gateway is None:
            raise RuntimeError("StateGraph resource gateway is not initialized")
        grant = self.gateway.request(request or ResourceRequest(kind=kind, units=1))
        self._record_gate_decision(state=state, kind=kind, grant=grant)
        if grant.allowed:
            return state, False, grant
        return self._stop_for_resource_grant(state, grant), True, grant

    def _record_gate_decision(
        self,
        *,
        state: LoopRunState,
        kind: str,
        grant: ResourceGrant,
    ) -> None:
        if self.trace_store is None or self.trace_context is None:
            return
        if not self.trace_context.trace_id:
            return
        self.trace_store.add_loop_decision(
            trace_id=self.trace_context.trace_id,
            session_id=self.trace_context.session_id or "",
            run_id=state.run_id,
            source=self.trace_context.source,
            peer_id=self.trace_context.peer_id,
            sender_id=self.trace_context.sender_id,
            decision=_gate_loop_decision(state=state, kind=kind, grant=grant),
        )

    def _stop_for_resource_grant(self, state: LoopRunState, grant: ResourceGrant) -> LoopRunState:
        evidence = {"resource_grant": grant.to_dict()}
        if grant.decision == ResourceDecision.PAUSE:
            paused = self._transition(
                state,
                node=LoopNode.PAUSE,
                condition="resource_pause",
                evidence=evidence,
            )
            return self._transition(
                paused,
                node=LoopNode.PAUSE,
                condition="resource_or_user_pause",
                terminal_state=LoopTerminalState.PAUSED,
                evidence=evidence,
            )
        if grant.decision == ResourceDecision.ESCALATE:
            escalated = self._transition(
                state,
                node=LoopNode.ESCALATE,
                condition="resource_escalate",
                evidence=evidence,
            )
            return self._transition(
                escalated,
                node=LoopNode.ESCALATE,
                condition="resource_escalate",
                terminal_state=LoopTerminalState.WAITING_APPROVAL,
                evidence=evidence,
            )
        return self._transition(
            state,
            node=state.node,
            condition="resource_blocked",
            terminal_state=LoopTerminalState.BLOCKED,
            evidence=evidence,
        )

    def _pause_for_lock_conflict(
        self,
        state: LoopRunState,
        lock_result: LockAcquireResult,
    ) -> LoopRunState:
        evidence = {"workspace_lock": lock_result.to_dict(), "reason": "workspace_lock_conflict"}
        paused = self._transition(
            state,
            node=LoopNode.PAUSE,
            condition="resource_pause",
            evidence=evidence,
        )
        return self._transition(
            paused,
            node=LoopNode.PAUSE,
            condition="resource_or_user_pause",
            terminal_state=LoopTerminalState.PAUSED,
            evidence=evidence,
        )

    def _transition(
        self,
        state: LoopRunState,
        *,
        node: LoopNode | str,
        condition: str,
        terminal_state: LoopTerminalState | str = "",
        evidence: dict[str, Any] | None = None,
    ) -> LoopRunState:
        checkpoint = self._checkpoint(
            state,
            inputs={
                "target_node": str(node),
                "condition": condition,
                "terminal_state": str(terminal_state),
            },
        )
        transitioned = self.store.transition(
            state.run_id,
            node=node,
            checkpoint_id=checkpoint.id,
            terminal_state=terminal_state,
            condition=condition,
            evidence=evidence,
        )
        self._record_transition_decision(
            from_state=state,
            to_state=transitioned,
            checkpoint_id=checkpoint.id,
            condition=condition,
            terminal_state=terminal_state,
            evidence=evidence or {},
        )
        return transitioned

    def _record_transition_decision(
        self,
        *,
        from_state: LoopRunState,
        to_state: LoopRunState,
        checkpoint_id: str,
        condition: str,
        terminal_state: LoopTerminalState | str,
        evidence: dict[str, Any],
    ) -> None:
        if self.trace_store is None or self.trace_context is None:
            return
        if not self.trace_context.trace_id:
            return
        self.trace_store.add_loop_decision(
            trace_id=self.trace_context.trace_id,
            session_id=self.trace_context.session_id or "",
            run_id=to_state.run_id,
            source=self.trace_context.source,
            peer_id=self.trace_context.peer_id,
            sender_id=self.trace_context.sender_id,
            decision=_transition_loop_decision(
                from_state=from_state,
                to_state=to_state,
                checkpoint_id=checkpoint_id,
                condition=condition,
                terminal_state=terminal_state,
                evidence=evidence,
            ),
        )

    def _checkpoint(self, state: LoopRunState, *, inputs: dict[str, Any]) -> LoopCheckpoint:
        return self.store.write_checkpoint(
            state.run_id,
            node=state.node,
            inputs=inputs,
            state=state.to_dict(),
        )


def _transition_loop_decision(
    *,
    from_state: LoopRunState,
    to_state: LoopRunState,
    checkpoint_id: str,
    condition: str,
    terminal_state: LoopTerminalState | str,
    evidence: dict[str, Any],
) -> LoopDecision:
    decision = _transition_decision_kind(to_state, condition, terminal_state)
    reason = _transition_reason(condition, terminal_state)
    failure_domain = _transition_failure_domain(condition, terminal_state)
    check = LoopCheckResult(
        name=_transition_check_name(condition, terminal_state),
        passed=str(decision)
        not in {
            str(LoopDecisionKind.BLOCKED),
            str(LoopDecisionKind.FAILED),
        },
        reason=str(reason),
        evidence={
            "from_node": str(from_state.node),
            "to_node": str(to_state.node),
            "condition": condition,
            "terminal_state": str(terminal_state or ""),
            "attempt": to_state.attempt,
            "checkpoint_id": checkpoint_id,
            "transition_evidence": dict(evidence),
        },
    )
    gate_results: tuple[LoopCheckResult, ...] = ()
    checker_results: tuple[LoopCheckResult, ...] = (check,)
    if str(check.name) in {
        str(LoopCheckName.APPROVAL_GATE),
        str(LoopCheckName.NO_PROGRESS_GATE),
    }:
        gate_results = (check,)
        checker_results = ()
    decision_evidence = {
        "from_node": str(from_state.node),
        "to_node": str(to_state.node),
        "condition": condition,
        "terminal_state": str(terminal_state or ""),
        "loop_run_id": to_state.run_id,
        "loop_spec_id": to_state.loop_spec_id,
        "attempt": to_state.attempt,
    }
    side_effect = _transition_side_effect(evidence)
    if side_effect:
        decision_evidence["side_effect"] = side_effect
    return LoopDecision(
        decision=decision,
        reason=reason,
        phase=LoopPhase.DECISION,
        failure_domain=failure_domain,
        tool=_transition_tool(evidence),
        run_id=to_state.run_id,
        step_id=checkpoint_id,
        goal_ids=(to_state.goal_id,),
        checker_results=checker_results,
        gate_results=gate_results,
        evidence=decision_evidence,
    )


def _gate_loop_decision(
    *,
    state: LoopRunState,
    kind: str,
    grant: ResourceGrant,
) -> LoopDecision:
    decision = _gate_decision_kind(grant)
    check = LoopCheckResult(
        name=LoopCheckName.APPROVAL_GATE,
        passed=grant.allowed,
        reason=grant.reason,
        evidence={
            "kind": kind,
            "grant": grant.to_dict(),
            "loop_run_id": state.run_id,
            "attempt": state.attempt,
        },
    )
    return LoopDecision(
        decision=decision,
        reason=LoopReason.CAPABILITY_FACT_RECORDED
        if grant.allowed
        else LoopReason.APPROVAL_REQUIRED,
        phase=LoopPhase.DECISION,
        failure_domain=TraceFailureDomain.NONE
        if grant.allowed
        else TraceFailureDomain.SAFEGUARD_POLICY,
        tool=kind,
        run_id=state.run_id,
        goal_ids=(state.goal_id,),
        gate_results=(check,),
        evidence={
            "kind": kind,
            "grant": grant.to_dict(),
            "loop_run_id": state.run_id,
            "loop_spec_id": state.loop_spec_id,
            "attempt": state.attempt,
        },
    )


def _gate_decision_kind(grant: ResourceGrant) -> LoopDecisionKind:
    if grant.decision == ResourceDecision.ALLOW:
        return LoopDecisionKind.CONTINUE
    if grant.decision == ResourceDecision.BLOCK:
        return LoopDecisionKind.FAILED
    return LoopDecisionKind.BLOCKED


def _resource_limits_for_spec(spec: LoopSpec) -> ResourceLimits:
    policy = spec.budget_policy
    return ResourceLimits(
        token_budget=policy.token_budget,
        call_budget=policy.call_budget,
        cost_budget=policy.cost_budget,
        qps_limit=policy.qps_limit,
        max_concurrent=policy.max_concurrent,
    )


def _transition_decision_kind(
    state: LoopRunState,
    condition: str,
    terminal_state: LoopTerminalState | str,
) -> LoopDecisionKind:
    terminal = str(terminal_state or "")
    if terminal == str(LoopTerminalState.CONVERGED):
        return LoopDecisionKind.CONVERGED
    if terminal in {
        str(LoopTerminalState.PAUSED),
        str(LoopTerminalState.WAITING_APPROVAL),
        str(LoopTerminalState.BLOCKED),
        str(LoopTerminalState.CONFLICTED),
    }:
        return LoopDecisionKind.BLOCKED
    if terminal:
        return LoopDecisionKind.FAILED
    if condition in {
        "capability_failed",
        "checker_failed",
        "new_route_available",
    } or str(state.node) == str(LoopNode.REFLECT):
        return LoopDecisionKind.RECOVER
    if condition.startswith("resource_"):
        return LoopDecisionKind.BLOCKED
    if condition == "side_effect_commit_required":
        return LoopDecisionKind.BLOCKED
    return LoopDecisionKind.CONTINUE


def _transition_reason(
    condition: str,
    terminal_state: LoopTerminalState | str,
) -> LoopReason:
    terminal = str(terminal_state or "")
    if condition in {"planner_failed", "missing_planned_capability"}:
        return LoopReason.PLANNER_OR_PARSER_FAILURE
    if condition == "capability_failed":
        return LoopReason.CAPABILITY_FAILURE
    if condition in {"checker_failed", "checker_rejected", "no_route_available"}:
        return LoopReason.COMPLETION_CHECKER_BLOCKED
    if condition in {
        "resource_pause",
        "resource_or_user_pause",
        "resource_escalate",
        "side_effect_commit_required",
    }:
        return LoopReason.APPROVAL_REQUIRED
    if condition == "hard_timeout":
        return LoopReason.TERMINAL_RESULT
    if terminal == str(LoopTerminalState.CONVERGED) or condition == "checker_passed":
        return LoopReason.COMPLETION_EVIDENCE_TRUE
    if condition in {"plan_ready", "side_effect_recorded", "continue_iteration"}:
        return LoopReason.CAPABILITY_FACT_RECORDED
    if terminal:
        return LoopReason.TERMINAL_RESULT
    return LoopReason.CAPABILITY_FACT_RECORDED


def _transition_failure_domain(
    condition: str,
    terminal_state: LoopTerminalState | str,
) -> TraceFailureDomain:
    terminal = str(terminal_state or "")
    if condition in {"planner_failed", "missing_planned_capability"}:
        return TraceFailureDomain.PLANNER_OR_PARSER
    if condition == "capability_failed":
        return TraceFailureDomain.CAPABILITY_FAILURE
    if condition in {"checker_failed", "checker_rejected", "no_route_available"}:
        return TraceFailureDomain.CHECKER_BLOCKED
    if condition.startswith("resource_") or condition == "side_effect_commit_required" or terminal in {
        str(LoopTerminalState.PAUSED),
        str(LoopTerminalState.WAITING_APPROVAL),
    }:
        return TraceFailureDomain.SAFEGUARD_POLICY
    if terminal in {
        str(LoopTerminalState.FAILED),
        str(LoopTerminalState.TIMED_OUT),
        str(LoopTerminalState.CANCELLED),
        str(LoopTerminalState.SUPERSEDED),
        str(LoopTerminalState.CONFLICTED),
    }:
        return TraceFailureDomain.RUNTIME
    return TraceFailureDomain.NONE


def _transition_check_name(
    condition: str,
    terminal_state: LoopTerminalState | str,
) -> LoopCheckName:
    if condition in {"planner_failed", "plan_ready"}:
        return LoopCheckName.PLANNER_RESULT
    if condition in {"capability_failed", "side_effect_recorded"}:
        return LoopCheckName.CAPABILITY_RESULT
    if condition.startswith("resource_") or condition == "side_effect_commit_required":
        return LoopCheckName.APPROVAL_GATE
    if terminal_state:
        return LoopCheckName.TERMINAL_RESULT
    return LoopCheckName.COMPLETION_CHECKER


def _transition_tool(evidence: dict[str, Any]) -> str:
    planned = evidence.get("planned_capability")
    if isinstance(planned, dict):
        return str(planned.get("tool") or "")
    executor = evidence.get("executor")
    if isinstance(executor, dict):
        facts = executor.get("facts")
        if isinstance(facts, dict):
            return str(facts.get("tool") or "")
        return str(executor.get("tool") or executor.get("action") or "")
    return str(evidence.get("tool") or evidence.get("action") or "")


def _transition_side_effect(evidence: dict[str, Any]) -> dict[str, Any]:
    executor = evidence.get("executor")
    if not isinstance(executor, dict):
        return {}
    facts = executor.get("facts")
    if not isinstance(facts, dict):
        return {}
    has_explicit_side_effect = any(
        key in facts
        for key in (
            "side_effect_scope",
            "side_effect_state",
            "side_effect_artifact",
            "side_effect_commit",
            "side_effect_compensate",
        )
    ) or str(executor.get("action") or "") == "connector_outbound"
    if not has_explicit_side_effect:
        return {}
    scope = str(facts.get("side_effect_scope") or "")
    state = str(facts.get("side_effect_state") or facts.get("state_transition") or "")
    artifact = str(facts.get("side_effect_artifact") or facts.get("outbound_path") or "")
    if not scope and not state and not artifact:
        return {}
    return {
        "scope": scope,
        "state": state,
        "artifact": artifact,
        "action": str(executor.get("action") or ""),
        "commit": str(facts.get("side_effect_commit") or ""),
        "compensate": str(facts.get("side_effect_compensate") or ""),
    }


def _side_effect_from_collected_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    cap_result = evidence.get("capability_result")
    if not isinstance(cap_result, dict):
        return {}
    return _transition_side_effect({"executor": cap_result})


def _step_runs_command(step: VerificationStep) -> bool:
    return bool(step.command.strip()) and step.kind in {
        VerificationKind.UNIT_TEST,
        VerificationKind.INTEGRATION_TEST,
        VerificationKind.COMMAND_EXIT_CODE,
    }


def _workspace_lock_resource(workspace: Path) -> str:
    return f"workspace:{workspace.expanduser().resolve()}"


def _checker_reason_code(checker_report: CheckerReport) -> str:
    if checker_report.timed_out:
        return "checker_timed_out"
    for item in checker_report.checker_results:
        reason = str(getattr(item, "reason", "") or "").strip()
        if reason:
            return reason
    if checker_report.blocked:
        return "checker_blocked"
    return "checker_rejected"


def _recovery_events_from_evidence(
    evidence: dict[str, Any],
    harness_results: tuple[HarnessResult, ...],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for key, value in evidence.items():
        if isinstance(value, dict):
            events.append({"tool": key, "facts": value})
    for item in harness_results:
        events.append({"tool": "harness.command", "facts": item.to_facts()})
    return events


def _goal_constraints(spec: LoopSpec) -> str:
    lines = [
        f"Objective: {spec.goal.objective}",
        "Acceptance criteria:",
        *[f"- {item}" for item in spec.goal.acceptance_criteria],
    ]
    if spec.goal.constraints:
        lines.extend(["Constraints:", *[f"- {item}" for item in spec.goal.constraints]])
    lines.append(f"Permission ceiling: {spec.goal.permission_ceiling}")
    return "\n".join(lines)
