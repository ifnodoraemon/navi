from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .loop_contracts import RequestIntent, RequestRoute
from .provider import ChatMessage, ModelPool


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


class ModelRequestRouter:
    """LLM-backed intake validator that must output the same explicit contract."""

    def __init__(self, provider: ModelPool):
        self.provider = provider
        self.validator = RequestRouter()

    async def route(
        self,
        text: str,
        *,
        current_state: dict[str, Any],
        connector_facts: dict[str, Any] | None = None,
    ) -> RequestRoutingDecision:
        response = await self.provider.complete_for(
            "router",
            [
                ChatMessage(
                    "system",
                    (
                        "You are Navi's unified loop intake validator. Classify the intent according to the provided schema. "
                        "Do not answer the user and do not select capabilities."
                    ),
                ),
                ChatMessage(
                    "user",
                    json.dumps(
                        {
                            "user_request": text,
                            "current_state": current_state,
                            "connector_facts": connector_facts or {},
                            "contract": request_router_contract(),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ),
            ],
            output_schema=_routing_output_schema(),
        )
        try:
            parsed = json.loads(response)
        except json.JSONDecodeError as exc:
            raise ValueError("router returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("router JSON was not an object")
        return self.validator.route_model_decision(parsed)


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


def _routing_output_schema() -> dict[str, Any]:
    return {
        "name": "request_routing_decision",
        "strict": False,
        "schema": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": [str(item) for item in RequestIntent],
                },
                "reason": {"type": "string"},
                "confidence": {"type": "number"},
                "goal_id": {"type": "string"},
                "facts": {"type": "object"},
            },
            "required": ["intent", "reason", "confidence", "facts"],
            "additionalProperties": False,
        },
    }
