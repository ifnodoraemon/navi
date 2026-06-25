"""Tests for the remote-surface → delegate.spawn routing fix.

Production symptom: from a WeChat (remote connector) surface, the user asked
"把我电脑上的简历发我". The planner saw that file.read/directory.list/shell
were not in its tool manifest and flatly refused with "我无法直接访问您电脑
上的文件" — instead of either delegating a local search task or asking for
clarification.

Current contract:
- delegate.spawn's description states only capability semantics.
- The planner policy states the general remote-surface rule without smuggling
  routing instructions into the tool description.
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


def test_routing_rules_cover_remote_surface_local_access() -> None:
    """The TASK ROUTING RULES must include guidance that, from a surface
    without direct OS tools, governed local delegation is the path to local access."""
    rules = SYSCALL_PLANNER_SPEC.get("routing_rules") or []
    parts: list[str] = []
    for rule in rules:
        if isinstance(rule, dict):
            parts.extend(str(v) for v in rule.values())
        else:
            parts.append(str(rule))
    combined = " ".join(parts).lower()
    assert "remote" in combined or "surface without" in combined, (
        "routing rules must address the remote-surface local-access case"
    )
    assert "governed local" in combined or "delegation capability" in combined, (
        "routing rules must describe the governed local-access path"
    )


def test_planner_system_prompt_contains_remote_access_rule() -> None:
    """The assembled planner system prompt must surface the remote-surface →
    governed local access guidance to the model."""
    rendered = assemble_planner_system_prompt().render().lower()
    assert "governed local" in rendered or "delegation capability" in rendered
    assert "remote" in rendered or "surface without" in rendered
