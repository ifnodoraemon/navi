from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .loop_contracts import RequestIntent, RequestRoute


@dataclass(frozen=True)
class RequestRoutingDecision:
    intent: RequestIntent | str
    route: RequestRoute | str
    reason: str
    goal_id: str = ""
    confidence: float = 0.0
    facts: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if str(self.intent) not in {str(item) for item in RequestIntent}:
            raise ValueError(f"unsupported request intent: {self.intent}")
        expected_route = route_for_intent(self.intent)
        if str(self.route) != str(expected_route):
            raise ValueError(
                f"intent {self.intent} must route to {expected_route}, got {self.route}"
            )
        if not self.reason.strip():
            raise ValueError("routing reason is required")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("routing confidence must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "intent": str(self.intent),
            "route": str(self.route),
            "reason": self.reason,
            "goal_id": self.goal_id,
            "confidence": self.confidence,
            "facts": dict(self.facts),
        }


class RequestRouter:
    """Validate model-owned request intent for Navi's unified loop intake."""

    def route_model_decision(self, decision: dict[str, Any]) -> RequestRoutingDecision:
        intent = _parse_intent(decision.get("intent"))
        routed = RequestRoutingDecision(
            intent=intent,
            route=route_for_intent(intent),
            reason=str(decision.get("reason") or "").strip(),
            goal_id=str(decision.get("goal_id") or "").strip(),
            confidence=_confidence(decision.get("confidence")),
            facts=_facts(decision.get("facts")),
        )
        routed.validate()
        return routed

    def contract_facts(self) -> dict[str, Any]:
        return request_router_contract()


def route_for_intent(intent: RequestIntent | str) -> RequestRoute:
    _parse_intent(intent)
    return RequestRoute.UNIFIED_LOOP


def request_router_contract() -> dict[str, Any]:
    return {
        "router": "request_router",
        "intent_owner": "model_or_structured_protocol",
        "system_role": "validate_intent_for_unified_loop",
        "allowed_intents": [str(item) for item in RequestIntent],
        "routes": {
            str(item): str(RequestRoute.UNIFIED_LOOP) for item in RequestIntent
        },
    }


def _parse_intent(value: Any) -> RequestIntent:
    raw = str(value or "").strip()
    for item in RequestIntent:
        if raw == str(item):
            return item
    raise ValueError(f"unsupported request intent: {raw}")


def _confidence(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(parsed, 1.0))


def _facts(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
