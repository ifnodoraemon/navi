from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RecoveryFacts:
    trigger: str
    reason: str
    blocked: bool = True
    details: dict[str, Any] = field(default_factory=dict)

    def to_observation(self) -> str:
        facts = {
            "trigger": self.trigger,
            "reason": self.reason,
            "blocked": self.blocked,
            **self.details,
        }
        return (
            "Loop checker facts:\n"
            + json.dumps(facts, ensure_ascii=False, sort_keys=True)
        )


@dataclass(frozen=True)
class CompletionBlock:
    """Structured completion-verifier block reason.

    Carries both the human-readable ``reason`` (used for traces and recovery
    plan ``reason``) and the structured ``run_id`` / ``run_status`` fields that
    the recovery planner consumes directly — instead of regex-parsing the
    reason string back into fields (a fragile string roundtrip).
    """

    reason: str
    run_id: str = ""
    run_status: str = ""


class RecoveryPlanner:
    def plan_completion_failure(
        self,
        *,
        block: CompletionBlock,
        events: list[dict[str, Any]],
    ) -> RecoveryFacts:
        if block.run_id:
            return self._run_progress_facts(
                block_reason=block.reason,
                run_id=block.run_id,
                run_status=block.run_status,
            )

        cleanup_facts = _last_cleanup_facts(events)
        if cleanup_facts:
            return self._cleanup_facts(block_reason=block.reason, facts=cleanup_facts)

        return RecoveryFacts(
            trigger="loop.check",
            reason=block.reason,
            details={"failure_domain": "checker_blocked"},
        )

    def _run_progress_facts(
        self,
        *,
        block_reason: str,
        run_id: str,
        run_status: str,
    ) -> RecoveryFacts:
        details = {
            "blocked_entity_type": "delegation_run",
            "run_id": run_id,
            "run_status": run_status,
        }
        return RecoveryFacts(
            trigger="loop.check",
            reason=block_reason,
            details=details,
        )

    def _cleanup_facts(self, *, block_reason: str, facts: dict[str, Any]) -> RecoveryFacts:
        details: dict[str, Any] = {
            "blocked_entity_type": "delegation_cleanup",
            "cleanup_complete": False,
            "remaining_count": facts.get("remaining_count"),
        }
        source = str(facts.get("source_filter") or "").strip()
        kind = str(facts.get("kind_filter") or "").strip()
        if source:
            details["source_filter"] = source
        if kind:
            details["kind_filter"] = kind
        return RecoveryFacts(
            trigger="loop.check",
            reason=block_reason,
            details=details,
        )


def _last_cleanup_facts(events: list[dict[str, Any]]) -> dict[str, Any]:
    for event in reversed(events):
        if event.get("tool") != "delegate.delete":
            continue
        facts = event.get("facts")
        if (
            isinstance(facts, dict)
            and facts.get("entity_type") == "bulk_delete"
            and facts.get("completion_evidence") is False
        ):
            return facts
    return {}
