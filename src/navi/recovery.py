from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .loop import LoopPhase


@dataclass(frozen=True)
class RecoveryFacts:
    trigger: str
    reason_code: str
    blocked: bool = True
    details: dict[str, Any] = field(default_factory=dict)

    def to_observation(self) -> str:
        facts = {
            "trigger": self.trigger,
            "reason_code": self.reason_code,
            "blocked": self.blocked,
            **self.details,
        }
        return (
            "Loop checker facts:\n"
            + json.dumps(facts, ensure_ascii=False, sort_keys=True)
        )


@dataclass(frozen=True)
class CompletionBlock:
    """Structured completion-verifier block facts."""

    reason_code: str
    run_id: str = ""
    run_status: str = ""
    details: dict[str, Any] = field(default_factory=dict)


class RecoveryPlanner:
    def plan_completion_failure(
        self,
        *,
        block: CompletionBlock,
        events: list[dict[str, Any]],
    ) -> RecoveryFacts:
        if block.run_id:
            return self._run_progress_facts(
                reason_code=block.reason_code,
                run_id=block.run_id,
                run_status=block.run_status,
            )

        cleanup_facts = _last_cleanup_facts(events)
        if cleanup_facts:
            return self._cleanup_facts(reason_code=block.reason_code, facts=cleanup_facts)

        return RecoveryFacts(
            trigger=LoopPhase.CHECK,
            reason_code=block.reason_code,
            details={"failure_domain": "checker_blocked", **block.details},
        )

    def _run_progress_facts(
        self,
        *,
        reason_code: str,
        run_id: str,
        run_status: str,
    ) -> RecoveryFacts:
        details = {
            "blocked_entity_type": "delegation_run",
            "run_id": run_id,
            "run_status": run_status,
        }
        return RecoveryFacts(
            trigger=LoopPhase.CHECK,
            reason_code=reason_code,
            details=details,
        )

    def _cleanup_facts(self, *, reason_code: str, facts: dict[str, Any]) -> RecoveryFacts:
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
            trigger=LoopPhase.CHECK,
            reason_code=reason_code,
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
