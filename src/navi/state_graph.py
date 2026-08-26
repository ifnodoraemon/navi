from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import uuid

import httpx

from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Protocol

from .capabilities import CapabilityRegistry
from .capabilities_types import CapabilityContext
from .capability_contract import CAPABILITY_RETRYABLE_KEY
from .checker import CheckerReport, DeterministicChecker
from .control import CurrentStateBuilder, SurfaceContext, current_state_facts, current_time_facts
from .harness import Harness, HarnessCommand, HarnessResult
from .loop import (
    LoopCheckName,
    LoopCheckResult,
    LoopDecision,
    LoopDecisionKind,
    LoopPhase,
    LoopReason,
    TraceFailureDomain,
)
from .loop_contracts import (
    LockMode,
    LoopNode,
    LoopRunState,
    LoopSpec,
    LoopTerminalState,
    MergeStatus,
    ResourceDecision,
    VerificationKind,
    VerificationStep,
    WorkspaceMode,
)
from .loop_runs import LoopCheckpoint, LoopRunStore
from .memory import ACTIVE_MEMORY_CONTEXT_LIMIT
from .model_facts import project_model_facts
from .prompt_os import assemble_semantic_checker_messages
from .resource_gateway import (
    GlobalResourceGateway,
    ResourceGrant,
    ResourceLimitError,
    ResourceLimits,
    ResourceRequest,
)
from .runtime import AgentRuntime
from .safeguards import call_mutates
from .syscalls import ModelSyscallPlanner, provider_failure_facts
from .provider import (
    ChatMessage,
    ProviderHTTPError,
    ProviderResponseError,
    StructuredOutputError,
)
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
PLANNER_ATTEMPT_HISTORY_LIMIT = 8
PLANNER_ATTEMPT_HISTORY_MAX_CHARS = 16_000
PLANNER_ATTEMPT_MESSAGE_MAX_CHARS = 1_000
PLANNER_PRIOR_RESULT_MAX_CHARS = 4_000
PLANNER_AMBIENT_RECORD_LIMIT = 3
SEMANTIC_CHECKER_ATTEMPT_LIMIT = 4
SEMANTIC_CHECKER_ARGS_MAX_CHARS = 3_000
SEMANTIC_CHECKER_FACTS_MAX_CHARS = 6_000
SEMANTIC_CHECKER_MESSAGE_MAX_CHARS = 3_000
# The verdict's evidence_summary must stay short enough that the JSON object
# cannot be truncated by the checker's max_tokens budget. An over-long summary
# was the direct trigger of a turn-killing StructuredOutputError.
SEMANTIC_CHECKER_EVIDENCE_SUMMARY_MAX_CHARS = 2_000
SEMANTIC_CHECKER_VERDICT_ERROR_CHARS = 200
# A malformed checker verdict (truncated or invalid JSON) is a boundary
# failure of the verdict call, not a verdict. The port re-asks the model a
# bounded number of times before failing closed so the loop stays governed.
SEMANTIC_CHECKER_VERDICT_RETRIES = 1
TASK_RESULT_PREVIEW_CHARS = 240
PROVIDER_TRANSPORT_MAX_RETRIES = 3
PROVIDER_TRANSPORT_RETRY_MIN_SECONDS = 1.0
PROVIDER_TRANSPORT_RETRY_MAX_SECONDS = 300.0
EXECUTION_LEASE_MIN_SECONDS = 900.0
EXECUTION_LEASE_HEARTBEAT_MAX_SECONDS = 30.0


def _exc_message(exc: BaseException, *, limit: int = 1_000) -> str:
    """Return a non-empty error message for an exception.

    httpx transport errors (e.g. ``ReadError``) frequently have an empty
    ``str(exc)``; fall back to ``repr`` so evidence and logs stay useful.
    """
    text = str(exc).strip()
    if not text:
        text = repr(exc).strip()
    return truncate_middle(text, limit)


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

    def to_facts(self) -> dict[str, Any]:
        """Return model-facing facts without duplicating durable run evidence."""
        run_state = self.run_state.to_dict()
        durable_evidence = run_state.pop("evidence", {})
        run_state["evidence_keys"] = sorted(durable_evidence)
        return {
            "run_state": run_state,
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
    reason: str = ""
    used_memory_ids: tuple[str, ...] = ()
    used_evidence_ids: tuple[str, ...] = ()
    memory_activation: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "args": dict(self.args),
            "permission": self.permission,
            "reason": self.reason,
            "used_memory_ids": list(self.used_memory_ids),
            "used_evidence_ids": list(self.used_evidence_ids),
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
    yields_control: bool = False
    deterministic_completion_authority: bool = False
    mutates: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "action": self.action,
            "facts": dict(self.facts),
            "message": self.message,
            "error_reason": self.error_reason,
            "terminal": self.terminal,
            "yields_control": self.yields_control,
            "deterministic_completion_authority": self.deterministic_completion_authority,
            "mutates": self.mutates,
        }


@dataclass(frozen=True)
class ReflectionDecision:
    replan_allowed: bool
    reason_code: str
    facts: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "replan_allowed": self.replan_allowed,
            "reason_code": self.reason_code,
            "facts": dict(self.facts),
        }


@dataclass(frozen=True)
class SemanticCheckDecision:
    passed: bool
    evidence_summary: str = ""

    def to_facts(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "ok": self.passed,
            "evidence_summary": self.evidence_summary,
            "evaluator_role": "checker",
            "isolated_context": True,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.to_facts()


class PlannerPort(Protocol):
    async def plan(
        self,
        spec: LoopSpec,
        state: LoopRunState,
        *,
        workspace: Path,
        evidence: dict[str, Any],
    ) -> PlannedCapabilityStep: ...


class ExecutorPort(Protocol):
    async def execute(
        self,
        step: PlannedCapabilityStep,
        spec: LoopSpec,
        state: LoopRunState,
        *,
        workspace: Path,
    ) -> ExecutedCapabilityStep: ...


class SemanticCheckerPort(Protocol):
    async def assess(
        self,
        spec: LoopSpec,
        state: LoopRunState,
        *,
        executed: ExecutedCapabilityStep,
        evidence: dict[str, Any],
    ) -> SemanticCheckDecision: ...


class CapabilityRecoveryPort:
    """Expose failed capability facts and whether the model may plan again."""

    def recover(
        self,
        spec: LoopSpec,
        state: LoopRunState,
        *,
        executed: ExecutedCapabilityStep,
    ) -> ReflectionDecision:
        retryable = executed.facts.get(CAPABILITY_RETRYABLE_KEY) is not False
        recovery_facts = {
            "trigger": "capability.failed",
            "reason_code": "execution_failed",
            "blocked": False,
            "failure_domain": "executor",
            "loop_run_id": state.run_id,
            "attempt": state.attempt,
            "goal_id": spec.goal_id,
            "error_reason": executed.error_reason,
            "retryable": retryable,
            "message": executed.message,
            "facts": executed.facts,
        }
        return ReflectionDecision(
            # ``retryable`` describes this exact capability call, not the
            # objective. The runtime only exposes another bounded planning
            # opportunity; the model owns the semantic recovery decision.
            replan_allowed=state.attempt < spec.retry_policy.max_attempts,
            reason_code="execution_failed" if retryable else "execution_not_retryable",
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


class ContractViolationRecoveryPort:
    """Expose runtime contract violations as facts for LLM-owned repair."""

    def recover(
        self,
        spec: LoopSpec,
        state: LoopRunState,
        *,
        exc: Exception,
        domain: str,
        failure_facts: dict[str, Any] | None = None,
    ) -> ReflectionDecision:
        reason_code = type(exc).__name__
        violation_record: dict[str, Any] = {
            "error_type": reason_code,
            "error": _exc_message(exc, limit=1_000),
            "domain": domain,
        }
        if failure_facts:
            violation_record["failure_facts"] = failure_facts
        recovery_facts = {
            "trigger": "runtime.contract_failed",
            "reason_code": reason_code,
            "blocked": False,
            "failure_domain": domain,
            "loop_run_id": state.run_id,
            "attempt": state.attempt,
            "max_attempts": spec.retry_policy.max_attempts,
            "goal_id": spec.goal_id,
            "contract_violation": violation_record,
            "contract": {
                "syscall_count": "exactly_one",
                "runtime_executes_one_syscall_per_plan": True,
                "respond_is_terminal": True,
            },
        }
        return ReflectionDecision(
            replan_allowed=state.attempt < spec.retry_policy.max_attempts,
            reason_code=reason_code,
            facts={
                "recovery": recovery_facts,
                "recovery_fact": json.dumps(
                    {
                        "fact_type": "runtime_contract_failed",
                        "facts": recovery_facts,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ),
            },
        )


class RecoveryReflectorPort:
    """Expose checker failure facts and whether the model may plan again."""

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
            "blocked": checker_report.blocked,
            "failure_domain": (
                "checker_blocked" if checker_report.blocked else "verification_failed"
            ),
            "loop_run_id": state.run_id,
            "attempt": state.attempt,
            "goal_id": spec.goal_id,
            "checker_report": checker_report.to_dict(),
            "harness_results": [item.to_facts() for item in harness_results],
        }
        return ReflectionDecision(
            replan_allowed=reason_code != "no_route_available"
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

    async def assess(
        self,
        spec: LoopSpec,
        state: LoopRunState,
        *,
        executed: ExecutedCapabilityStep,
        evidence: dict[str, Any],
    ) -> SemanticCheckDecision:
        messages = self._build_messages(spec, state, executed=executed, evidence=evidence)
        verdict_failures: list[str] = []
        for _ in range(1 + SEMANTIC_CHECKER_VERDICT_RETRIES):
            try:
                response = await self.runtime.provider.complete_for(
                    "checker",
                    messages,
                    output_schema=_semantic_checker_output_schema(),
                )
                return self._parse(response)
            except (StructuredOutputError, json.JSONDecodeError) as exc:
                # A malformed or truncated verdict is a boundary failure of the
                # verdict call, not a verdict. Re-ask a bounded number of times,
                # then fail closed so the loop reflects/retries or terminates
                # governed instead of leaking an unhandled internal error.
                # Transport and resource errors are deliberately NOT caught here:
                # they keep the durable retry-gate / resource-pause machinery.
                verdict_failures.append(
                    _exc_message(exc, limit=SEMANTIC_CHECKER_VERDICT_ERROR_CHARS)
                )
        return SemanticCheckDecision(
            passed=False,
            evidence_summary=self._verdict_failure_summary(verdict_failures),
        )

    def _build_messages(
        self,
        spec: LoopSpec,
        state: LoopRunState,
        *,
        executed: ExecutedCapabilityStep,
        evidence: dict[str, Any],
    ) -> list[ChatMessage]:
        return assemble_semantic_checker_messages(
            objective=spec.goal.objective,
            acceptance_criteria=list(spec.goal.acceptance_criteria),
            conversation_context=_semantic_checker_conversation_context(
                runtime=self.runtime,
                spec=spec,
            ),
            current_time=current_time_facts(),
            trigger_facts=_goal_trigger_facts(spec),
            task_context=_semantic_checker_task_context(spec, executed),
            evaluation_contract=_semantic_checker_evaluation_contract(
                spec,
                executed,
            ),
            attempt=state.attempt,
            max_attempts=spec.retry_policy.max_attempts,
            last_capability=_semantic_checker_capability_result(executed),
            observed_capability_evidence=_semantic_checker_attempt_evidence(evidence),
        )

    @staticmethod
    def _parse(response: str) -> SemanticCheckDecision:
        data = json.loads(response)
        if not isinstance(data, dict):
            raise json.JSONDecodeError(
                "checker verdict must be a JSON object", response, 0
            )
        return SemanticCheckDecision(
            passed=bool(data.get("passed", False)),
            evidence_summary=str(data.get("evidence_summary", "")),
        )

    @staticmethod
    def _verdict_failure_summary(failures: list[str]) -> str:
        detail = " | ".join(failures[-2:])
        return (
            "semantic checker returned no valid verdict after "
            f"{len(failures)} attempt(s); failing closed. last error: {detail}"
        )


def _semantic_checker_output_schema() -> dict[str, Any]:
    return {
        "name": "semantic_check_decision",
        "strict": False,
        "schema": {
            "type": "object",
            "properties": {
                "passed": {"type": "boolean"},
                "evidence_summary": {
                    "type": "string",
                    "maxLength": SEMANTIC_CHECKER_EVIDENCE_SUMMARY_MAX_CHARS,
                },
            },
            "required": ["passed", "evidence_summary"],
            "additionalProperties": False,
        },
    }


class SemanticCheckerCallError(RuntimeError):
    """Preserve the checker-call boundary for typed provider recovery."""

    def __init__(self, cause: Exception):
        super().__init__(str(cause))
        self.cause = cause


def _goal_trigger_facts(spec: LoopSpec) -> dict[str, Any]:
    value = spec.goal.metadata.get("trigger_facts")
    return dict(value) if isinstance(value, dict) else {}


def _semantic_checker_evaluation_contract(
    spec: LoopSpec,
    executed: ExecutedCapabilityStep,
) -> dict[str, Any]:
    task_context = _goal_task_context(spec)
    delivery = task_context.get("delivery")
    delivery_stage = (
        str(delivery.get("stage") or "")
        if isinstance(delivery, dict)
        else ""
    )
    current_result = _executed_result_text(executed)
    candidate_copy_present = bool(current_result["text"]) and executed.action in {
        "ask",
        "chat",
        "respond",
    }
    return {
        "scope": (
            "candidate_semantics_before_external_transport"
            if candidate_copy_present
            else "capability_evidence_before_candidate_presentation"
        ),
        "evaluates": [
            "objective_coverage",
            "acceptance_criteria",
            "grounding_in_authoritative_capability_facts",
            "contradiction_absence",
        ],
        "does_not_evaluate": [
            "connector_transport",
            "external_delivery_receipt",
        ],
        "presentation_semantics": {
            "candidate_copy_role": "proposed_user_facing_communication",
            "candidate_copy_present": candidate_copy_present,
            "candidate_copy_source": (
                current_result["source"] if candidate_copy_present else ""
            ),
            "conversation_assistant_is_current_candidate": False,
            "communication_obligation_rule": (
                "judge whether the candidate copy communicates the requested grounded "
                "content within this pre-transport scope"
                if candidate_copy_present
                else "judge whether current capability evidence covers the objective; "
                "missing candidate copy is not a failure because a passed fact check "
                "enters the governed response phase"
            ),
            "transport_proof_rule": (
                "never require an outbox entry, connector send result, or external "
                "delivery receipt in this check"
            ),
        },
        "downstream_transport_evidence_unavailable_by_design": [
            "outbox_entry",
            "connector_send_result",
            "external_delivery_receipt",
        ],
        "transport_receipt_required_for_this_check": False,
        "transport_stage_after_acceptance": (
            delivery_stage == "post_semantic_acceptance_outbox"
        ),
        "conversation_context_authority": "resolve_referents_only",
    }


def _semantic_checker_task_context(
    spec: LoopSpec,
    executed: ExecutedCapabilityStep,
) -> dict[str, Any]:
    """Project only facts the pre-transport semantic checker may evaluate."""
    context = _goal_task_context_with_result_comparison(spec, executed)
    # Delivery is owned by a later durable protocol. Its necessarily
    # receipt-free pre-acceptance state would create a dependency cycle:
    # semantic acceptance would require the transport it authorizes.
    context.pop("delivery", None)
    return context


def _goal_task_context(spec: LoopSpec) -> dict[str, Any]:
    metadata = spec.goal.metadata
    raw_context = metadata.get("task_context")
    context = dict(raw_context) if isinstance(raw_context, dict) else {}
    raw_lineage = context.get("lineage")
    lineage = dict(raw_lineage) if isinstance(raw_lineage, dict) else {}
    raw_progress = context.get("progress")
    progress = dict(raw_progress) if isinstance(raw_progress, dict) else {}
    raw_delivery = context.get("delivery")
    delivery = dict(raw_delivery) if isinstance(raw_delivery, dict) else {}
    parent_goal_id = str(metadata.get("parent_goal_id") or "")
    lineage_id = str(lineage.get("id") or parent_goal_id or spec.goal_id)
    prior_items = [
        dict(item)
        for item in progress.get("authoritative_prior_items") or []
        if isinstance(item, dict)
    ]
    return {
        "lineage": {
            "id": lineage_id,
            "kind": str(lineage.get("kind") or "goal"),
            "current_goal_id": spec.goal_id,
            "parent_goal_id": parent_goal_id,
        },
        "progress": {
            "scope": str(progress.get("scope") or "goal"),
            "sequence_number": _int_or_default(progress.get("sequence_number"), 0),
            "authority": str(progress.get("authority") or "current_goal"),
            "authoritative_prior_items": prior_items,
            "authoritative_prior_item_count": len(prior_items),
            "prior_result_facts": _prior_result_facts(prior_items),
            "ambient_history_authoritative": bool(
                progress.get("ambient_history_authoritative", False)
            ),
        },
        "delivery": {
            "stage": str(delivery.get("stage") or "not_applicable"),
            "transport_receipt_available": bool(delivery.get("transport_receipt_available", False)),
        },
    }


def _goal_task_context_with_result_comparison(
    spec: LoopSpec,
    executed: ExecutedCapabilityStep,
) -> dict[str, Any]:
    context = _goal_task_context(spec)
    raw_progress = context.get("progress")
    progress = dict(raw_progress) if isinstance(raw_progress, dict) else {}
    prior_items = [
        dict(item)
        for item in progress.get("authoritative_prior_items") or []
        if isinstance(item, dict)
    ]
    progress["current_result_comparison"] = _result_comparison_facts(
        current=_executed_result_text(executed),
        prior_items=prior_items,
    )
    return {**context, "progress": progress}


def _prior_result_facts(prior_items: list[dict[str, Any]]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for index, item in enumerate(prior_items, start=1):
        prior = _prior_result_text(item)
        if not prior["text"]:
            continue
        text = prior["text"]
        canonical = _canonical_result_text(text)
        items.append(
            {
                "prior_index": index,
                "goal_id": str(item.get("goal_id") or ""),
                "run_id": str(item.get("run_id") or ""),
                "source": prior["source"],
                "canonical_sha256_16": _text_fingerprint(canonical),
                "char_count": len(text),
                "preview": _result_preview(text),
            }
        )
    return {
        "item_count": len(items),
        "items": items,
        "canonical_whitespace_policy": "collapse_all_whitespace",
    }


def _result_comparison_facts(
    *,
    current: dict[str, str],
    prior_items: list[dict[str, Any]],
) -> dict[str, Any]:
    current_text = current["text"]
    current_canonical = _canonical_result_text(current_text)
    current_facts: dict[str, Any] = {
        "present": bool(current_text),
        "source": current["source"],
        "canonical_sha256_16": _text_fingerprint(current_canonical) if current_text else "",
        "char_count": len(current_text),
        "preview": _result_preview(current_text),
    }
    comparisons: list[dict[str, Any]] = []
    for index, item in enumerate(prior_items, start=1):
        prior = _prior_result_text(item)
        prior_text = prior["text"]
        if not current_text or not prior_text:
            continue
        prior_canonical = _canonical_result_text(prior_text)
        exact_duplicate = bool(current_canonical and current_canonical == prior_canonical)
        similarity = (
            1.0
            if exact_duplicate
            else SequenceMatcher(None, current_canonical, prior_canonical).ratio()
        )
        comparisons.append(
            {
                "prior_index": index,
                "goal_id": str(item.get("goal_id") or ""),
                "run_id": str(item.get("run_id") or ""),
                "prior_source": prior["source"],
                "prior_canonical_sha256_16": _text_fingerprint(prior_canonical),
                "exact_duplicate": exact_duplicate,
                "similarity": round(float(similarity), 4),
                "length_delta": len(current_text) - len(prior_text),
            }
        )
    max_similarity = max((item["similarity"] for item in comparisons), default=0.0)
    exact_count = sum(1 for item in comparisons if item["exact_duplicate"])
    latest = comparisons[-1] if comparisons else {}
    most_similar = max(comparisons, key=lambda item: item["similarity"], default={})
    return {
        "current_result": current_facts,
        "prior_comparisons": comparisons,
        "prior_comparison_count": len(comparisons),
        "exact_duplicate_prior_count": exact_count,
        "latest_prior_exact_duplicate": bool(latest.get("exact_duplicate", False)),
        "max_similarity": round(float(max_similarity), 4),
        "most_similar_prior_index": most_similar.get("prior_index", 0),
        "canonical_whitespace_policy": "collapse_all_whitespace",
    }


def _executed_result_text(executed: ExecutedCapabilityStep) -> dict[str, str]:
    facts = executed.facts if isinstance(executed.facts, dict) else {}
    for key in ("responded_message", "message", "body", "text", "result_summary"):
        value = str(facts.get(key) or "").strip()
        if value:
            return {"text": value, "source": f"capability.facts.{key}"}
    message = str(executed.message or "").strip()
    if message:
        return {"text": message, "source": "capability.message"}
    return {"text": "", "source": ""}


def _prior_result_text(item: dict[str, Any]) -> dict[str, str]:
    for key in ("accepted_result_text", "result_summary", "body", "message", "text"):
        value = str(item.get(key) or "").strip()
        if value:
            return {"text": value, "source": f"prior_item.{key}"}
    accepted_result = item.get("accepted_result")
    if isinstance(accepted_result, dict):
        for key in ("body", "responded_message", "message", "text", "result_summary"):
            value = str(accepted_result.get(key) or "").strip()
            if value:
                return {"text": value, "source": f"prior_item.accepted_result.{key}"}
    return {"text": "", "source": ""}


def _canonical_result_text(text: str) -> str:
    return " ".join(str(text or "").split()).strip()


def _text_fingerprint(text: str) -> str:
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _result_preview(text: str) -> str:
    clean = _canonical_result_text(text)
    return truncate_middle(clean, TASK_RESULT_PREVIEW_CHARS)


def _int_or_default(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class BoundedConversationContext:
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
    commit_strategy: str = ""
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
            "commit_strategy": self.commit_strategy,
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
        commit_strategy = str(side_effect.get("commit_strategy") or "")
        if state not in {"staged", "prepared"}:
            return SideEffectSagaResult(
                action="commit",
                ok=True,
                scope=scope,
                state=state or "no_staged_side_effect",
                artifact=artifact,
                commit=commit,
                compensate=compensate,
                commit_strategy=commit_strategy,
                reason="side_effect_not_staged",
            )
        if commit and commit_strategy == "deferred":
            return SideEffectSagaResult(
                action="commit",
                ok=True,
                scope=scope,
                state="released_for_deferred_commit",
                artifact=artifact,
                commit=commit,
                compensate=compensate,
                commit_strategy=commit_strategy,
                reason="deferred_commit_released",
            )
        return SideEffectSagaResult(
            action="commit",
            ok=False,
            scope=scope,
            state="commit_handler_missing",
            artifact=artifact,
            commit=commit,
            compensate=compensate,
            commit_strategy=commit_strategy,
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


def _bounded_conversation_context(
    *,
    session_id: str,
    messages: list[Any],
    consumer: str,
) -> BoundedConversationContext:
    relevant = [msg for msg in messages if str(getattr(msg, "role", "")) in {"user", "assistant"}]
    raw_chars = sum(len(str(getattr(msg, "content", "") or "")) for msg in relevant)
    base_facts: dict[str, Any] = {
        "session_id": session_id,
        "message_count": len(relevant),
        "raw_character_count": raw_chars,
        "max_character_count": PLANNER_CONTEXT_MAX_CHARS,
        "recent_message_limit": PLANNER_CONTEXT_RECENT_MESSAGES,
        "message_fetch_limit": PLANNER_CONTEXT_MESSAGE_LIMIT,
        "policy": "bounded_conversation_context_v1",
        "consumer": consumer,
    }
    if not relevant:
        return BoundedConversationContext(text="", facts={**base_facts, "compacted": False})

    raw_text = "\n\n".join(_format_conversation_message(msg) for msg in relevant)
    should_compact = (
        len(relevant) > PLANNER_CONTEXT_RECENT_MESSAGES or len(raw_text) > PLANNER_CONTEXT_MAX_CHARS
    )
    if not should_compact:
        return BoundedConversationContext(
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
    older = relevant[:-PLANNER_CONTEXT_RECENT_MESSAGES]
    older_user_messages = [
        (index, msg)
        for index, msg in enumerate(older, start=1)
        if str(getattr(msg, "role", "")) == "user"
    ]
    preview_items = older_user_messages[-PLANNER_CONTEXT_OLDER_PREVIEW_MESSAGES:]
    preview_lines = [
        f"Conversation context was compacted before {consumer} intake.",
        f"Older messages omitted: {len(older)}.",
        (
            "Older user-message provenance preview. Older assistant replies are "
            "omitted because they are non-authoritative candidate text:"
        ),
    ]
    for index, msg in preview_items:
        preview = _head_preview(str(getattr(msg, "content", "") or ""))
        preview_lines.append(
            f"- older_index={index} role={getattr(msg, 'role', '') or ''!s} "
            f"created_at={float(getattr(msg, 'created_at', 0.0) or 0.0):.3f} "
            f"chars={len(str(getattr(msg, 'content', '') or ''))} preview={preview}"
        )
    recent_lines = [f"Recent messages preserved for {consumer}:"]
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
    return BoundedConversationContext(
        text=compacted_text,
        facts={
            **base_facts,
            "compacted": True,
            "retained_recent_message_count": len(recent),
            "omitted_message_count": len(older),
            "older_preview_count": len(preview_items),
            "omitted_older_assistant_message_count": len(older) - len(older_user_messages),
            "truncated_recent_message_count": truncated_recent,
            "compacted_character_count": len(compacted_text),
        },
    )


def _conversation_context_policy(spec: LoopSpec) -> tuple[bool, str]:
    """Keep ambient transcript out of detached background cognition by default."""
    metadata = spec.goal.metadata if isinstance(spec.goal.metadata, dict) else {}
    execution_mode = str(metadata.get("execution_mode") or "")
    task_context = metadata.get("task_context")
    progress = (
        task_context.get("progress")
        if isinstance(task_context, dict)
        else {}
    )
    ambient_authoritative = bool(
        progress.get("ambient_history_authoritative", False)
        if isinstance(progress, dict)
        else False
    )
    if execution_mode == "background" and not ambient_authoritative:
        return False, "background_ambient_history_not_authoritative"
    if ambient_authoritative:
        return True, "task_context_declared_ambient_history_authoritative"
    return True, "foreground_conversation_continuity"


def _semantic_checker_conversation_context(
    *,
    runtime: AgentRuntime,
    spec: LoopSpec,
) -> dict[str, Any]:
    session_id = str(spec.goal.metadata.get("session_id") or "")
    authority = {
        "authority": "semantic_context_only",
        "establishes": ["conversation_referents", "elliptical_turn_meaning"],
        "does_not_establish": [
            "capability_facts",
            "task_completion",
            "external_effects",
            "connector_delivery",
        ],
    }
    if not session_id:
        return {
            **authority,
            "included": False,
            "reason": "no_session_context",
            "policy": "bounded_conversation_context_v1",
        }
    include_conversation, reason = _conversation_context_policy(spec)
    if not include_conversation:
        return {
            **authority,
            "session_id": session_id,
            "included": False,
            "reason": reason,
            "policy": "bounded_conversation_context_v1",
        }
    conversation = _bounded_conversation_context(
        session_id=session_id,
        messages=runtime.memory.get_messages(
            session_id,
            limit=PLANNER_CONTEXT_MESSAGE_LIMIT,
        ),
        consumer="semantic_checker",
    )
    return {
        **authority,
        **conversation.facts,
        "included": True,
        "reason": reason,
        "transcript": conversation.text,
    }


def _planner_memory_context(*, memory: Any, spec: LoopSpec) -> PlannerMemoryContext:
    query = spec.goal.objective
    allowed_scopes = _memory_scopes_for_spec(spec, home=memory.home)
    recalls = memory.recall(
        query,
        limit=ACTIVE_MEMORY_CONTEXT_LIMIT,
        allowed_scopes=allowed_scopes,
    )
    candidate_ids = tuple(recall.item.id for recall in recalls)
    facts = {
        "policy": "planner_memory_context_v1",
        "query": query,
        "candidate_ids": list(candidate_ids),
        "count": len(candidate_ids),
        "limit": ACTIVE_MEMORY_CONTEXT_LIMIT,
        "allowed_scopes": sorted(allowed_scopes),
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
    raw_role = str(getattr(message, "role", "") or "")
    role = raw_role.upper()
    if raw_role == "assistant":
        role = "ASSISTANT_CANDIDATE_NON_AUTHORITATIVE"
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

    def __init__(
        self,
        *,
        runtime: AgentRuntime,
        capabilities: CapabilityRegistry,
        context: CapabilityContext | None = None,
    ):
        self.runtime = runtime
        self.capabilities = capabilities
        self.context = context
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
        policy_context = replace(
            self.context
            or CapabilityContext(
                home=self.capabilities.home,
                source="state_graph",
                peer_id="state_graph",
                sender_id=spec.goal.owner or "state_graph",
            ),
            permission_ceiling=spec.goal.permission_ceiling,
            workspace=_scope_workspace_for_spec(spec, default_workspace=workspace),
        )
        tools = [
            item
            for item in self.capabilities.planner_specs(
                permission_ceiling=spec.goal.permission_ceiling,
            )
            if "*" in allowed or item.name in allowed
        ]
        if not tools:
            raise StructuredOutputError(
                f"LoopSpec allowed no planner-visible capabilities: {sorted(allowed)}"
            )
        session_id = spec.goal.metadata.get("session_id") or ""
        conversation_context = ""
        conversation_facts: dict[str, Any] = {}
        if session_id:
            include_conversation, conversation_reason = _conversation_context_policy(spec)
            if include_conversation:
                conversation = _bounded_conversation_context(
                    session_id=session_id,
                    messages=self.runtime.memory.get_messages(
                        session_id,
                        limit=PLANNER_CONTEXT_MESSAGE_LIMIT,
                    ),
                    consumer="planner",
                )
                conversation_context = conversation.text
                conversation_facts = {
                    **conversation.facts,
                    "included": True,
                    "reason": conversation_reason,
                }
            else:
                conversation_facts = {
                    "session_id": session_id,
                    "included": False,
                    "reason": conversation_reason,
                    "policy": "bounded_conversation_context_v1",
                    "consumer": "planner",
                }
        memory_context = _planner_memory_context(memory=self.runtime.memory, spec=spec)

        syscalls = await self.planner.plan(
            spec.goal.objective,
            tools=tools,
            conversation_context=conversation_context,
            runtime_facts={
                "loop_spec": _planner_loop_spec_facts(spec),
                "loop_run_state": _planner_loop_run_facts(state),
                "objective_evidence": _planner_objective_evidence(evidence),
                "attempt_history": _planner_attempt_history(evidence),
                "conversation_compaction": conversation_facts,
                "memory_context": memory_context.facts,
                "ingress_facts": _planner_ingress_facts(policy_context, spec),
                "last_verification_failure": _planner_verification_failure_text(evidence),
            },
            permission_ceiling=spec.goal.permission_ceiling,
            durable_constraints=_durable_constraints(self.runtime, spec),
            memory_context=memory_context.text,
        )
        if len(syscalls) != 1:
            raise StructuredOutputError(
                f"planner_must_return_exactly_one_syscall: count={len(syscalls)}"
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
            reason=selected.reason,
            used_memory_ids=used_memory_ids,
            used_evidence_ids=selected.used_evidence_ids,
            memory_activation=memory_activation,
        )


class CapabilityExecutorPort:
    """Executor node port that invokes the capability registry in the node workspace."""

    def __init__(
        self,
        *,
        home: Path,
        context: CapabilityContext,
        runtime: AgentRuntime | None = None,
        sensitive_approval_mode: str = "enforce",
        governed_run_id: str = "",
    ):
        self.home = home
        self.context = context
        self.runtime = runtime
        self.sensitive_approval_mode = sensitive_approval_mode
        self.governed_run_id = governed_run_id

    async def execute(
        self,
        step: PlannedCapabilityStep,
        spec: LoopSpec,
        state: LoopRunState,
        *,
        workspace: Path,
    ) -> ExecutedCapabilityStep:
        spec_allowed = set(spec.allowed_capabilities)
        context_allowed = (
            set(self.context.allowed_tools) if self.context.allowed_tools is not None else None
        )
        if "*" in spec_allowed:
            effective_allowed = context_allowed
        elif context_allowed is None:
            effective_allowed = spec_allowed
        else:
            effective_allowed = spec_allowed & context_allowed
        registry = CapabilityRegistry(
            home=self.home,
            project_dir=workspace,
            allowed_tools=effective_allowed,
            disabled_tools=set(self.context.disabled_tools),
            disabled_capability_classes=self.context.disabled_capability_classes,
            permission_ceiling=spec.goal.permission_ceiling,
            governed_run_id=self.governed_run_id or state.run_id,
            sensitive_approval_mode=self.sensitive_approval_mode,
            runtime=self.runtime,
            resource_gateway=(
                self.runtime.provider.current_resource_gateway()
                if self.runtime is not None
                and hasattr(self.runtime.provider, "current_resource_gateway")
                else None
            ),
        )
        context = replace(
            self.context,
            workspace=_scope_workspace_for_spec(
                spec,
                default_workspace=(
                    Path(self.context.workspace) if self.context.workspace else workspace
                ),
            ),
            permission_ceiling=spec.goal.permission_ceiling,
            source=self.context.source or "state_graph",
            effect_idempotency_key=_effect_idempotency_key(state, step),
        )
        tool_spec = registry.get(step.tool)
        execution_args = _execution_args_for_workspace(
            tool_spec,
            step.args,
            logical_workspace=context.workspace,
            execution_workspace=workspace,
        )
        result = await registry.invoke(
            step.tool,
            execution_args,
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
            yields_control=result.yields_control,
            deterministic_completion_authority=bool(
                tool_spec and tool_spec.deterministic_completion_authority
            ),
            mutates=bool(
                tool_spec
                and call_mutates(tool_spec, execution_args)
            ),
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
        planner_port: PlannerPort | None = None,
        executor_port: ExecutorPort | None = None,
        reflector_port: RecoveryReflectorPort | None = None,
        recovery_port: CapabilityRecoveryPort | None = None,
        contract_violation_recovery_port: ContractViolationRecoveryPort | None = None,
        semantic_checker_port: SemanticCheckerPort | None = None,
        side_effect_saga_port: SideEffectSagaPort | None = None,
        trace_store: TraceStore | None = None,
        trace_context: CapabilityContext | None = None,
        execution_owner: str = "",
        account_phase_gates: bool | None = None,
    ):
        self.home = home
        self.store = LoopRunStore(home)
        self.gateway = gateway
        self._account_phase_gates = (
            gateway is None if account_phase_gates is None else account_phase_gates
        )
        self.harness = harness or Harness(home=home)
        self.checker = checker or DeterministicChecker()
        self.planner_port = planner_port
        self.executor_port = executor_port
        self.reflector_port = reflector_port or RecoveryReflectorPort()
        self.recovery_port = recovery_port or CapabilityRecoveryPort()
        self.contract_violation_recovery_port = (
            contract_violation_recovery_port or ContractViolationRecoveryPort()
        )
        self.semantic_checker_port = semantic_checker_port
        self.side_effect_saga_port = side_effect_saga_port or SideEffectSagaPort(home=home)
        self.trace_store = trace_store
        self.trace_context = trace_context
        self.execution_owner = (
            execution_owner or f"state-graph:{os.getpid()}:{uuid.uuid4().hex}"
        )
        self._lease_claimed = False
        self._lease_heartbeat_task: asyncio.Task[None] | None = None
        self._lease_renewal_error: RuntimeError | None = None
        self._pending_transition_evidence: dict[str, Any] = {}

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
        self._lease_renewal_error = None
        self._pending_transition_evidence.clear()
        next_run_id = run_id
        next_evidence = evidence
        try:
            while True:
                result = await self._run_async_impl(
                    spec,
                    workspace=workspace,
                    run_id=next_run_id,
                    evidence=next_evidence,
                )
                if self._lease_renewal_error is not None:
                    raise self._lease_renewal_error
                if result.run_state.node != LoopNode.PLAN or result.run_state.is_stopped():
                    return result
                next_run_id = result.run_state.run_id
                next_evidence = result.evidence
        finally:
            await self._stop_execution_lease_heartbeat()
            self._lease_claimed = False
            self._pending_transition_evidence.clear()

    async def _run_async_impl(
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
        if state.is_stopped():
            return StateGraphRunResult(run_state=state, evidence=evidence or {})
        if not self._lease_claimed:
            lease_seconds = max(
                EXECUTION_LEASE_MIN_SECONDS,
                max(step.timeout.seconds for step in spec.verification_ladder) + 60.0,
            )
            claimed = self.store.claim_for_execution(
                state.run_id,
                owner=self.execution_owner,
                lease_seconds=lease_seconds,
            )
            if claimed is None:
                raise RuntimeError("loop run is already claimed by another execution driver")
            state = claimed
            self._lease_claimed = True
            self._start_execution_lease_heartbeat(
                run_id=state.run_id,
                lease_seconds=lease_seconds,
            )

        collected_evidence: dict[str, Any] = dict(evidence or {})
        attempt_history: list[dict[str, Any]] = list(
            collected_evidence.get("attempt_history") or []
        )
        grants: list[ResourceGrant] = []
        harness_results: list[HarnessResult] = []
        checker_report: CheckerReport | None = None
        planned_step = (
            self._planned_step_from_checkpoint(state.run_id)
            if state.node == LoopNode.EXECUTE
            else None
        )
        if planned_step is None and state.node == LoopNode.EXECUTE:
            planned_step = self._planned_step_from_raw(collected_evidence.get("planned_capability"))
        execution_workspace = workspace
        shadow_workspace = None
        if str(spec.workspace_policy.mode) == str(WorkspaceMode.SHADOW):
            shadow_workspace = self.harness.create_shadow_workspace(
                run_id=state.run_id,
                workspace=workspace,
            )
            execution_workspace = Path(shadow_workspace.shadow_workspace)
            collected_evidence["shadow_workspace"] = shadow_workspace.to_facts()

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
                try:
                    planned_step = await self.planner_port.plan(
                        spec,
                        state,
                        workspace=execution_workspace,
                        evidence=collected_evidence,
                    )
                except ResourceLimitError as exc:
                    grants.append(exc.grant)
                    self._record_gate_decision(
                        state=state,
                        kind="llm:planner",
                        grant=exc.grant,
                    )
                    return StateGraphRunResult(
                        run_state=self._stop_for_resource_grant(state, exc.grant),
                        resource_grants=tuple(grants),
                        evidence=collected_evidence,
                    )
                except (
                    ProviderHTTPError,
                    ProviderResponseError,
                    StructuredOutputError,
                    httpx.TransportError,
                ) as planner_exc:
                    self._discard_shadow_if_needed(spec, state.run_id, shadow_workspace)
                    planner_failures = list(collected_evidence.get("planner_failure_history") or [])
                    failure_facts = provider_failure_facts(planner_exc)
                    planner_failures.append(
                        {
                            "attempt": state.attempt,
                            "error_type": type(planner_exc).__name__,
                            "error": _exc_message(planner_exc, limit=1_000),
                            "failure_facts": failure_facts,
                        }
                    )
                    collected_evidence["planner_failure_history"] = planner_failures[-5:]
                    decision = self.contract_violation_recovery_port.recover(
                        spec,
                        state,
                        exc=planner_exc,
                        domain="planner",
                        failure_facts=failure_facts,
                    )
                    is_transport_failure = bool(
                        failure_facts.get("provider_call_failure")
                    ) and not bool(failure_facts.get("structured_output_failure"))
                    if is_transport_failure:
                        decision = replace(
                            decision,
                            replan_allowed=False,
                            facts={
                                **decision.facts,
                                "automatic_model_retry": False,
                            },
                        )
                        retry_gate = _next_provider_transport_retry_gate_for_facts(
                            failure_facts,
                            evidence=collected_evidence,
                            model_role="planner",
                            resume_node=LoopNode.PLAN,
                        )
                        if retry_gate is not None:
                            retry_evidence = {
                                "error_type": type(planner_exc).__name__,
                                "failure_facts": failure_facts,
                                "automatic_model_retry": False,
                                "durable_transport_retry": True,
                                "retry_gate": retry_gate,
                            }
                            collected_evidence.update(retry_evidence)
                            state = self._pause_for_provider_transport_retry(
                                state,
                                evidence=retry_evidence,
                            )
                            self.gateway.release(grant_id=grant.grant_id)
                            return StateGraphRunResult(
                                run_state=state,
                                resource_grants=tuple(grants),
                                evidence=collected_evidence,
                            )
                        if bool(failure_facts.get("retryable", False)):
                            decision = replace(
                                decision,
                                facts={
                                    **decision.facts,
                                    "durable_transport_retry": True,
                                    "transport_retry_exhausted": True,
                                },
                            )
                    collected_evidence["reflection"] = decision.to_dict()
                    planner_error_evidence = {
                        "error_type": type(planner_exc).__name__,
                        "error": _exc_message(planner_exc, limit=1_000),
                    }
                    if decision.replan_allowed:
                        reflected = self._transition(
                            state,
                            node=LoopNode.REFLECT,
                            condition="planner_failed",
                            evidence=planner_error_evidence,
                        )
                        state = self._transition(
                            reflected,
                            node=LoopNode.PLAN,
                            condition="new_route_available",
                            evidence=decision.to_dict(),
                        )
                        self.gateway.release(grant_id=grant.grant_id)
                        return StateGraphRunResult(
                            run_state=state,
                            resource_grants=tuple(grants),
                            harness_results=tuple(harness_results),
                            evidence=collected_evidence,
                        )
                    state = self._transition(
                        state,
                        node=LoopNode.PLAN,
                        condition="planner_failed",
                        terminal_state=LoopTerminalState.FAILED,
                        evidence={
                            **planner_error_evidence,
                            "reflection": decision.to_dict(),
                        },
                    )
                    self.gateway.release(grant_id=grant.grant_id)
                    return StateGraphRunResult(
                        run_state=state,
                        resource_grants=tuple(grants),
                        evidence=collected_evidence,
                    )
                except Exception:
                    self._discard_shadow_if_needed(spec, state.run_id, shadow_workspace)
                    raise

                collected_evidence["planned_capability"] = planned_step.to_dict()
                _clear_provider_transport_retry_gate(
                    collected_evidence,
                    model_role="planner",
                )
            state = self._transition(
                state,
                node=LoopNode.EXECUTE,
                condition="plan_ready",
                evidence={
                    "planned_capability": planned_step.to_dict(),
                    **_provider_transport_retry_clearance_evidence(collected_evidence),
                },
            )
            self.gateway.release(grant_id=grant.grant_id)

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
                self.gateway.release(grant_id=grant.grant_id)
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
            try:
                executed = await self.executor_port.execute(
                    planned_step,
                    spec,
                    state,
                    workspace=execution_workspace,
                )
            except Exception:
                self._discard_shadow_if_needed(spec, state.run_id, shadow_workspace)
                raise
            collected_evidence["capability_result"] = executed.to_dict()
            # ``ask`` explicitly yields control, so it is safe to surface before
            # evaluation. A terminal chat response is only a candidate until the
            # checker accepts it; rejected text must never cross the reply boundary.
            if executed.action == "ask" and executed.message:
                collected_evidence["responded_message"] = executed.message
                collected_evidence["responded_action"] = executed.action
            else:
                collected_evidence.pop("responded_message", None)
                collected_evidence.pop("responded_action", None)
            if executed.action == "chat" and executed.message:
                collected_evidence["candidate_response"] = executed.message
                collected_evidence["candidate_response_action"] = executed.action
            else:
                collected_evidence.pop("candidate_response", None)
                collected_evidence.pop("candidate_response_action", None)
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
                    "progress_signature": _progress_signature(planned_step, executed),
                }
            )
            collected_evidence["attempt_history"] = attempt_history
            progress_signature = str(attempt_history[-1]["progress_signature"])
            collected_evidence["progress_signature"] = progress_signature
            execution_evidence = {
                **executed.to_dict(),
                "progress_signature": progress_signature,
            }
            repeated_count = sum(
                1
                for item in attempt_history
                if item.get("progress_signature") == progress_signature
            )
            if repeated_count >= 3 and not executed.mutates:
                warning_count = repeated_count - 2
                collected_evidence["loop_gate"] = {
                    "reason": "repeated_progress_signature",
                    "progress_signature": progress_signature,
                    "repeat_count": repeated_count,
                    "warning_count": warning_count,
                }
                if warning_count >= 2:
                    self._discard_shadow_if_needed(spec, state.run_id, shadow_workspace)
                    state = self._transition(
                        state,
                        node=LoopNode.EXECUTE,
                        condition="repeated_progress_signature",
                        terminal_state=LoopTerminalState.BLOCKED,
                        evidence={
                            **collected_evidence["loop_gate"],
                            "tool": planned_step.tool,
                        },
                    )
                    self.gateway.release(grant_id=grant.grant_id)
                    return StateGraphRunResult(
                        run_state=state,
                        resource_grants=tuple(grants),
                        evidence=collected_evidence,
                    )
            if executed.yields_control:
                if executed.action in {"ask", "connector_outbound"}:
                    state = self._transition(
                        state,
                        node=LoopNode.PAUSE,
                        condition="resource_pause",
                        evidence=execution_evidence,
                    )
                    state = self._transition(
                        state,
                        node=LoopNode.PAUSE,
                        condition="resource_or_user_pause",
                        terminal_state=LoopTerminalState.PAUSED,
                        evidence=execution_evidence,
                    )
                else:
                    state = self._transition(
                        state,
                        node=LoopNode.ESCALATE,
                        condition="approval_required",
                        evidence=execution_evidence,
                    )
                    state = self._transition(
                        state,
                        node=LoopNode.ESCALATE,
                        condition="approval_required",
                        terminal_state=LoopTerminalState.WAITING_APPROVAL,
                        evidence=execution_evidence,
                    )
                self.gateway.release(grant_id=grant.grant_id)
                return StateGraphRunResult(
                    run_state=state,
                    resource_grants=tuple(grants),
                    evidence=collected_evidence,
                )
            if not executed.ok:
                self._discard_shadow_if_needed(spec, state.run_id, shadow_workspace)
                state = self._transition(
                    state,
                    node=LoopNode.REFLECT,
                    condition="capability_failed",
                    evidence=execution_evidence,
                )
                decision = self.recovery_port.recover(spec, state, executed=executed)
                collected_evidence["reflection"] = decision.to_dict()
                if decision.replan_allowed:
                    state = self._transition(
                        state,
                        node=LoopNode.PLAN,
                        condition="new_route_available",
                        evidence=decision.to_dict(),
                    )
                    self.gateway.release(grant_id=grant.grant_id)
                    return StateGraphRunResult(
                        run_state=state,
                        resource_grants=tuple(grants),
                        harness_results=tuple(harness_results),
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
                    self.gateway.release(grant_id=grant.grant_id)
                    return StateGraphRunResult(
                        run_state=state,
                        resource_grants=tuple(grants),
                        evidence=collected_evidence,
                    )
            state = self._transition(
                state,
                node=LoopNode.EVALUATE,
                condition="side_effect_recorded",
                evidence={
                    "executor": collected_evidence["capability_result"],
                    "progress_signature": progress_signature,
                },
            )
            self.gateway.release(grant_id=grant.grant_id)

        if state.node == LoopNode.EVALUATE:
            try:
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
            except ResourceLimitError as exc:
                grants.append(exc.grant)
                self._record_gate_decision(
                    state=state,
                    kind="llm:semantic_checker",
                    grant=exc.grant,
                )
                return StateGraphRunResult(
                    run_state=self._stop_for_resource_grant(state, exc.grant),
                    resource_grants=tuple(grants),
                    harness_results=tuple(harness_results),
                    evidence=collected_evidence,
                )
            except SemanticCheckerCallError as exc:
                failure_facts = provider_failure_facts(exc.cause)
                if not bool(failure_facts.get("provider_call_failure", False)):
                    self._discard_shadow_if_needed(
                        spec,
                        state.run_id,
                        shadow_workspace,
                    )
                    raise exc.cause
                retry_gate = _next_provider_transport_retry_gate_for_facts(
                    failure_facts,
                    evidence=collected_evidence,
                    model_role="checker",
                    resume_node=LoopNode.EVALUATE,
                )
                if retry_gate is not None:
                    retry_evidence = {
                        "checker_provider_failure": failure_facts,
                        "automatic_model_retry": False,
                        "durable_transport_retry": True,
                        "retry_gate": retry_gate,
                        "executor": collected_evidence.get("capability_result", {}),
                    }
                    collected_evidence.update(retry_evidence)
                    return StateGraphRunResult(
                        run_state=self._pause_for_provider_transport_retry(
                            state,
                            evidence=retry_evidence,
                        ),
                        resource_grants=tuple(grants),
                        harness_results=tuple(harness_results),
                        evidence=collected_evidence,
                    )
                failure_evidence = {
                    "checker_provider_failure": failure_facts,
                    "automatic_model_retry": False,
                    "durable_transport_retry": bool(
                        collected_evidence.get("retry_gate")
                    ),
                    "transport_retry_exhausted": bool(
                        failure_facts.get("retryable", False)
                        and collected_evidence.get("retry_gate")
                    ),
                }
                collected_evidence.update(failure_evidence)
                reflected = self._transition(
                    state,
                    node=LoopNode.REFLECT,
                    condition="checker_failed",
                    evidence=failure_evidence,
                )
                failed = self._transition(
                    reflected,
                    node=LoopNode.REFLECT,
                    condition="checker_rejected",
                    terminal_state=LoopTerminalState.FAILED,
                    evidence=failure_evidence,
                )
                self._discard_shadow_if_needed(spec, state.run_id, shadow_workspace)
                return StateGraphRunResult(
                    run_state=failed,
                    resource_grants=tuple(grants),
                    harness_results=tuple(harness_results),
                    evidence=collected_evidence,
                )
            except Exception:
                self._discard_shadow_if_needed(spec, state.run_id, shadow_workspace)
                raise
            return result

        return StateGraphRunResult(
            run_state=state,
            checker_report=checker_report,
            resource_grants=tuple(grants),
            harness_results=tuple(harness_results),
            evidence=collected_evidence,
        )

    def _planned_step_from_checkpoint(self, run_id: str) -> PlannedCapabilityStep | None:
        checkpoint = self.store.latest_checkpoint(
            run_id,
            node=LoopNode.EXECUTE,
            input_key="planned_capability",
        )
        if checkpoint is None:
            return None
        inputs = json.loads(checkpoint.inputs_json or "{}")
        raw = inputs.get("planned_capability") if isinstance(inputs, dict) else None
        return self._planned_step_from_raw(raw)

    @staticmethod
    def _planned_step_from_raw(raw: Any) -> PlannedCapabilityStep | None:
        if not isinstance(raw, dict):
            return None
        tool = raw.get("tool")
        args = raw.get("args")
        permission = raw.get("permission")
        if (
            not isinstance(tool, str)
            or not tool.strip()
            or not isinstance(args, dict)
            or not isinstance(permission, str)
            or not permission.strip()
        ):
            return None
        return PlannedCapabilityStep(
            tool=tool.strip(),
            args=dict(args),
            permission=permission.strip(),
            reason=str(raw.get("reason") or ""),
            used_memory_ids=tuple(str(item) for item in raw.get("used_memory_ids") or ()),
            used_evidence_ids=tuple(str(item) for item in raw.get("used_evidence_ids") or ()),
            memory_activation=dict(raw.get("memory_activation") or {}),
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
        gateway = self.gateway
        if gateway is None:
            raise RuntimeError("resource gateway is not initialized")
        state, stopped, grant = self._gate_or_stop(state, kind="state_graph.evaluate")
        grants.append(grant)
        if stopped:
            return StateGraphRunResult(
                run_state=state,
                resource_grants=tuple(grants),
                evidence=collected_evidence,
            )
        gateway.release(grant_id=grant.grant_id)
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
                    gateway.release(grant_id=command_grant.grant_id)
                    return StateGraphRunResult(
                        run_state=state,
                        resource_grants=tuple(grants),
                        harness_results=tuple(harness_results),
                        evidence=collected_evidence,
                    )
            try:
                execution_command = _execution_command_for_workspace(
                    step.command,
                    logical_workspace=_scope_workspace_for_spec(
                        spec,
                        default_workspace=workspace,
                    ),
                    execution_workspace=execution_workspace,
                )
                result = self.harness.run_command(
                    HarnessCommand(
                        command=tuple(shlex.split(execution_command)),
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
                gateway.release(grant_id=command_grant.grant_id)
            harness_results.append(result)
            collected_evidence[step.evidence_key or step.name] = result.to_facts()

        await self._populate_semantic_checker_evidence(
            spec,
            state,
            collected_evidence=collected_evidence,
        )
        _clear_provider_transport_retry_gate(
            collected_evidence,
            model_role="checker",
        )
        self._pending_transition_evidence.update(
            _provider_transport_retry_clearance_evidence(collected_evidence)
        )
        checker_report = self.checker.evaluate(spec, collected_evidence)
        cap_result = collected_evidence.get("capability_result", {})
        is_terminal = isinstance(cap_result, dict) and bool(cap_result.get("terminal", False))
        if checker_report.accepted:
            candidate_response = str(collected_evidence.pop("candidate_response", "") or "")
            candidate_action = str(
                collected_evidence.pop("candidate_response_action", "") or "chat"
            )
            if candidate_response:
                collected_evidence["responded_message"] = candidate_response
                collected_evidence["responded_action"] = candidate_action
            if (
                _surface_response_required(
                    spec,
                    CapabilityRegistry(
                        home=self.home,
                        project_dir=workspace,
                    ),
                )
                and not _has_surface_result(collected_evidence)
                and not is_terminal
                and state.attempt < spec.retry_policy.max_attempts
            ):
                collected_evidence["surface_response"] = {
                    "required": True,
                    "verified_work_complete": True,
                    "authority": "semantic_checker",
                    "presentation_missing": True,
                    "completed_effect_replay_prohibited": True,
                }
                state = self._transition(
                    state,
                    node=LoopNode.PLAN,
                    condition="continue_iteration",
                    evidence={
                        "reason": "verified_facts_require_user_facing_result",
                        "attempt": state.attempt,
                    },
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
                evidence={
                    **checker_report.to_dict(),
                    # Recovery remains available in attempt history and trace
                    # events. It is no longer the current state once a later
                    # attempt converges.
                    "reason_code": "",
                    "reason": "",
                    "repeat_count": 0,
                    "replan_allowed": False,
                    "facts": {},
                },
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
        if decision.replan_allowed:
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
            deterministic_completion_authority=bool(
                cap_result.get("deterministic_completion_authority", False)
            ),
            mutates=bool(cap_result.get("mutates", False)),
        )
        execution_profile = spec.goal.metadata.get("execution_profile")
        checker_tier = (
            str(execution_profile.get("checker_tier") or "")
            if isinstance(execution_profile, dict)
            else ""
        )
        for step in spec.verification_ladder:
            if step.kind != VerificationKind.LLM_CHECKER:
                continue
            key = step.evidence_key or step.name

            completion_evidence = executed_step.facts.get("completion_evidence") is True
            if (
                checker_tier == "objective_evidence"
                and executed_step.ok
                and completion_evidence
                and executed_step.deterministic_completion_authority
            ):
                facts = {
                    "passed": True,
                    "ok": True,
                    "evidence_summary": "capability returned objective completion evidence",
                    "evaluator_role": "deterministic_evidence",
                    "isolated_context": True,
                    "attempt": state.attempt,
                }
                collected_evidence[key] = facts
                collected_evidence["semantic_checker_result"] = facts
                continue

            try:
                decision = await self.semantic_checker_port.assess(
                    spec,
                    state,
                    executed=executed_step,
                    evidence=collected_evidence,
                )
            except Exception as exc:
                raise SemanticCheckerCallError(exc) from exc

            facts = {**decision.to_facts(), "attempt": state.attempt}
            collected_evidence[key] = facts
            collected_evidence["semantic_checker_result"] = facts

    def _gate_or_stop(
        self,
        state: LoopRunState,
        *,
        kind: str,
        request: ResourceRequest | None = None,
    ) -> tuple[LoopRunState, bool, ResourceGrant]:
        if self.gateway is None:
            raise RuntimeError("StateGraph resource gateway is not initialized")
        grant = self.gateway.request(
            request
            or ResourceRequest(
                kind=kind,
                estimated_tokens=_default_phase_tokens(self.gateway.limits)
                if self._account_phase_gates
                else 0,
                estimated_cost=_default_phase_cost(self.gateway.limits)
                if self._account_phase_gates
                else 0.0,
                units=1,
                reserve=self._account_phase_gates,
            )
        )
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
        evidence = {
            "resource_grant": grant.to_dict(),
            "resource_resume_node": str(state.node),
        }
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

    def _pause_for_provider_transport_retry(
        self,
        state: LoopRunState,
        *,
        evidence: dict[str, Any],
    ) -> LoopRunState:
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
        if self._lease_renewal_error is not None:
            raise self._lease_renewal_error
        checkpoint = self._checkpoint(
            state,
            inputs={
                "target_node": str(node),
                "condition": condition,
                "terminal_state": str(terminal_state),
            },
        )
        transition_evidence = {
            **self._pending_transition_evidence,
            **dict(evidence or {}),
        }
        transitioned = self.store.transition(
            state.run_id,
            node=node,
            checkpoint_id=checkpoint.id,
            terminal_state=terminal_state,
            condition=condition,
            evidence=transition_evidence,
            lease_owner=self.execution_owner,
        )
        self._pending_transition_evidence.clear()
        self._record_transition_decision(
            from_state=state,
            to_state=transitioned,
            checkpoint_id=checkpoint.id,
            condition=condition,
            terminal_state=terminal_state,
            evidence=transition_evidence,
        )
        return transitioned

    def _start_execution_lease_heartbeat(
        self,
        *,
        run_id: str,
        lease_seconds: float,
    ) -> None:
        if self._lease_heartbeat_task is not None:
            return
        self._lease_renewal_error = None
        self._lease_heartbeat_task = asyncio.create_task(
            self._renew_execution_lease(
                run_id=run_id,
                lease_seconds=lease_seconds,
            )
        )

    async def _renew_execution_lease(
        self,
        *,
        run_id: str,
        lease_seconds: float,
    ) -> None:
        interval = max(
            0.05,
            min(EXECUTION_LEASE_HEARTBEAT_MAX_SECONDS, lease_seconds / 3.0),
        )
        try:
            while True:
                await asyncio.sleep(interval)
                renewed = await asyncio.to_thread(
                    self.store.renew_execution_lease,
                    run_id,
                    owner=self.execution_owner,
                    lease_seconds=lease_seconds,
                )
                if not renewed:
                    current = await asyncio.to_thread(self.store.get_run, run_id)
                    if current is not None and current.is_stopped():
                        return
                    self._lease_renewal_error = RuntimeError(
                        "loop execution lease could not be renewed by this driver"
                    )
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # renewal must fail closed on any driver error
            self._lease_renewal_error = RuntimeError(
                f"loop execution lease renewal failed: {type(exc).__name__}: {exc}"
            )

    async def _stop_execution_lease_heartbeat(self) -> None:
        task = self._lease_heartbeat_task
        self._lease_heartbeat_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

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
        passed=_transition_check_passed(condition, decision),
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
        str(LoopCheckName.EXTERNAL_PAUSE),
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
        progress_signature=str(
            evidence.get("progress_signature") or to_state.evidence.get("progress_signature") or ""
        ),
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
    is_external_pause = grant.decision == ResourceDecision.PAUSE
    check = LoopCheckResult(
        name=(LoopCheckName.EXTERNAL_PAUSE if is_external_pause else LoopCheckName.APPROVAL_GATE),
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
        else (LoopReason.EXTERNAL_PAUSE if is_external_pause else LoopReason.APPROVAL_REQUIRED),
        phase=LoopPhase.DECISION,
        failure_domain=TraceFailureDomain.NONE
        if grant.allowed or is_external_pause
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


def _default_phase_tokens(limits: ResourceLimits) -> int:
    if limits.token_budget <= 0:
        return 0
    divisor = limits.call_budget if limits.call_budget > 0 else 1
    return max(1, limits.token_budget // divisor)


def _default_phase_cost(limits: ResourceLimits) -> float:
    if limits.cost_budget <= 0:
        return 0.0
    divisor = limits.call_budget if limits.call_budget > 0 else 1
    return limits.cost_budget / divisor


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
    if condition == "resource_pause":
        return LoopDecisionKind.CONTINUE
    if condition in {
        "planner_failed",
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
    if condition == "repeated_progress_signature":
        return LoopReason.REPEATED_PROGRESS_SIGNATURE
    if condition in {"checker_failed", "checker_rejected", "no_route_available"}:
        return LoopReason.COMPLETION_CHECKER_BLOCKED
    if condition in {"resource_pause", "resource_or_user_pause"}:
        return LoopReason.EXTERNAL_PAUSE
    if condition in {
        "approval_required",
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
    if condition == "repeated_progress_signature":
        return TraceFailureDomain.LOOP_NO_PROGRESS
    if condition in {"checker_failed", "checker_rejected", "no_route_available"}:
        return TraceFailureDomain.CHECKER_BLOCKED
    if condition in {"resource_pause", "resource_or_user_pause"} or terminal == str(
        LoopTerminalState.PAUSED
    ):
        return TraceFailureDomain.NONE
    if (
        condition in {"resource_escalate", "resource_blocked"}
        or condition == "side_effect_commit_required"
        or terminal
        in {
            str(LoopTerminalState.WAITING_APPROVAL),
        }
    ):
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
    if condition == "repeated_progress_signature":
        return LoopCheckName.NO_PROGRESS_GATE
    if condition in {"resource_pause", "resource_or_user_pause"}:
        return LoopCheckName.EXTERNAL_PAUSE
    if condition.startswith("resource_") or condition == "side_effect_commit_required":
        return LoopCheckName.APPROVAL_GATE
    if terminal_state:
        return LoopCheckName.TERMINAL_RESULT
    return LoopCheckName.COMPLETION_CHECKER


def _transition_check_passed(condition: str, decision: LoopDecisionKind | str) -> bool:
    if condition in {"resource_pause", "resource_or_user_pause"}:
        return True
    return str(decision) not in {
        str(LoopDecisionKind.BLOCKED),
        str(LoopDecisionKind.FAILED),
    }


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
    has_explicit_side_effect = (
        any(
            key in facts
            for key in (
                "side_effect_scope",
                "side_effect_state",
                "side_effect_artifact",
                "side_effect_commit",
                "side_effect_compensate",
            )
        )
        or str(executor.get("action") or "") == "connector_outbound"
    )
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
        "commit_strategy": str(facts.get("side_effect_commit_strategy") or ""),
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


def _scope_workspace_for_spec(spec: LoopSpec, *, default_workspace: Path) -> str:
    workspace = str(spec.goal.metadata.get("workspace") or "").strip()
    return workspace or str(default_workspace)


def _execution_args_for_workspace(
    tool_spec: Any,
    args: dict[str, Any],
    *,
    logical_workspace: str,
    execution_workspace: Path,
) -> dict[str, Any]:
    """Map declared real-workspace paths into the active shadow workspace."""

    translated = dict(args)
    if tool_spec is None or tool_spec.workspace_scope == "context" or not logical_workspace:
        return translated
    logical_root = Path(logical_workspace).expanduser().resolve()
    execution_root = execution_workspace.expanduser().resolve()
    if logical_root == execution_root:
        return translated

    def translate(value: Any) -> Any:
        text = str(value or "").strip()
        if not text:
            return value
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            return value
        resolved = candidate.resolve()
        if resolved == logical_root:
            return str(execution_root)
        if logical_root in resolved.parents:
            return str(execution_root / resolved.relative_to(logical_root))
        return value

    for path_field in tool_spec.workspace_fields:
        if path_field in translated:
            translated[path_field] = translate(translated[path_field])
    if tool_spec.workspace_policy == "sandbox" and isinstance(translated.get("command"), list):
        translated["command"] = [translate(item) for item in translated["command"]]
    return translated


def _execution_command_for_workspace(
    command: str,
    *,
    logical_workspace: str,
    execution_workspace: Path,
) -> str:
    """Translate logical workspace paths embedded in verifier arguments."""

    if not logical_workspace:
        return command
    logical_root = Path(logical_workspace).expanduser().resolve()
    execution_root = execution_workspace.expanduser().resolve()
    if logical_root == execution_root:
        return command

    logical_text = str(logical_root)
    execution_text = str(execution_root)
    translated: list[str] = []
    for argument in shlex.split(command):
        start = 0
        pieces: list[str] = []
        while True:
            index = argument.find(logical_text, start)
            if index < 0:
                pieces.append(argument[start:])
                break
            boundary = index + len(logical_text)
            if boundary < len(argument) and argument[boundary] != "/":
                pieces.append(argument[start:boundary])
                start = boundary
                continue
            pieces.append(argument[start:index])
            pieces.append(execution_text)
            start = boundary
        translated.append("".join(pieces))
    return shlex.join(translated)


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


def _durable_constraints(runtime: AgentRuntime, spec: LoopSpec) -> str:
    parts = [_goal_constraints(spec)]
    rendered = runtime.memory.render_durable_constraints(
        allowed_scopes=_memory_scopes_for_spec(spec, home=runtime.home),
    ).strip()
    if rendered:
        parts.extend(["Durable state constraints:", rendered])
    return "\n".join(parts)


def _memory_scopes_for_spec(spec: LoopSpec, *, home: Path | None = None) -> set[str]:
    from .memory.scopes import memory_scopes_for_context

    metadata = spec.goal.metadata
    return set(
        memory_scopes_for_context(
            source=str(metadata.get("source") or ""),
            peer_id=str(metadata.get("peer_id") or ""),
            sender_id=str(metadata.get("sender_id") or ""),
            session_id=str(metadata.get("session_id") or ""),
            workspace=str(metadata.get("workspace") or ""),
            home=home,
        )
    )


def _refreshed_ingress_facts(context: CapabilityContext) -> dict[str, Any]:
    facts = dict(context.runtime_facts or {})
    intent_facts = facts.get("intent_facts")
    if isinstance(intent_facts, dict) and "current_state" in intent_facts:
        # Intent intake and planner execution happen at different times. Keep
        # intent provenance, but expose only the freshly rebuilt state below.
        facts["intent_facts"] = {
            key: value for key, value in intent_facts.items() if key != "current_state"
        }
    surface = SurfaceContext(
        home=context.home,
        source=context.source,
        peer_id=context.peer_id,
        sender_id=context.sender_id,
        session_id=context.session_id,
        workspace=context.workspace,
        input_text=context.input_text,
    )
    facts["current_state"] = current_state_facts(CurrentStateBuilder(context.home).build(surface))
    if context.goal_id:
        from .goals import GoalStore

        events = GoalStore(context.home).list_events(context.goal_id, limit=1000)
        inbox: list[dict[str, Any]] = []
        for event in events:
            if event.event_type != "agent.message_received":
                continue
            try:
                payload = json.loads(event.evidence_json or "{}")
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, dict):
                inbox.append(
                    {
                        "message_id": event.id,
                        "created_at": event.created_at,
                        **payload,
                    }
                )
        if inbox:
            facts["agent_inbox"] = inbox[-20:]
    return facts


def _planner_ingress_facts(context: CapabilityContext, spec: LoopSpec) -> dict[str, Any]:
    facts = _refreshed_ingress_facts(context)
    task_context = _goal_task_context(spec)
    facts["task_context"] = _planner_task_context(task_context)
    facts["current_state"] = _project_current_state_for_task(
        facts.get("current_state"),
        task_context,
    )
    return facts


def _planner_task_context(task_context: dict[str, Any]) -> dict[str, Any]:
    """Project task lineage once without duplicating delivery and result bodies."""
    lineage = task_context.get("lineage")
    progress = task_context.get("progress")
    delivery = task_context.get("delivery")
    lineage_facts = dict(lineage) if isinstance(lineage, dict) else {}
    progress_facts = dict(progress) if isinstance(progress, dict) else {}
    delivery_facts = dict(delivery) if isinstance(delivery, dict) else {}
    prior_items: list[dict[str, Any]] = []
    for item in progress_facts.get("authoritative_prior_items") or []:
        if not isinstance(item, dict):
            continue
        prior = _prior_result_text(item)
        result_text = prior["text"]
        prior_items.append(
            {
                "goal_id": str(item.get("goal_id") or ""),
                "run_id": str(item.get("run_id") or ""),
                "result_source": prior["source"],
                "result_text": truncate_middle(
                    result_text,
                    PLANNER_PRIOR_RESULT_MAX_CHARS,
                ),
                "result_characters": len(result_text),
                "result_truncated": len(result_text) > PLANNER_PRIOR_RESULT_MAX_CHARS,
            }
        )
    projected = {
        "lineage": lineage_facts,
        "progress": {
            "scope": str(progress_facts.get("scope") or ""),
            "sequence_number": _int_or_default(progress_facts.get("sequence_number"), 0),
            "authority": str(progress_facts.get("authority") or ""),
            "authoritative_prior_items": prior_items,
            "authoritative_prior_item_count": len(prior_items),
            "ambient_history_authoritative": bool(
                progress_facts.get("ambient_history_authoritative", False)
            ),
        },
        "delivery": delivery_facts,
    }
    bounded = project_model_facts(
        projected,
        max_characters=(
            PLANNER_PRIOR_RESULT_MAX_CHARS * max(1, len(prior_items)) + 4_000
        ),
        max_string_characters=PLANNER_PRIOR_RESULT_MAX_CHARS,
        max_depth=6,
        max_items=30,
    )
    return bounded if isinstance(bounded, dict) else {}


def _project_current_state_for_task(
    current_state: Any,
    task_context: dict[str, Any],
) -> Any:
    if not isinstance(current_state, dict):
        return current_state
    progress = task_context.get("progress")
    if isinstance(progress, dict) and progress.get("ambient_history_authoritative"):
        return dict(current_state)
    lineage_ids = _task_context_lineage_ids(task_context)
    if not lineage_ids:
        return dict(current_state)
    projected = dict(current_state)
    recent_outcomes, ambient_outcomes = _split_task_records(
        projected.get("recent_goal_outcomes"),
        lineage_ids,
    )
    projected["recent_goal_outcomes"] = recent_outcomes
    _append_ambient_records(
        projected,
        "ambient_goal_outcomes",
        [_ambient_goal_outcome(item) for item in ambient_outcomes],
    )

    active_goals, ambient_goals = _split_task_records(
        projected.get("active_goals"),
        lineage_ids,
    )
    task_run_ids = _task_run_ids(task_context, active_goals, recent_outcomes)
    active_runs, ambient_runs = _split_task_run_records(
        projected.get("active_runs"),
        task_run_ids,
    )
    projected["active_runs"] = active_runs
    _append_ambient_records(
        projected,
        "ambient_active_runs",
        [_ambient_run(item) for item in ambient_runs],
    )
    anomalies = projected.get("runtime_state_anomalies")
    if isinstance(anomalies, dict):
        projected_anomalies = dict(anomalies)
        orphan_runs, ambient_orphan_runs = _split_task_run_records(
            projected_anomalies.get("active_runs_without_active_goals"),
            task_run_ids,
        )
        projected_anomalies["active_runs_without_active_goals"] = orphan_runs
        projected_anomalies["active_run_without_active_goal_count"] = len(orphan_runs)
        _append_ambient_records(
            projected_anomalies,
            "ambient_active_runs_without_active_goals",
            [_ambient_run(item) for item in ambient_orphan_runs],
        )
        if ambient_orphan_runs:
            projected_anomalies["ambient_active_run_without_active_goal_count"] = len(
                ambient_orphan_runs
            )
        projected["runtime_state_anomalies"] = projected_anomalies
    projected["active_goals"] = active_goals
    _append_ambient_records(
        projected,
        "ambient_active_goals",
        [_ambient_goal(item) for item in ambient_goals],
    )
    goal_state = projected.get("goal_state")
    if isinstance(goal_state, dict):
        projected_goal_state = dict(goal_state)
        state_goals, state_ambient_goals = _split_task_records(
            projected_goal_state.get("active_goals"),
            lineage_ids,
        )
        projected_goal_state["active_goals"] = state_goals
        _append_ambient_records(
            projected_goal_state,
            "ambient_active_goals",
            [_ambient_goal(item) for item in state_ambient_goals],
        )
        projected["goal_state"] = projected_goal_state

    active_loop_runs, ambient_loop_runs = _split_task_records(
        projected.get("active_loop_runs"),
        lineage_ids,
    )
    projected["active_loop_runs"] = active_loop_runs
    _append_ambient_records(
        projected,
        "ambient_active_loop_runs",
        [_ambient_loop_run(item) for item in ambient_loop_runs],
    )
    loop_run_state = projected.get("loop_run_state")
    if isinstance(loop_run_state, dict):
        projected_loop_state = dict(loop_run_state)
        state_loop_runs, state_ambient_loop_runs = _split_task_records(
            projected_loop_state.get("active_loop_runs"),
            lineage_ids,
        )
        projected_loop_state["active_loop_runs"] = state_loop_runs
        _append_ambient_records(
            projected_loop_state,
            "ambient_active_loop_runs",
            [_ambient_loop_run(item) for item in state_ambient_loop_runs],
        )
        projected["loop_run_state"] = projected_loop_state

    recent_deliveries, ambient_deliveries = _split_task_records(
        projected.get("recent_deliveries"),
        lineage_ids,
    )
    projected["recent_deliveries"] = recent_deliveries
    _append_ambient_records(
        projected,
        "ambient_recent_deliveries",
        [_ambient_delivery(item) for item in ambient_deliveries],
    )
    lineage = task_context.get("lineage") if isinstance(task_context, dict) else {}
    projected["task_projection_policy"] = {
        "progress_scope": str(progress.get("scope") or "") if isinstance(progress, dict) else "",
        "lineage_id": str(lineage.get("id") or "") if isinstance(lineage, dict) else "",
        "current_goal_id": str(lineage.get("current_goal_id") or "")
        if isinstance(lineage, dict)
        else "",
        "ambient_history_authoritative": False,
        "ambient_goal_outcome_count": len(ambient_outcomes),
        "ambient_active_run_count": len(ambient_runs),
        "ambient_active_goal_count": len(ambient_goals),
        "ambient_active_loop_run_count": len(ambient_loop_runs),
        "ambient_recent_delivery_count": len(ambient_deliveries),
        "ambient_record_limit": PLANNER_AMBIENT_RECORD_LIMIT,
    }
    return projected


def _task_context_lineage_ids(task_context: dict[str, Any]) -> set[str]:
    lineage = task_context.get("lineage") if isinstance(task_context, dict) else {}
    if not isinstance(lineage, dict):
        return set()
    return {
        value
        for value in (
            str(lineage.get("id") or ""),
            str(lineage.get("current_goal_id") or ""),
            str(lineage.get("parent_goal_id") or ""),
        )
        if value
    }


def _split_task_records(
    records: Any,
    lineage_ids: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    matching: list[dict[str, Any]] = []
    ambient: list[dict[str, Any]] = []
    if not isinstance(records, list):
        return matching, ambient
    for item in records:
        if not isinstance(item, dict):
            continue
        copied = dict(item)
        if _record_matches_task_lineage(copied, lineage_ids):
            matching.append(copied)
        else:
            ambient.append(copied)
    return matching, ambient


def _record_matches_task_lineage(record: dict[str, Any], lineage_ids: set[str]) -> bool:
    return any(
        str(record.get(field) or "") in lineage_ids for field in ("id", "goal_id", "parent_goal_id")
    )


def _task_run_ids(
    task_context: dict[str, Any],
    *record_groups: list[dict[str, Any]],
) -> set[str]:
    run_ids = {
        str(record.get("run_id") or "")
        for records in record_groups
        for record in records
        if str(record.get("run_id") or "")
    }
    progress = task_context.get("progress") if isinstance(task_context, dict) else {}
    prior_items = progress.get("authoritative_prior_items") if isinstance(progress, dict) else []
    if isinstance(prior_items, list):
        run_ids.update(
            str(item.get("run_id") or "")
            for item in prior_items
            if isinstance(item, dict) and str(item.get("run_id") or "")
        )
    return run_ids


def _split_task_run_records(
    records: Any,
    run_ids: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    matching: list[dict[str, Any]] = []
    ambient: list[dict[str, Any]] = []
    if not isinstance(records, list):
        return matching, ambient
    for item in records:
        if not isinstance(item, dict):
            continue
        copied = dict(item)
        if _record_matches_task_run(copied, run_ids):
            matching.append(copied)
        else:
            ambient.append(copied)
    return matching, ambient


def _record_matches_task_run(record: dict[str, Any], run_ids: set[str]) -> bool:
    return any(str(record.get(field) or "") in run_ids for field in ("id", "run_id"))


def _append_ambient_records(
    container: dict[str, Any],
    key: str,
    records: list[dict[str, Any]],
) -> None:
    if not records:
        return
    records = records[:PLANNER_AMBIENT_RECORD_LIMIT]
    existing = container.get(key)
    if isinstance(existing, list):
        container[key] = [*existing, *records][:PLANNER_AMBIENT_RECORD_LIMIT]
    else:
        container[key] = records


def _ambient_goal_outcome(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "goal_id": str(record.get("goal_id") or ""),
        "parent_goal_id": str(record.get("parent_goal_id") or ""),
        "run_id": str(record.get("run_id") or ""),
        "phase": str(record.get("phase") or ""),
        "resolution": str(record.get("resolution") or ""),
        "task_status": str(record.get("task_status") or ""),
        "loop_terminal_state": str(record.get("loop_terminal_state") or ""),
        "updated_at": record.get("updated_at", 0),
        "objective_omitted": True,
        "result_summary_omitted": True,
        "progress_authority": "ambient_not_authoritative_for_current_task",
    }


def _ambient_run(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(record.get("id") or record.get("run_id") or ""),
        "phase": str(record.get("phase") or ""),
        "acceptance": str(record.get("acceptance") or ""),
        "resolution": str(record.get("resolution") or ""),
        "kind": str(record.get("kind") or ""),
        "updated_at": record.get("updated_at", 0),
        "title_omitted": True,
        "result_summary_omitted": True,
        "progress_authority": "ambient_not_authoritative_for_current_task",
    }


def _ambient_goal(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(record.get("id") or ""),
        "parent_goal_id": str(record.get("parent_goal_id") or ""),
        "run_id": str(record.get("run_id") or ""),
        "phase": str(record.get("phase") or ""),
        "task_status": str(record.get("task_status") or ""),
        "updated_at": record.get("updated_at", 0),
        "objective_omitted": True,
        "progress_authority": "ambient_not_authoritative_for_current_task",
    }


def _ambient_loop_run(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": str(record.get("run_id") or ""),
        "goal_id": str(record.get("goal_id") or ""),
        "loop_spec_id": str(record.get("loop_spec_id") or ""),
        "node": str(record.get("node") or ""),
        "terminal_state": str(record.get("terminal_state") or ""),
        "attempt": _int_or_default(record.get("attempt"), 0),
        "updated_at": record.get("updated_at", 0),
        "progress_authority": "ambient_not_authoritative_for_current_task",
    }


def _ambient_delivery(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "state_transition": str(record.get("state_transition") or ""),
        "channel": str(record.get("channel") or ""),
        "goal_id": str(record.get("goal_id") or ""),
        "run_id": str(record.get("run_id") or ""),
        "delivery_id": str(record.get("delivery_id") or ""),
        "sent_at": record.get("sent_at", 0),
        "recorded_at": record.get("recorded_at", 0),
        "text_length": _int_or_default(record.get("text_length"), 0),
        "media_count": _int_or_default(record.get("media_count"), 0),
        "text_preview_omitted": True,
        "progress_authority": "ambient_not_authoritative_for_current_task",
    }


def _planner_loop_spec_facts(spec: LoopSpec) -> dict[str, Any]:
    """Expose semantic loop contracts without serializing the runtime graph."""
    goal = spec.goal.to_dict()
    metadata = goal.get("metadata")
    if isinstance(metadata, dict):
        metadata = dict(metadata)
        # Task context has a dedicated planner projection. Scheduled prior
        # occurrences are the durable source used to build that context; do
        # not serialize either copy again through Goal metadata.
        metadata.pop("task_context", None)
        trigger_facts = metadata.get("trigger_facts")
        if isinstance(trigger_facts, dict):
            trigger_facts = dict(trigger_facts)
            trigger_facts.pop("prior_occurrences", None)
            metadata["trigger_facts"] = trigger_facts
        goal["metadata"] = metadata
    return {
        "id": spec.id,
        "goal_id": spec.goal_id,
        "goal": goal,
        "allowed_capabilities": list(spec.allowed_capabilities),
        "verification_ladder": [item.to_dict() for item in spec.verification_ladder],
        "retry_policy": spec.retry_policy.to_dict(),
        "budget_policy": spec.budget_policy.to_dict(),
    }


def _planner_loop_run_facts(state: LoopRunState) -> dict[str, Any]:
    facts = state.to_dict()
    evidence = facts.pop("evidence", {})
    facts["evidence_keys"] = sorted(evidence) if isinstance(evidence, dict) else []
    return facts


def _planner_objective_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    # Attempt history is exposed once, in compact form.  Keep durable evidence
    # intact, but give the planner a bounded projection so one file read cannot
    # crowd out the task, checker facts, and tool contract.
    projected = project_model_facts(
        {key: value for key, value in evidence.items() if key != "attempt_history"},
        max_characters=24_000,
    )
    return projected if isinstance(projected, dict) else {}


def _planner_verification_failure_text(evidence: dict[str, Any]) -> str:
    """Surface the last checker rejection as a prominent, plain-text directive.

    Without this the rejection reason is buried ~7 levels deep inside the
    RUNTIME FACTS JSON (objective_evidence.reflection.facts.recovery
    .checker_report.checker_results[].evidence.evidence_summary) and the
    planner repeatedly re-attempts the same failed route. The full detail
    remains in RUNTIME FACTS; this block is the actionable headline.
    """
    reflection = evidence.get("reflection")
    if not isinstance(reflection, dict):
        return ""
    recovery = reflection.get("facts", {})
    if isinstance(recovery, dict):
        recovery = recovery.get("recovery")
    if not isinstance(recovery, dict):
        return ""
    checker_report = recovery.get("checker_report")
    if not isinstance(checker_report, dict) or checker_report.get("accepted"):
        return ""
    failed = [
        item
        for item in checker_report.get("checker_results") or []
        if isinstance(item, dict) and not item.get("passed")
    ]
    if not failed:
        return ""
    attempt = recovery.get("attempt")
    blocked = bool(checker_report.get("blocked"))
    lines: list[str] = []
    if attempt:
        lines.append(f"Attempt {attempt} was rejected by the verification checker.")
    else:
        lines.append("The previous attempt was rejected by the verification checker.")
    lines.append(
        "Do not repeat the same capability and arguments that produced this "
        "failure; the same route will be rejected again."
    )
    for item in failed:
        name = str(item.get("name") or "check")
        reason = str(item.get("reason") or "").strip()
        summary = str(
            (item.get("evidence") or {}).get("evidence_summary") or ""
        ).strip()
        lines.append("")
        lines.append(f"Rejected check: {name}")
        if reason:
            lines.append(f"Reason: {reason}")
        if blocked:
            lines.append("Blocked: yes (no further route available)")
        if summary:
            lines.append(f"Checker findings: {summary}")
    text = "\n".join(lines)
    if len(text) > 1200:
        text = text[:1197] + "..."
    return text


def _planner_attempt_history(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    raw_history = evidence.get("attempt_history") or []
    if not isinstance(raw_history, list):
        return []
    for raw in raw_history[-PLANNER_ATTEMPT_HISTORY_LIMIT:]:
        if not isinstance(raw, dict):
            continue
        facts = raw.get("facts")
        message = str(raw.get("message") or "").strip()
        candidate_response = _is_candidate_response_attempt(raw)
        compact.append(
            {key: value for key, value in raw.items() if key not in {"facts", "message"}}
            | {
                "evidence_authority": (
                    "candidate_response_only"
                    if candidate_response
                    else "declared_capability_observation"
                ),
                "fact_keys": sorted(facts) if isinstance(facts, dict) else [],
                "facts": (
                    facts
                    if not candidate_response and isinstance(facts, dict)
                    else {}
                ),
                "message_present": bool(message),
                "message_preview": (
                    truncate_middle(message, PLANNER_ATTEMPT_MESSAGE_MAX_CHARS)
                    if candidate_response and message
                    else ""
                ),
            }
        )
    projected = project_model_facts(
        compact,
        max_characters=PLANNER_ATTEMPT_HISTORY_MAX_CHARS,
        max_string_characters=PLANNER_ATTEMPT_MESSAGE_MAX_CHARS,
        max_depth=6,
        max_items=30,
    )
    return projected if isinstance(projected, list) else []


def _is_candidate_response_attempt(raw: dict[str, Any]) -> bool:
    """Classify presentation results by the graph protocol, not a tool name."""
    return (
        str(raw.get("action") or "") == "chat"
        and bool(raw.get("terminal", False))
        and bool(str(raw.get("message") or "").strip())
    )


def _next_provider_transport_retry_gate_for_facts(
    failure_facts: dict[str, Any],
    *,
    evidence: dict[str, Any],
    model_role: str,
    resume_node: LoopNode,
) -> dict[str, Any] | None:
    if not bool(failure_facts.get("retryable", False)):
        return None
    prior = evidence.get("retry_gate")
    prior_count = 0
    if (
        isinstance(prior, dict)
        and prior.get("kind") == "provider_transport"
        and str(prior.get("model_role") or "") == model_role
    ):
        try:
            prior_count = max(0, int(prior.get("retry_count") or 0))
        except (TypeError, ValueError):
            prior_count = PROVIDER_TRANSPORT_MAX_RETRIES
    if prior_count >= PROVIDER_TRANSPORT_MAX_RETRIES:
        return None
    try:
        retry_after = float(failure_facts.get("retry_after_seconds") or 0.0)
    except (TypeError, ValueError):
        retry_after = 0.0
    retry_after = min(
        PROVIDER_TRANSPORT_RETRY_MAX_SECONDS,
        max(PROVIDER_TRANSPORT_RETRY_MIN_SECONDS, retry_after),
    )
    return {
        "decision": "pause",
        "kind": "provider_transport",
        "reason": "provider_transport_unavailable",
        "model_role": model_role,
        "retry_after_seconds": retry_after,
        "retry_count": prior_count + 1,
        "max_retries": PROVIDER_TRANSPORT_MAX_RETRIES,
        "resume_node": str(resume_node),
    }


def _clear_provider_transport_retry_gate(
    evidence: dict[str, Any],
    *,
    model_role: str,
) -> None:
    prior = evidence.get("retry_gate")
    if not (
        isinstance(prior, dict)
        and prior.get("kind") == "provider_transport"
        and str(prior.get("model_role") or "") == model_role
    ):
        return
    history = [
        dict(item)
        for item in evidence.get("provider_transport_retry_history") or ()
        if isinstance(item, dict)
    ]
    history.append({**prior, "outcome": "recovered"})
    evidence["provider_transport_retry_history"] = history[-10:]
    evidence["retry_gate"] = None


def _provider_transport_retry_clearance_evidence(
    evidence: dict[str, Any],
) -> dict[str, Any]:
    if "retry_gate" not in evidence or evidence.get("retry_gate") is not None:
        return {}
    history = evidence.get("provider_transport_retry_history")
    return {
        "retry_gate": None,
        "provider_transport_retry_history": list(history or ()),
    }


def _semantic_checker_capability_result(
    executed: ExecutedCapabilityStep,
) -> dict[str, Any]:
    from .safeguards import redact_secrets_deep

    # The deterministic completion-authority flag is an internal runtime gate.
    # It does not grade the provenance of ordinary capability facts and must
    # not bias the semantic checker into discarding them.
    checker_result = executed.to_dict()
    checker_result.pop("deterministic_completion_authority", None)
    redacted = redact_secrets_deep(checker_result)
    return redacted if isinstance(redacted, dict) else {}


def _semantic_checker_attempt_evidence(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    """Return bounded executed-capability facts without planner rationale or verdicts."""
    from .safeguards import redact_secrets, redact_secrets_deep

    history = evidence.get("attempt_history") or []
    if not isinstance(history, list):
        return []
    compact: list[dict[str, Any]] = []
    for raw in history[-SEMANTIC_CHECKER_ATTEMPT_LIMIT:]:
        if not isinstance(raw, dict):
            continue
        candidate_response = _is_candidate_response_attempt(raw)
        compact.append(
            {
                "attempt": raw.get("attempt"),
                "tool": str(raw.get("tool") or ""),
                "evidence_authority": (
                    "candidate_response_only"
                    if candidate_response
                    else "declared_capability_observation"
                ),
                "args_json": truncate_middle(
                    json.dumps(
                        redact_secrets_deep(
                            raw.get("args") if isinstance(raw.get("args"), dict) else {}
                        ),
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    ),
                    SEMANTIC_CHECKER_ARGS_MAX_CHARS,
                ),
                "ok": bool(raw.get("ok", False)),
                "action": str(raw.get("action") or ""),
                "facts_json": truncate_middle(
                    json.dumps(
                        redact_secrets_deep(
                            raw.get("facts") if isinstance(raw.get("facts"), dict) else {}
                        ),
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    ),
                    SEMANTIC_CHECKER_FACTS_MAX_CHARS,
                ),
                "message": truncate_middle(
                    redact_secrets(str(raw.get("message") or "")),
                    SEMANTIC_CHECKER_MESSAGE_MAX_CHARS,
                ),
                "error_reason": redact_secrets(str(raw.get("error_reason") or "")),
                "terminal": bool(raw.get("terminal", False)),
            }
        )
    return compact


def _surface_response_required(
    spec: LoopSpec,
    capabilities: CapabilityRegistry,
) -> bool:
    allowed = set(spec.allowed_capabilities)
    surface_available = any(
        not item.facts_only and ("*" in allowed or item.name in allowed)
        for item in capabilities.planner_specs()
    )
    if not surface_available:
        return False
    metadata = spec.goal.metadata if isinstance(spec.goal.metadata, dict) else {}
    loop_kind = str(metadata.get("loop_kind") or "")
    execution_mode = str(metadata.get("execution_mode") or "")
    source = str(metadata.get("source") or "")
    delivery = (
        metadata.get("task_context", {}).get("delivery", {})
        if isinstance(metadata.get("task_context"), dict)
        else {}
    )
    delivery_stage = str(delivery.get("stage") or "") if isinstance(delivery, dict) else ""
    return (
        loop_kind == "turn"
        or delivery_stage == "post_semantic_acceptance_outbox"
        or (bool(source) and execution_mode == "foreground")
    )


def _has_surface_result(evidence: dict[str, Any]) -> bool:
    if str(evidence.get("responded_message") or "").strip():
        return True
    capability_result = evidence.get("capability_result")
    if not isinstance(capability_result, dict):
        return False
    facts = capability_result.get("facts")
    return isinstance(facts, dict) and isinstance(facts.get("connector_delivery"), dict)


def _effect_idempotency_key(
    state: LoopRunState,
    planned: PlannedCapabilityStep,
) -> str:
    payload = json.dumps(
        {
            "loop_run_id": state.run_id,
            "attempt": state.attempt,
            "tool": planned.tool,
            "args": planned.args,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _progress_signature(
    planned: PlannedCapabilityStep,
    executed: ExecutedCapabilityStep,
) -> str:
    stable_fact_keys = (
        "status",
        "state_transition",
        "approval_id",
        "continuation_status",
        "loop_terminal_state",
        "reason",
        "entity_type",
        "entity_id",
        "source_path",
    )
    stable_facts = {key: executed.facts[key] for key in stable_fact_keys if key in executed.facts}
    payload = {
        "tool": planned.tool,
        "args": planned.args,
        "ok": executed.ok,
        "action": executed.action,
        "error_reason": executed.error_reason,
        "terminal": executed.terminal,
        "facts": stable_facts,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
