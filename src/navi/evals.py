from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from .app_factory import build_runtime
from .capabilities import build_capability_registry
from .syscalls import ModelSyscall, ModelSyscallPlanner
from .tools import ToolSpec


@dataclass(frozen=True)
class EvalResult:
    id: str
    ok: bool
    expected: dict[str, Any]
    actual: dict[str, Any]
    errors: list[str]


def load_delegation_eval_cases(path: Path) -> list[dict[str, Any]]:
    return load_delegation_eval_dataset(path)["cases"]


def load_delegation_eval_dataset(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    data = {} if loaded is None else loaded
    if not isinstance(data, dict):
        raise ValueError("eval dataset must be a mapping")
    cases = data.get("cases")
    if not isinstance(cases, list):
        raise ValueError("eval dataset must contain a cases list")
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ValueError(f"case {index} must be a mapping")
    return data


def validate_delegation_eval_cases(cases: list[dict[str, Any]], tools: list[ToolSpec]) -> list[str]:
    return validate_delegation_eval_dataset({"cases": cases}, tools)


def validate_delegation_eval_dataset(dataset: dict[str, Any], tools: list[ToolSpec]) -> list[str]:
    errors: list[str] = []
    by_name = {tool.name: tool for tool in tools}
    seen: set[str] = set()
    cases = dataset.get("cases")
    if not isinstance(cases, list):
        return ["dataset: missing cases list"]
    required_categories = _required_categories(dataset)
    required_tools = _required_tools(dataset)
    unknown_required_tools = required_tools - set(by_name)
    for tool_name in sorted(unknown_required_tools):
        errors.append(f"dataset: unknown required tool {tool_name!r}")
    categories_seen: set[str] = set()
    tools_seen: set[str] = set()
    for index, case in enumerate(cases):
        case_id = str(case.get("id") or "")
        prefix = case_id or f"case[{index}]"
        if not case_id:
            errors.append(f"{prefix}: missing id")
        if case_id in seen:
            errors.append(f"{prefix}: duplicate id")
        seen.add(case_id)
        if not str(case.get("message") or "").strip():
            errors.append(f"{prefix}: missing message")
        category = str(case.get("category") or "").strip()
        if required_categories:
            if not category:
                errors.append(f"{prefix}: missing category")
            elif category not in required_categories:
                errors.append(f"{prefix}: unknown category {category!r}")
            else:
                categories_seen.add(category)
        expected = case.get("expect")
        if not isinstance(expected, dict):
            errors.append(f"{prefix}: missing expect mapping")
            continue
        tool_name = str(expected.get("tool") or "")
        tool = by_name.get(tool_name)
        if tool is None:
            errors.append(f"{prefix}: unknown expected tool {tool_name!r}")
            continue
        if tool_name in required_tools:
            tools_seen.add(tool_name)
        permission = str(expected.get("permission") or "")
        if permission != tool.permission:
            errors.append(f"{prefix}: expected permission {permission!r} does not match {tool.permission!r}")
        _validate_expected_args(prefix, expected.get("args") or {}, tool, errors)
    for category in sorted(required_categories - categories_seen):
        errors.append(f"dataset: missing required category {category!r}")
    for tool_name in sorted(required_tools - tools_seen - unknown_required_tools):
        errors.append(f"dataset: missing required tool {tool_name!r}")
    return errors


async def run_delegation_eval_dataset(
    *,
    home: Path,
    project_dir: Path,
    dataset: Path,
    timeout_seconds: float = 75.0,
) -> list[EvalResult]:
    loaded = load_delegation_eval_dataset(dataset)
    cases = loaded["cases"]
    tools = delegation_eval_tools(home, project_dir=project_dir)
    validation_errors = validate_delegation_eval_dataset(loaded, tools)
    if validation_errors:
        return [
            EvalResult(
                id="dataset",
                ok=False,
                expected={},
                actual={},
                errors=validation_errors,
            )
        ]
    runtime = build_runtime(home)
    planner = ModelSyscallPlanner(runtime.provider)
    results: list[EvalResult] = []
    for case in cases:
        decision = await asyncio.wait_for(
            planner.plan(
                str(case["message"]),
                tools=tools,
                conversation_context=str(case.get("conversation_context") or ""),
            ),
            timeout=timeout_seconds,
        )
        errors = match_delegation_eval_case(case, decision)
        results.append(
            EvalResult(
                id=str(case["id"]),
                ok=not errors,
                expected=dict(case["expect"]),
                actual=asdict(decision),
                errors=errors,
            )
        )
    return results


def delegation_eval_tools(home: Path, *, project_dir: Path) -> list[ToolSpec]:
    return build_capability_registry(home, project_dir=project_dir).list_specs()


def match_delegation_eval_case(case: dict[str, Any], decision: ModelSyscall) -> list[str]:
    expected = case.get("expect") or {}
    errors: list[str] = []
    expected_tool = str(expected.get("tool") or "")
    expected_permission = str(expected.get("permission") or "")
    if decision.tool != expected_tool:
        errors.append(f"tool expected {expected_tool!r}, got {decision.tool!r}")
    if decision.permission != expected_permission:
        errors.append(f"permission expected {expected_permission!r}, got {decision.permission!r}")
    for key, value in (expected.get("args") or {}).items():
        actual = decision.args.get(str(key))
        if str(actual).lower() != str(value).lower():
            errors.append(f"args.{key} expected {value!r}, got {actual!r}")
    return errors


def results_to_json(results: list[EvalResult]) -> str:
    return json.dumps([asdict(result) for result in results], ensure_ascii=False, indent=2)


def _validate_expected_args(
    prefix: str,
    expected_args: dict[str, Any],
    tool: ToolSpec,
    errors: list[str],
) -> None:
    if not isinstance(expected_args, dict):
        errors.append(f"{prefix}: expect.args must be a mapping")
        return
    properties = tool.input_schema.get("properties") or {}
    if not isinstance(properties, dict):
        properties = {}
    for key in expected_args:
        if str(key) not in properties:
            errors.append(f"{prefix}: args.{key} is not declared by {tool.name}")


def _required_categories(dataset: dict[str, Any]) -> set[str]:
    coverage = dataset.get("coverage") or {}
    if not isinstance(coverage, dict):
        return set()
    raw = coverage.get("required_categories") or []
    if not isinstance(raw, list):
        return set()
    return {str(item).strip() for item in raw if str(item).strip()}


def _required_tools(dataset: dict[str, Any]) -> set[str]:
    coverage = dataset.get("coverage") or {}
    if not isinstance(coverage, dict):
        return set()
    raw = coverage.get("required_tools") or []
    if not isinstance(raw, list):
        return set()
    return {str(item).strip() for item in raw if str(item).strip()}
