from navi.turn_result import AgentTurnResult


def test_failure_surface_does_not_synthesize_user_text() -> None:
    result = AgentTurnResult(
        text="",
        action="execute:system.provider_error",
        ok=False,
        error_reason="provider_no_response",
        facts={"error_type": "RuntimeError"},
    )

    text = result.surfaced_text()

    assert text == ""
