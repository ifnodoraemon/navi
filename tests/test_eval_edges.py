from __future__ import annotations

from pathlib import Path

import pytest

from navi.evals import (
    EvalResult,
    load_delegation_eval_cases,
    load_delegation_eval_dataset,
    match_delegation_eval_case,
    results_to_json,
    run_delegation_eval_dataset,
    validate_delegation_eval_dataset,
    validate_delegation_eval_cases,
)
from navi.syscalls import ModelSyscall
from navi.tools import ToolSpec


def _tool(name: str = "final.answer", permission: str = "read") -> ToolSpec:
    return ToolSpec(
        name=name,
        description="test",
        input_schema={"type": "object", "properties": {"message": {"type": "string"}}},
        output_schema={"type": "object"},
        permission=permission,
    )


def test_load_delegation_eval_cases_rejects_invalid_shapes(tmp_path):
    not_mapping = tmp_path / "not_mapping.yaml"
    not_mapping.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a mapping"):
        load_delegation_eval_cases(not_mapping)

    missing_cases = tmp_path / "missing_cases.yaml"
    missing_cases.write_text("version: 1", encoding="utf-8")
    with pytest.raises(ValueError, match="cases list"):
        load_delegation_eval_cases(missing_cases)

    bad_case = tmp_path / "bad_case.yaml"
    bad_case.write_text("cases:\n  - text\n", encoding="utf-8")
    with pytest.raises(ValueError, match="case 0"):
        load_delegation_eval_cases(bad_case)

    loaded = load_delegation_eval_dataset(
        Path(__file__).resolve().parents[1] / "evals" / "delegation_cases.yaml"
    )
    assert "coverage" in loaded


def test_validate_delegation_eval_cases_reports_dataset_errors():
    cases = [
        {},
        {
            "id": "dup",
            "message": "",
            "expect": {"tool": "missing", "permission": "read"},
        },
        {
            "id": "dup",
            "message": "hello",
            "expect": {
                "tool": "final.answer",
                "permission": "write",
                "args": {"unknown": "value"},
            },
        },
        {"id": "no-expect", "message": "hello"},
    ]

    errors = validate_delegation_eval_cases(cases, [_tool()])

    assert "case[0]: missing id" in errors
    assert "case[0]: missing message" in errors
    assert "case[0]: missing expect mapping" in errors
    assert "dup: unknown expected tool 'missing'" in errors
    assert "dup: duplicate id" in errors
    assert "dup: expected permission 'write' does not match 'read'" in errors
    assert "dup: args.unknown is not declared by final.answer" in errors
    assert "no-expect: missing expect mapping" in errors


def test_validate_delegation_eval_dataset_enforces_required_category_coverage():
    dataset = {
        "coverage": {"required_categories": ["greeting", "coding_debugging"]},
        "cases": [
            {
                "id": "hello",
                "category": "unknown",
                "message": "hello",
                "expect": {"tool": "final.answer", "permission": "read"},
            },
            {
                "id": "missing-category",
                "message": "hello",
                "expect": {"tool": "final.answer", "permission": "read"},
            },
        ],
    }

    errors = validate_delegation_eval_dataset(dataset, [_tool()])

    assert "hello: unknown category 'unknown'" in errors
    assert "missing-category: missing category" in errors
    assert "dataset: missing required category 'coding_debugging'" in errors
    assert "dataset: missing required category 'greeting'" in errors


def test_validate_delegation_eval_dataset_enforces_required_tool_coverage():
    dataset = {
        "coverage": {"required_tools": ["final.answer", "delegate.list", "missing.tool"]},
        "cases": [
            {
                "id": "hello",
                "message": "hello",
                "expect": {"tool": "final.answer", "permission": "read"},
            },
        ],
    }

    errors = validate_delegation_eval_dataset(dataset, [_tool(), _tool("delegate.list")])

    assert "dataset: unknown required tool 'missing.tool'" in errors
    assert "dataset: missing required tool 'delegate.list'" in errors


def test_match_delegation_eval_case_reports_tool_and_permission_drift():
    case = {"expect": {"tool": "delegate.list", "permission": "read"}}
    decision = ModelSyscall(tool="final.answer", permission="write")

    errors = match_delegation_eval_case(case, decision)

    assert "tool expected 'delegate.list', got 'final.answer'" in errors
    assert "permission expected 'read', got 'write'" in errors


def test_results_to_json_serializes_eval_results():
    text = results_to_json(
        [
            EvalResult(
                id="case",
                ok=True,
                expected={"tool": "final.answer"},
                actual={"tool": "final.answer"},
                errors=[],
            )
        ]
    )

    assert '"id": "case"' in text


@pytest.mark.asyncio
async def test_run_delegation_eval_dataset_returns_dataset_error(tmp_path):
    dataset = tmp_path / "bad.yaml"
    dataset.write_text(
        """
cases:
  - id: bad
    message: hello
    expect:
      tool: missing
      permission: read
""",
        encoding="utf-8",
    )

    results = await run_delegation_eval_dataset(home=tmp_path, project_dir=tmp_path, dataset=dataset)

    assert results[0].id == "dataset"
    assert results[0].ok is False
    assert "unknown expected tool" in results[0].errors[0]
