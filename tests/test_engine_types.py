from navi.engine_types import AgentTurnResult


def test_failure_surface_is_structured_facts_not_synthesized_sentence() -> None:
    result = AgentTurnResult(
        text="",
        action="execute:system.provider_error",
        ok=False,
        error_reason="provider_no_response",
        facts={"error_type": "RuntimeError"},
    )

    text = result.surfaced_text()

    assert "[execute:system.provider_error] failed" not in text
    assert "action=execute:system.provider_error" in text
    assert "error_reason=provider_no_response" in text
    assert "error_type=RuntimeError" in text
    assert "ok=False" in text
