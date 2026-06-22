"""FP-2 regression: mutating tools must return the uniform state-transition
vocabulary (entity_type, entity_id, state_transition, turn_scope) so the agent
layer can reason over state changes without parsing tool-specific fields.
"""
from __future__ import annotations

from navi.core_tools import _file_write, _shell_run, _test_run

_TRANSITION_KEYS = {"entity_type", "entity_id", "state_transition", "turn_scope"}


def _assert_transition_vocab(facts: dict, expected_transition: str) -> None:
    missing = _TRANSITION_KEYS - facts.keys()
    assert not missing, f"missing transition keys: {missing}"
    assert facts["state_transition"] == expected_transition
    assert facts["turn_scope"] == "current"
    assert facts["entity_type"]
    assert facts["entity_id"]


def test_file_write_returns_transition_vocab(tmp_path):
    target = tmp_path / "out.txt"
    result = _file_write(
        {"path": str(target), "content": "hello", "create_dirs": True},
        project_dir=tmp_path,
    )
    assert result.ok
    _assert_transition_vocab(result.facts, "written")


def test_file_write_append_transition(tmp_path):
    target = tmp_path / "out.txt"
    _file_write(
        {"path": str(target), "content": "a", "create_dirs": True},
        project_dir=tmp_path,
    )
    result = _file_write(
        {"path": str(target), "content": "b", "mode": "append"},
        project_dir=tmp_path,
    )
    assert result.ok
    _assert_transition_vocab(result.facts, "appended")


def test_shell_run_returns_transition_vocab(tmp_path):
    result = _shell_run(
        {"command": ["echo", "hi"], "cwd": str(tmp_path)},
        project_dir=tmp_path,
    )
    assert result.ok
    _assert_transition_vocab(result.facts, "executed")


def test_test_run_returns_transition_vocab(tmp_path):
    result = _test_run(
        {"command": ["echo", "hi"], "cwd": str(tmp_path)},
        project_dir=tmp_path,
    )
    assert result.ok
    _assert_transition_vocab(result.facts, "executed")
