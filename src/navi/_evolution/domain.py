"""Evolution domain types and pure functions."""

from __future__ import annotations
from navi.lifecycle import Phase, Governance, Acceptance, Resolution

import json
from dataclasses import dataclass
from typing import Any

from ..json_utils import json_object
from ..loop import TracePhase


_EVALUATION_RESULTS = frozenset({"approved", "rejected", "pending"})


@dataclass(frozen=True)
class EvolutionEvent:
    id: str
    run_id: str
    target_type: str
    target_id: str
    reason: str
    before: str
    after: str
    diff: str
    created_at: float
    rolled_back_at: float


@dataclass(frozen=True)
class EvolutionProposal:
    id: str
    target_type: str
    target_id: str
    reason: str
    expected_benefit: str
    risk: str
    before: str
    after: str
    diff: str
    rollback_plan: str
    required_approval_level: str
    evidence: str
    source_run_id: str
    status: str
    created_at: float
    applied_at: float
    applied_event_id: str
    eval_cases: str
    evaluation_result: str
    approved_by: str = ""
    approved_at: float = 0.0


@dataclass(frozen=True)
class EvolutionTarget:
    target_type: str
    description: str
    source: str
    permissions_can_expand: bool = False


EVOLUTION_TARGETS: tuple[EvolutionTarget, ...] = (
    EvolutionTarget(
        "prompt_layer", "Versioned prompt layer content that shapes model behavior.", "prompting"
    ),
    EvolutionTarget(
        "skill", "Promptable skill content, metadata, provenance, and verification state.", "skills"
    ),
    EvolutionTarget(
        "memory_item", "Typed durable memory item content and lifecycle state.", "memory"
    ),
    EvolutionTarget(
        "memory_schema",
        "Memory types, priority, expiry, contradiction, and recall policy.",
        "memory",
    ),
    EvolutionTarget(
        "tool_spec",
        "Capability/tool manifest schema, permissions, and descriptions.",
        "tools",
        True,
    ),
    EvolutionTarget(
        "connector_spec",
        "Connector affordances, surface commands, and status facts.",
        "connectors",
        True,
    ),
    EvolutionTarget(
        "workflow_policy",
        "Daemon, execution, approval, and lifecycle decision policy.",
        "runtime",
        True,
    ),
    EvolutionTarget("eval_case", "Evaluation dataset case and expected behavior.", "evals"),
    EvolutionTarget("graph_node", "Personal graph project, person, and task relationship facts.", "graph"),
    EvolutionTarget("run_execution", "Recorded run execution outcome state.", "execution"),
)


def list_evolution_targets() -> list[dict[str, Any]]:
    return [target.__dict__ for target in EVOLUTION_TARGETS]


def known_evolution_target(target_type: str) -> bool:
    return any(target.target_type == target_type for target in EVOLUTION_TARGETS)


# Governance event types recorded in the evolution ledger alongside evolution
# target types. Declaring these prevents schema drift (principle 1.2): the
# ``record()`` gate rejects any ``target_type`` not in this set, so typos and
# undeclared event categories surface loudly instead of silently persisting.
GOVERNANCE_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "execution_grant",
        "approval",
    }
)


def known_ledger_target_type(target_type: str) -> bool:
    """Whether ``target_type`` is a declared evolution target or governance event."""
    return (
        known_evolution_target(target_type)
        or target_type in GOVERNANCE_EVENT_TYPES
    )


# Data-driven map of which spec-file targets persist to (subdir, suffix) on apply.
# prompt_layer is handled separately because it writes through PromptLayerStore.
_SPEC_FILE_TARGETS: dict[str, tuple[str, str]] = {
    "tool_spec": ("specs", ".yaml"),
    "connector_spec": ("specs", ".yaml"),
    "workflow_policy": ("specs", ".yaml"),
    "memory_schema": ("specs", ".yaml"),
    "eval_case": ("evals", ".json"),
}


def _summarize_trace_events(events: list[Any]) -> str:
    lines: list[str] = []
    for event in events:
        if event.phase == TracePhase.TURN_START:
            message = _json_field(event.input_json, "message")
            if message:
                lines.append(f"user: {message}")
        elif event.phase == TracePhase.PLANNER_SYSCALL:
            details = json_object(event.output_json)
            tool = str(details.get("tool") or event.tool or "").strip()
            reason = str(details.get("reason") or event.message or "").strip()
            if tool:
                lines.append(f"planner selected {tool}: {reason}")
        elif event.phase == TracePhase.CAPABILITY_RESULT:
            outcome = "ok" if event.ok else "failed"
            lines.append(f"capability {event.tool} {outcome}: {event.message}".strip())
        elif event.phase == TracePhase.TURN_FINAL:
            lines.append(f"assistant: {event.message}")
    return "\n".join(line for line in lines if line)[:12000]


def _json_field(raw: str, field: str) -> str:
    value = json_object(raw).get(field)
    return str(value).strip() if value is not None else ""


def _daily_journey_eval_schema() -> dict[str, Any]:
    return {
        "name": "daily_journey_eval",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "id": {"type": "string"},
                "user_goal": {"type": "string"},
                "steps": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "additionalProperties": True,
                        "properties": {
                            "user": {"type": "string"},
                            "expect": {
                                "type": "object",
                                "additionalProperties": True,
                            },
                        },
                        "required": ["user", "expect"],
                    },
                },
            },
            "required": ["id", "user_goal", "steps"],
        },
    }
