from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..capabilities_types import CapabilityResult
from ..connector_registry import load_connector_adapters


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
    payload = {"error_reason": error_reason, **(facts or {})}
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


def approval_selection(args: dict[str, Any], *, code: str, run_id: str) -> str:
    explicit = arg_text(args, "selection")
    if explicit:
        return explicit
    return "current_run" if run_id and not code else "explicit_code"


def approval_result_message(message: str, facts: dict[str, Any] | None) -> str:
    reason = approval_reason(facts)
    if reason == "run_has_no_approval":
        return "Run has no approval request."
    return message


def approval_failure_is_terminal(facts: dict[str, Any] | None) -> bool:
    return approval_reason(facts) == "approval_code_not_found_in_text"


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


def parse_one_shot_run_at(text: str, *, now: float | None = None) -> float | None:
    raw = text.strip()
    if not raw:
        return None
    base = datetime.fromtimestamp(now or time.time())
    day_offset = 1 if "\u660e\u5929" in raw else 0
    hour, minute = parse_clock_time(raw)
    if hour is None:
        return None
    candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(
        days=day_offset
    )
    if day_offset == 0 and candidate.timestamp() <= base.timestamp():
        candidate += timedelta(days=1)
    return candidate.timestamp()


def parse_clock_time(text: str) -> tuple[int | None, int]:
    match = re.search(r"(^|\D)([01]?\d|2[0-3])[:：]([0-5]\d)", text)
    if match:
        return int(match.group(2)), int(match.group(3))
    match = re.search(r"(\d{1,2})\s*(?:\u70b9|\u65f6)(?:\s*([0-5]?\d)\s*\u5206?)?", text)
    if not match:
        return None, 0
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if "\u4e0b\u5348" in text and hour < 12:
        hour += 12
    if "\u665a\u4e0a" in text and hour < 12:
        hour += 12
    if "\u4e2d\u5348" in text and hour < 12:
        hour += 12
    if hour > 23 or minute > 59:
        return None, 0
    return hour, minute


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


def json_list(value: str) -> list[Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def json_dict(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


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
