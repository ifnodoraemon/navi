from __future__ import annotations

import sys

from navi.checker import DeterministicChecker
from navi.harness import Harness, HarnessCommand
from navi.loop_contracts import (
    GoalSpec,
    LoopSpec,
    LoopTerminalState,
    TimeoutPolicy,
    VerificationKind,
    VerificationStep,
)


def _spec() -> LoopSpec:
    return LoopSpec.from_goal(
        GoalSpec(
            objective="verify objective facts",
            scope=("repo:/tmp/project",),
            acceptance_criteria=("tests pass",),
            permission_ceiling="read",
        ),
        goal_id="goal-1",
        allowed_capabilities=("test.run",),
        verification_ladder=(
            VerificationStep(
                kind=VerificationKind.COMMAND_EXIT_CODE,
                name="unit-tests",
                command="pytest -q",
                timeout=TimeoutPolicy(seconds=120),
            ),
            VerificationStep(
                kind=VerificationKind.ARTIFACT_INSPECTION,
                name="artifact",
            ),
        ),
    )


def test_deterministic_checker_accepts_only_objective_passing_evidence():
    report = DeterministicChecker().evaluate(
        _spec(),
        {
            "unit-tests": {"exit_code": 0, "timed_out": False, "checker_fact": {"error_type": ""}},
            "artifact": {"passed": True},
        },
    )

    assert report.accepted is True
    assert report.state_hint == LoopTerminalState.CONVERGED
    assert [result.passed for result in report.checker_results] == [True, True]


def test_deterministic_checker_failed_command_routes_back_to_reflect():
    report = DeterministicChecker().evaluate(
        _spec(),
        {
            "unit-tests": {"exit_code": 1, "timed_out": False},
            "artifact": {"passed": True},
        },
    )

    assert report.accepted is False
    assert report.blocked is False
    assert report.timed_out is False
    assert report.state_hint == ""
    assert report.checker_results[0].reason == "exit_code_nonzero"


def test_deterministic_checker_timeout_and_missing_evidence_are_structured_states(tmp_path):
    timeout_result = Harness().run_command(
        HarnessCommand(
            command=(sys.executable, "-c", "import time; time.sleep(2)"),
            cwd=tmp_path,
            timeout=TimeoutPolicy(seconds=0.1),
        )
    )
    timed_out = DeterministicChecker().evaluate(
        _spec(),
        {
            "unit-tests": timeout_result.to_facts(),
            "artifact": {"passed": True},
        },
    )
    assert timed_out.accepted is False
    assert timed_out.timed_out is True
    assert timed_out.state_hint == LoopTerminalState.TIMED_OUT

    missing = DeterministicChecker().evaluate(_spec(), {"artifact": {"passed": True}})
    assert missing.accepted is False
    assert missing.blocked is True
    assert missing.state_hint == LoopTerminalState.BLOCKED
    assert missing.checker_results[0].reason == "evidence_missing"
