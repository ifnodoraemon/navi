"""Tests for the remote-surface local-access capability boundary.

Production symptom: from a WeChat (remote connector) surface, the user asked
"把我电脑上的简历发我". The planner saw that file.read/directory.list/shell
were not in its tool manifest and flatly refused with "我无法直接访问您电脑
上的文件" — instead of either delegating a local search task or asking for
clarification.

Current contract:
- delegate.spawn's description states only capability semantics.
- Remote connector policy exposes governed delegation while blocking direct OS
  classes; the planner prompt must not carry remote-surface routing policy.
"""

from __future__ import annotations

from navi.actions.specs import ACTION_SPECS
from navi.connector_runtime import (
    REMOTE_BLOCKED_CAPABILITY_CLASSES,
    REMOTE_CONNECTOR_TOOL_POLICY,
)
from navi.prompt_os import assemble_planner_system_prompt


def _delegate_spawn_spec():
    for spec in ACTION_SPECS:
        if spec.name == "delegate.spawn":
            return spec
    raise AssertionError("delegate.spawn spec not found")


def test_delegate_spawn_description_states_local_execution() -> None:
    """The tool description should say what the capability does, not when the
    planner should choose it."""
    description = _delegate_spawn_spec().description.lower()
    assert "local" in description, (
        "delegate.spawn description must state the task runs locally"
    )
    assert "file.read" not in description
    assert "shell.run" not in description


def test_delegate_spawn_description_does_not_carry_routing_policy() -> None:
    description = _delegate_spawn_spec().description.lower()
    assert "do not" not in description
    assert "refuse" not in description
    assert "use delegate.spawn" not in description


def test_remote_surface_local_access_is_declared_by_policy_not_prompt() -> None:
    policy = REMOTE_CONNECTOR_TOOL_POLICY
    assert "delegate.spawn" not in policy.blocked_tools
    assert "delegation" not in policy.blocked_capability_classes
    assert {"file.read", "directory", "shell"} <= REMOTE_BLOCKED_CAPABILITY_CLASSES


def test_planner_system_prompt_does_not_encode_remote_access_routing() -> None:
    rendered = assemble_planner_system_prompt().render().lower()
    assert "remote surface" not in rendered
    assert "governed local" not in rendered
    assert "delegation capability" not in rendered
    assert "delegate.spawn" not in rendered
