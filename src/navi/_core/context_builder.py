from navi.lifecycle import Phase, Governance, Acceptance, Resolution
import json
from pathlib import Path
from typing import Any, Tuple, List, Dict

from ..capabilities import CapabilityContext
from ..control import CurrentStateBuilder, SurfaceContext, current_state_facts
from ..loop import TracePhase
from ..runs import RunStore
from ..operating_context import max_permission

_CONVERSATION_CONTEXT_MESSAGE_LIMIT = 100

def _observation_event(kind: str, facts: Dict[str, Any]) -> str:
    return json.dumps(
        {
            "observation_type": kind,
            "facts": facts,
        },
        ensure_ascii=False,
        sort_keys=True,
    )

def _dynamic_intent_facts(intent_facts: Dict[str, Any] | None) -> Dict[str, Any]:
    if not intent_facts:
        return {}
    facts = dict(intent_facts)
    if (
        facts.get("source_agent") == "intent_agent"
        and facts.get("intent_basis") == "current_state_facts"
    ):
        facts.pop("current_state", None)
        if set(facts) <= {"source_agent", "intent_basis"}:
            return {}
    return facts

class ContextBuilder:
    def __init__(
        self,
        home: Path,
        project_dir: Path,
        runtime: Any,
        trace: Any,
        permission_ceiling: str,
        event_bus: Any | None,
        context_manager: Any,
    ):
        self.home = home
        self.project_dir = project_dir
        self.runtime = runtime
        self.trace = trace
        self.permission_ceiling = permission_ceiling
        self.event_bus = event_bus
        self.context_manager = context_manager

    def conversation_context(self, session_id: str | None) -> str:
        if not session_id:
            return ""
        messages = self.runtime.memory.get_messages(session_id, limit=_CONVERSATION_CONTEXT_MESSAGE_LIMIT)
        return self.context_manager.build_conversation_context(messages)

    def _get_effective_permission_ceiling(
        self,
        *,
        source: str,
        peer_id: str,
        sender_id: str,
    ) -> str:
        approval = RunStore(self.home).active_session_elevation(
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
        )
        if approval is None or not approval.requested_permission:
            return self.permission_ceiling
        return max_permission(self.permission_ceiling, approval.requested_permission)

    def initialize_turn(
        self,
        text: str,
        peer_id: str,
        sender_id: str,
        source: str,
        session_id: str | None,
        session_alias: str | None,
        intent_facts: Dict[str, Any] | None,
        trace_id: str | None = None,
    ) -> Tuple[str, str, CapabilityContext, SurfaceContext, List[str]]:
        resolved_session_id = session_id
        if not resolved_session_id and session_alias:
            resolved_session_id = self.runtime.memory.current_session_id(session_alias)
        trace_id = trace_id or self.trace.new_trace_id()
        self.trace.add_event(
            trace_id=trace_id,
            phase=TracePhase.TURN_START,
            session_id=resolved_session_id or "",
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            input_data={"message": text, "session_alias": session_alias or ""},
        )

        context = CapabilityContext(
            home=self.home,
            peer_id=peer_id,
            sender_id=sender_id,
            source=source,
            permission_ceiling=self._get_effective_permission_ceiling(
                source=source,
                peer_id=peer_id,
                sender_id=sender_id,
            ),
            workspace=str(self.project_dir.resolve()),
            session_id=resolved_session_id,
            trace_id=trace_id,
            input_text=text,
            event_bus=self.event_bus,
        )

        observations: List[str] = []
        state_context = SurfaceContext(
            home=self.home,
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            session_id=resolved_session_id,
            workspace=context.workspace,
            input_text=text,
        )
        current_state = CurrentStateBuilder(self.home).build(state_context)
        observations.append(
            _observation_event("current_state", current_state_facts(current_state))
        )
        dynamic_intent_facts = _dynamic_intent_facts(intent_facts)
        if dynamic_intent_facts:
            observations.append(
                _observation_event("dynamic_intent", dynamic_intent_facts)
            )

        return resolved_session_id, trace_id, context, state_context, observations
