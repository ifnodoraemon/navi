from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any


DELIVERY_FACT_KEY = "connector_delivery"
DELIVERY_CONTRACT_VERSION = "1"


@dataclass(frozen=True)
class ConnectorDelivery:
    """Connector-neutral request for one synchronous outbound delivery.

    The agent kernel produces this contract.  The active connector consumes it
    immediately and is responsible for recording the real transport receipt.
    No connector-specific queue or outbox is part of the contract.
    """

    path: str
    text: str = ""
    delivery_id: str = ""
    run_id: str = ""
    goal_id: str = ""
    channel: str = "current"
    kind: str = "file"
    mode: str = "synchronous"
    version: str = DELIVERY_CONTRACT_VERSION

    def to_dict(self) -> dict[str, str]:
        return {
            "version": self.version,
            "delivery_id": self.delivery_id,
            "kind": self.kind,
            "mode": self.mode,
            "channel": self.channel,
            "path": self.path,
            "text": self.text,
            "run_id": self.run_id,
            "goal_id": self.goal_id,
        }

    def bind(
        self,
        *,
        delivery_id: str = "",
        run_id: str = "",
        goal_id: str = "",
    ) -> "ConnectorDelivery":
        return replace(
            self,
            delivery_id=self.delivery_id or delivery_id,
            run_id=self.run_id or run_id,
            goal_id=self.goal_id or goal_id,
        )


def connector_delivery_from_facts(facts: Any) -> ConnectorDelivery | None:
    if not isinstance(facts, dict):
        return None
    raw = facts.get(DELIVERY_FACT_KEY)
    if not isinstance(raw, dict):
        return None
    path = str(raw.get("path") or "").strip()
    kind = str(raw.get("kind") or "").strip()
    mode = str(raw.get("mode") or "").strip()
    channel = str(raw.get("channel") or "").strip()
    version = str(raw.get("version") or "").strip()
    if (
        not path
        or kind != "file"
        or mode != "synchronous"
        or channel != "current"
        or version != DELIVERY_CONTRACT_VERSION
    ):
        return None
    return ConnectorDelivery(
        path=path,
        text=str(raw.get("text") or ""),
        delivery_id=str(raw.get("delivery_id") or ""),
        run_id=str(raw.get("run_id") or ""),
        goal_id=str(raw.get("goal_id") or ""),
        channel=channel,
        kind=kind,
        mode=mode,
        version=version,
    )


def bind_connector_delivery_facts(
    facts: dict[str, Any],
    *,
    delivery_id: str = "",
    run_id: str = "",
    goal_id: str = "",
) -> dict[str, Any]:
    delivery = connector_delivery_from_facts(facts)
    if delivery is None:
        return dict(facts)
    bound = delivery.bind(
        delivery_id=delivery_id,
        run_id=run_id,
        goal_id=goal_id,
    )
    return {**facts, DELIVERY_FACT_KEY: bound.to_dict()}


def connector_delivery_from_loop_result(result: Any) -> ConnectorDelivery | None:
    state_graph_result = getattr(result, "state_graph_result", None)
    evidence = getattr(state_graph_result, "evidence", None) or {}
    capability_result = evidence.get("capability_result")
    if not isinstance(capability_result, dict):
        return None
    delivery = connector_delivery_from_facts(capability_result.get("facts"))
    if delivery is None:
        return None
    loop_run = getattr(result, "loop_run", None)
    goal = getattr(result, "goal", None)
    run = getattr(result, "run", None)
    loop_run_id = str(getattr(loop_run, "run_id", "") or "")
    run_id = str(getattr(run, "id", "") or "")
    goal_id = str(getattr(goal, "id", "") or "")
    return replace(
        delivery,
        delivery_id=loop_run_id or run_id or delivery.delivery_id,
        run_id=run_id or delivery.run_id,
        goal_id=goal_id or delivery.goal_id,
    )


def connector_delivery_client_id(delivery_id: str, *, prefix: str) -> str:
    digest = hashlib.sha256(delivery_id.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-{digest}"


def register_connector_delivery_tool(registry: Any, *, home: Path) -> None:
    del home
    from .tools import ALL_EXECUTION_CONTEXTS, SideEffectPolicy, ToolSpec

    registry.register(
        ToolSpec(
            name="channel.send_file",
            capability_class="connector.outbound_media",
            execution_contexts=ALL_EXECUTION_CONTEXTS,
            description=(
                "Deliver one existing local file, with optional text, through the current "
                "interaction channel. The active connector executes this request synchronously "
                "after any required approval and reports the real transport result."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "text": {
                        "type": "string",
                        "description": "Optional text to send with the file.",
                    },
                },
                "required": ["path"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "entity_type": {"type": "string"},
                    "entity_id": {"type": "string"},
                    "state_transition": {"type": "string"},
                    "turn_scope": {"type": "string"},
                    "source_path": {"type": "string"},
                    "size": {"type": "integer"},
                    "connector_delivery": {"type": "object"},
                    "side_effect_scope": {"type": "string"},
                    "side_effect_state": {"type": "string"},
                    "side_effect_artifact": {"type": "string"},
                },
            },
            facts_only=True,
            mutates=True,
            permission="write",
            source="core.connector_delivery",
            side_effect_policy=SideEffectPolicy(
                scope="external",
                mode="synchronous",
                state_field="side_effect_state",
                artifact_field="source_path",
                description=(
                    "The active connector must complete the delivery synchronously; "
                    "there is no staged artifact or deferred commit."
                ),
            ),
        ),
        _send_file_handler,
    )


def _send_file_handler(args: dict[str, Any]):
    from .tools import ToolResult

    raw_path = str(args.get("path") or "").strip()
    if not raw_path:
        return ToolResult(tool="channel.send_file", ok=False, error="path is required")
    try:
        source = Path(raw_path).expanduser().resolve()
    except OSError as exc:
        return ToolResult(tool="channel.send_file", ok=False, error=str(exc))
    if not source.exists():
        return ToolResult(
            tool="channel.send_file",
            ok=False,
            error=f"file not found: {source}",
            facts={"source_path": str(source)},
        )
    if not source.is_file():
        return ToolResult(
            tool="channel.send_file",
            ok=False,
            error=f"path is not a file: {source}",
            facts={"source_path": str(source)},
        )

    text = str(args.get("text") or "")
    delivery = ConnectorDelivery(path=str(source), text=text)
    return ToolResult(
        tool="channel.send_file",
        ok=True,
        terminal=False,
        yields_control=True,
        action="connector_outbound",
        message=text,
        facts={
            "entity_type": "connector_delivery",
            "entity_id": str(source),
            "state_transition": "delivery_requested",
            "turn_scope": "current",
            "source_path": str(source),
            "size": source.stat().st_size,
            DELIVERY_FACT_KEY: delivery.to_dict(),
            "side_effect_scope": "external",
            "side_effect_state": "delivery_requested",
            "side_effect_artifact": str(source),
        },
    )
