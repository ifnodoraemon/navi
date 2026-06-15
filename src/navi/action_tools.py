from __future__ import annotations

from typing import Any

from .spec_loader import load_spec
from .tools import ToolSpec


def load_action_tool_specs() -> list[ToolSpec]:
    raw_specs = load_spec("action_tools.yaml") or []
    if not isinstance(raw_specs, list):
        raise ValueError("action_tools.yaml must contain a list")
    return [_tool_spec(item) for item in raw_specs]


def action_handler_keys() -> dict[str, str]:
    return {spec["name"]: spec["handler"] for spec in _raw_action_specs()}


def _raw_action_specs() -> list[dict[str, Any]]:
    raw_specs = load_spec("action_tools.yaml") or []
    if not isinstance(raw_specs, list) or not all(isinstance(item, dict) for item in raw_specs):
        raise ValueError("action_tools.yaml must contain a list of mappings")
    return raw_specs


def _tool_spec(item: dict[str, Any]) -> ToolSpec:
    capability_class = str(item.get("capability_class") or "").strip()
    if not capability_class:
        raise ValueError(f"action tool {item.get('name')!r} must declare capability_class")
    execution_contexts = tuple(str(value).strip() for value in item.get("execution_contexts") or [])
    execution_contexts = tuple(value for value in execution_contexts if value)
    if not execution_contexts:
        raise ValueError(f"action tool {item.get('name')!r} must declare execution_contexts")
    return ToolSpec(
        name=str(item["name"]),
        capability_class=capability_class,
        execution_contexts=execution_contexts,
        description=str(item.get("description", "")),
        input_schema=dict(item.get("input_schema") or {"type": "object"}),
        output_schema=dict(item.get("output_schema") or {"type": "object"}),
        facts_only=bool(item.get("facts_only", True)),
        mutates=bool(item.get("mutates", False)),
        permission=str(item.get("permission", "read")),
        source=str(item.get("source", "action")),
        routing_hints=list(item.get("routing_hints") or []),
    )
