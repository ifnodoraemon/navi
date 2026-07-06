from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..capabilities_types import CapabilityResult
from ..connector_registry import load_connector_adapters
from ..capability_contract import CAPABILITY_ERROR_REASON_KEY
from ..json_utils import json_object


def fact_result(action: str, facts: dict[str, Any], *, run_id: str = "") -> CapabilityResult:
    return CapabilityResult(
        ok=True,
        action=action,
        observation=json.dumps(facts, ensure_ascii=False, sort_keys=True),
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
        observation=json.dumps(payload, ensure_ascii=False, sort_keys=True),
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
    explicit = arg_text(args, "selection")
    if explicit:
        return explicit
    if batch_id and not code and not run_id:
        return "batch_id"
    return "current_run" if run_id and not code else "explicit_code"


def approval_result_message(message: str, facts: dict[str, Any] | None) -> str:
    reason = approval_reason(facts)
    if reason == "run_has_no_approval":
        return "Run has no approval request."
    return message


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


def float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None



def positive_int(value: Any, *, default: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, maximum))


def workflow_not_found(workflow_id: str) -> CapabilityResult:
    return failure_result(
        "workflow",
        f"workflow not found: {workflow_id}",
        error_reason="not_found",
        facts={"workflow_id": workflow_id, "reason": "workflow_not_found"},
    )


json_dict = json_object


def json_list(value: str) -> list[Any]:
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def remote_source(source: str) -> bool:
    raw = source.strip()
    if not raw:
        return False
    connector_sources: set[str] = set()
    for adapter in load_connector_adapters():
        connector_sources.update({adapter.name, adapter.spec.surface, adapter.spec.local_source})
    return raw in connector_sources


def resolve_workspace(workspace: str, *, default: Path) -> str:
    raw = workspace.strip() if workspace else ""
    return str(Path(raw).expanduser().resolve()) if raw else str(default.resolve())
