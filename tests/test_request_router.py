from __future__ import annotations

import pytest

from navi.loop_contracts import RequestIntent, RequestRoute
from navi.request_router import RequestRouter, request_router_contract, route_for_intent


def test_request_router_maps_model_owned_intents_to_fast_and_slow_paths() -> None:
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
    assert answer.route == RequestRoute.FAST_PATH
    assert open_goal.intent == RequestIntent.OPEN_GOAL
    assert open_goal.route == RequestRoute.SLOW_PATH
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
    assert contract["system_role"] == "validate_intent_and_map_fast_slow"
    assert contract["routes"]["answer_now"] == "fast_path"
    assert contract["routes"]["open_goal"] == "slow_path"
    assert route_for_intent("resume_goal") == RequestRoute.SLOW_PATH
    assert route_for_intent("control_goal") == RequestRoute.FAST_PATH
