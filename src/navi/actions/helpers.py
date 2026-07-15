from __future__ import annotations

from typing import Any

from ..capabilities_types import CapabilityResult
from ..capability_contract import CAPABILITY_ERROR_REASON_KEY
from ..json_utils import json_object


def fact_result(action: str, facts: dict[str, Any], *, run_id: str = "") -> CapabilityResult:
    return CapabilityResult(
        ok=True,
        action=action,
        run_id=run_id,
        facts=facts,
    )


def failure_result(
    action: str,
    message: str,
    *,
    error_reason: str,
    facts: dict[str, Any] | None = None,
    run_id: str = "",
    terminal: bool = False,
) -> CapabilityResult:
    payload = {CAPABILITY_ERROR_REASON_KEY: error_reason, **(facts or {})}
    return CapabilityResult(
        ok=False,
        action=action,
        message=message,
        run_id=run_id,
        terminal=terminal,
        facts=payload,
        error_reason=error_reason,
    )


def transition_facts(entity_type: str, entity_id: str, transition: str) -> dict[str, Any]:
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "state_transition": transition,
        "turn_scope": "current",
    }


def arg_text(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    return str(value).strip() if value is not None else ""


def approval_selection(args: dict[str, Any], *, code: str, run_id: str, batch_id: str = "") -> str:
    if batch_id and not code and not run_id:
        return "batch_id"
    return "current_run" if run_id and not code else "explicit_code"


def approval_failure_is_terminal(facts: dict[str, Any] | None) -> bool:
    return approval_reason(facts) in {"approval_code_not_found", "approval_code_not_found_in_text"}


def approval_error_reason(facts: dict[str, Any] | None) -> str:
    reason = approval_reason(facts)
    if reason in {"invalid_decision", "approval_identifier_missing"}:
        return "schema_mismatch"
    return "not_found" if reason else "unknown"


def approval_reason(facts: dict[str, Any] | None) -> str:
    if not isinstance(facts, dict):
        return ""
    if isinstance(facts.get("approval_resolution"), dict):
        return str(facts["approval_resolution"].get("reason") or "")
    return str(facts.get("reason") or "")


def positive_int(value: Any, *, default: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, maximum))


json_dict = json_object
