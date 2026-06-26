from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class RecoveryChoice:
    kind: str
    reason: str
    tool: str = ""
    permission: str = "read"
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RecoveryPlan:
    trigger: str
    reason: str
    recommended: str
    choices: list[RecoveryChoice]

    def to_observation(self) -> str:
        facts = {
            "trigger": self.trigger,
            "reason": self.reason,
            "blocked": True,
        }
        return (
            "Completion verifier facts:\n"
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
    ) -> RecoveryPlan:
        if block.run_id:
            return self._run_progress_plan(
                block_reason=block.reason,
                run_id=block.run_id,
                run_status=block.run_status,
            )

        cleanup_facts = _last_cleanup_facts(events)
        if cleanup_facts:
            return self._cleanup_plan(block_reason=block.reason, facts=cleanup_facts)

        return RecoveryPlan(
            trigger="completion.verify",
            reason=block.reason,
            recommended="retry_capability",
            choices=[
                RecoveryChoice(
                    kind="retry_capability",
                    reason="Retry the last capability or try an alternative approach.",
                ),
                RecoveryChoice(
                    kind="rollback_proposal",
                    reason="Create a rollback proposal if the failed work changed local state.",
                    tool="evolution.propose",
                    permission="prepare",
                ),
                RecoveryChoice(
                    kind="report_status",
                    reason="Report current status to user if all retry options are exhausted.",
                    tool="final.answer",
                    permission="read",
                    args={
                        "message": ("Verification incomplete. Attempted alternatives exhausted.")
                    },
                ),
            ],
        )

    def _run_progress_plan(
        self,
        *,
        block_reason: str,
        run_id: str,
        run_status: str,
    ) -> RecoveryPlan:
        if run_status == "pending":
            first = RecoveryChoice(
                kind="continue",
                reason="The delegation run exists but has not been prepared.",
                tool="delegate.prepare",
                permission="prepare",
                args={"run_id": run_id},
            )
        else:
            first = RecoveryChoice(
                kind="ask_user",
                reason="The delegation run is prepared and needs user approval before execution.",
                tool="approval.request",
                permission="prepare",
                args={"run_id": run_id},
            )
        return RecoveryPlan(
            trigger="completion.verify",
            reason=block_reason,
            recommended=first.kind,
            choices=[
                first,
                RecoveryChoice(
                    kind="alternate_capability",
                    reason="Inspect delegation state before choosing another action.",
                    tool="delegate.list",
                    permission="read",
                ),
                RecoveryChoice(
                    kind="report_status",
                    reason="Report delegation status to user if no other option is viable.",
                    tool="final.answer",
                    permission="read",
                    args={
                        "message": "Delegation run is in progress. Awaiting next actionable state."
                    },
                ),
            ],
        )

    def _cleanup_plan(self, *, block_reason: str, facts: dict[str, Any]) -> RecoveryPlan:
        args: dict[str, Any] = {"status": "failed"}
        source = str(facts.get("source_filter") or "").strip()
        kind = str(facts.get("kind_filter") or "").strip()
        if source:
            args["source"] = source
        if kind:
            args["kind"] = kind
        return RecoveryPlan(
            trigger="completion.verify",
            reason=block_reason,
            recommended="continue",
            choices=[
                RecoveryChoice(
                    kind="continue",
                    reason="Cleanup is incomplete; run the same cleanup without the limiting filter.",
                    tool="delegate.delete",
                    permission="write",
                    args=args,
                ),
                RecoveryChoice(
                    kind="ask_user",
                    reason="Ask before continuing if the remaining failed records are ambiguous.",
                    tool="final.answer",
                    permission="read",
                    args={
                        "message": (
                            f"Cleanup is not verified complete; {facts.get('remaining_count')} failed records remain."
                        )
                    },
                ),
                RecoveryChoice(
                    kind="rollback_proposal",
                    reason="If cleanup removed the wrong records, propose rollback from trace and run evidence.",
                    tool="evolution.propose",
                    permission="prepare",
                ),
            ],
        )


def _last_cleanup_facts(events: list[dict[str, Any]]) -> dict[str, Any]:
    for event in reversed(events):
        if event.get("tool") != "delegate.delete":
            continue
        facts = event.get("facts")
        if isinstance(facts, dict) and facts.get("cleanup_complete") is False:
            return facts
    return {}
