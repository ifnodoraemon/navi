"""Tests for the remote-surface → delegate.spawn routing fix.

Production symptom: from a WeChat (remote connector) surface, the user asked
"把我电脑上的简历发我". The planner saw that file.read/directory.list/shell
were not in its tool manifest and flatly refused with "我无法直接访问您电脑
上的文件" — instead of either delegating a local search task or asking for
clarification.

Root cause (prompt layer):
- delegate.spawn's description didn't state that the delegated task runs
  locally with full OS access. The planner didn't recognize delegate.spawn
  as the path to local file access from a surface without direct OS tools.
- The "Fact-First / Local-First Policy" routing rule said to gather facts
  with file.read BEFORE delegating. In the remote context file.read is
  blocked, so this condition could never be satisfied → the planner gave up
  and refused.

Fix:
- delegate.spawn description now states the delegated task runs locally with
  full OS access, and that from a surface without direct OS tools,
  delegate.spawn IS the path to local access.
- A new routing rule "Remote Surface → Local Access" makes the
  remote→delegate.spawn path explicit.
"""

from __future__ import annotations

from navi.actions.specs import ACTION_SPECS
from navi.prompt_os import assemble_planner_system_prompt
from navi.specs_data import SYSCALL_PLANNER_SPEC


def _delegate_spawn_spec():
    for spec in ACTION_SPECS:
        if spec.name == "delegate.spawn":
            return spec
    raise AssertionError("delegate.spawn spec not found")


def test_delegate_spawn_description_states_local_execution() -> None:
    """The planner must learn from the delegate.spawn description that the
    delegated task runs locally with full OS access — this is what makes
    delegate.spawn the path to local file/process access from a remote
    surface."""
    description = _delegate_spawn_spec().description.lower()
    assert "local" in description, (
        "delegate.spawn description must state the task runs locally"
    )
    assert "file.read" in description or "file" in description, (
        "delegate.spawn description must mention local file access"
    )


def test_delegate_spawn_description_forbids_flat_refusal() -> None:
    """The delegate.spawn description must explicitly tell the planner not to
    flatly refuse local-file/process requests from a surface without direct
    OS tools."""
    description = _delegate_spawn_spec().description.lower()
    assert "do not" in description and "refuse" in description, (
        "delegate.spawn description must forbid flat refusal of local "
        "access requests"
    )


def test_routing_rules_cover_remote_surface_local_access() -> None:
    """The TASK ROUTING RULES must include guidance that, from a surface
    without direct OS tools, delegate.spawn is the path to local access."""
    rules = SYSCALL_PLANNER_SPEC.get("routing_rules") or []
    parts: list[str] = []
    for rule in rules:
        if isinstance(rule, dict):
            parts.extend(str(v) for v in rule.values())
        else:
            parts.append(str(rule))
    combined = " ".join(parts).lower()
    # The new rule must mention both the remote-surface case and delegate.spawn.
    assert "remote" in combined or "surface without" in combined, (
        "routing rules must address the remote-surface local-access case"
    )
    assert "delegate.spawn" in combined, (
        "routing rules must name delegate.spawn as the local-access path"
    )


def test_planner_system_prompt_contains_remote_access_rule() -> None:
    """The assembled planner system prompt must surface the remote-surface →
    delegate.spawn guidance to the model."""
    rendered = assemble_planner_system_prompt().render().lower()
    assert "delegate.spawn" in rendered
    assert "remote" in rendered or "surface without" in rendered
