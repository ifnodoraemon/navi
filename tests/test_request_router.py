from __future__ import annotations

import pytest

from navi.loop_contracts import RequestIntent, RequestRoute
from navi.request_router import RequestRouter, request_router_contract, route_for_intent


def test_request_router_maps_model_owned_intents_to_unified_loop() -> None:
    router = RequestRouter()

    answer = router.route_model_decision(
        {
            "intent": "answer_now",
            "reason": "single-turn factual response",
            "confidence": 0.8,
        }
    )
    open_goal = router.route_model_decision(
        {
            "intent": "open_goal",
            "reason": "requires durable multi-step work",
            "confidence": 0.9,
            "facts": {"requires_verification": True},
        }
    )

    assert answer.intent == RequestIntent.ANSWER_NOW
    assert answer.route == RequestRoute.UNIFIED_LOOP
    assert open_goal.intent == RequestIntent.OPEN_GOAL
    assert open_goal.route == RequestRoute.UNIFIED_LOOP
    assert open_goal.facts["requires_verification"] is True


def test_request_router_rejects_invalid_or_mismatched_decisions() -> None:
    router = RequestRouter()

    with pytest.raises(ValueError, match="unsupported request intent"):
        router.route_model_decision({"intent": "edit_code_now", "reason": "bad intent"})
    with pytest.raises(ValueError, match="routing reason is required"):
        router.route_model_decision({"intent": "answer_now"})


def test_route_contract_is_explicit_and_not_keyword_classifier() -> None:
    contract = request_router_contract()

    assert contract["intent_owner"] == "model_or_structured_protocol"
    assert contract["system_role"] == "validate_intent_for_unified_loop"
    assert set(contract["routes"].values()) == {"unified_loop"}
    assert route_for_intent("resume_goal") == RequestRoute.UNIFIED_LOOP
    assert route_for_intent("control_goal") == RequestRoute.UNIFIED_LOOP
