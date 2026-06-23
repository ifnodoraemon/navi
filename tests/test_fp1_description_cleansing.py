"""FP-1/FP-7 regression: tool descriptions must carry pure capability
semantics, not routing policy, sequencing rules, or follow-up advice.

After Batch C, the following descriptions were cleansed of procedural/routing
content:
  - delegate.spawn: removed "Must only be used after gathering sufficient local
    facts via foreground tools" (sequencing rule belongs in routing_rules).
  - delegate.delete: removed "Bulk cleanup must include source or kind..."
    (constraint moved to input_schema field descriptions).
  - watch.delete: removed "Requires user approval" (safeguard, not semantics).
  - shell.run: removed the allocate_pty decision rule from the description
    (moved to the allocate_pty arg description).
"""
from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (PROJECT_ROOT / path).read_text(encoding="utf-8")


def test_delegate_spawn_description_is_pure_semantics() -> None:
    specs = _read("src/navi/actions/specs.py")
    # The sequencing rule must be gone from the description.
    assert "Must only be used after gathering sufficient local facts" not in specs


def test_delegate_delete_bulk_constraint_moved_to_schema() -> None:
    specs = _read("src/navi/actions/specs.py")
    # The description no longer carries the bulk-cleanup guardrail prose...
    assert (
        "Bulk cleanup must include source or kind so status-only filters"
        not in specs
    )
    # ...but the constraint survives in the input_schema field descriptions.
    assert "Status-only filters are rejected" in specs


def test_watch_delete_description_drops_approval_safeguard() -> None:
    specs = _read("src/navi/actions/specs.py")
    desc_line = 'description="""Permanently delete a watch or task'
    assert desc_line in specs
    assert "Requires user approval" not in specs


def test_shell_run_description_drops_pty_decision_rule() -> None:
    core = _read("src/navi/core_tools/registration.py")
    # The pty decision rule moved out of the description...
    assert "Set allocate_pty to true if the command strictly requires" not in core
    # ...and into the allocate_pty arg description.
    assert "Allocate a pseudo-terminal. Use only when the command strictly requires" in core


def test_specs_data_scheduling_rule_does_not_name_specific_tools() -> None:
    """FP-1: the scheduling routing rule must not hardcode tool names like
    watch.create or shell.run. The model discovers the right tool from the
    manifest."""
    data = _read("src/navi/specs_data.py")
    # The scheduling routing rule previously named watch.create and shell.run.
    # After cleansing it refers only to "the scheduling capability".
    assert "call watch.create" not in data
    assert "kind=once and run_at_text rather than using shell.run" not in data


def test_prompt_os_elevation_explanation_does_not_name_tool() -> None:
    """FP-1: the read-ceiling permission explanation must not hardcode the
    session.request_elevation tool name as a routing hint."""
    pos = _read("src/navi/prompt_os.py")
    assert "session.request_elevation" not in pos
    assert "approval.request" not in pos
