from __future__ import annotations

from dataclasses import asdict
from typing import Any

from ..capabilities_types import (
    BaseCapability,
    CapabilityContext,
    CapabilityResult,
    capability,
)
from ..memory import MemoryStore
from ..memory.scopes import (
    default_memory_scope,
    resolve_memory_scope,
    writable_memory_scopes_for_context,
)
from ..result import PermissionDenied, SchemaMismatch, guarded
from ..tools import API_CONTEXT
from .helpers import arg_text as _arg_text
from .helpers import fact_result as _fact_result
from .helpers import transition_facts as _transition_facts


@capability("memory_add")
class MemoryAddCapability(BaseCapability):

    @guarded
    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        operation = _arg_text(args, "operation").lower() or "add"
        if operation not in {"add", "revoke"}:
            raise SchemaMismatch("memory.add operation must be add or revoke.")
        local_admin = _is_local_memory_admin(context)
        allowed_scopes = set(
            writable_memory_scopes_for_context(
                source=context.source,
                peer_id=context.peer_id,
                sender_id=context.sender_id,
                session_id=context.session_id or "",
                workspace=context.workspace,
                allow_global=local_admin,
            )
        )
        if operation == "revoke":
            memory_id = _arg_text(args, "memory_id")
            if not memory_id:
                raise SchemaMismatch("memory.add revoke requires memory_id.")
            store = MemoryStore(self.home)
            current = store.get_item(memory_id)
            if current is None:
                from ..result import NotFound

                raise NotFound("memory item not found.")
            if not local_admin and current.scope not in allowed_scopes:
                raise PermissionDenied("memory item scope is outside the caller envelope.")
            item = store.set_status(memory_id, "revoked")
            assert item is not None
            facts = {
                **_transition_facts("memory_item", item.id, "revoked"),
                "memory_id": item.id,
                "item": asdict(item),
                "reason": _arg_text(args, "reason"),
                "provenance": _arg_text(args, "provenance"),
            }
            return _fact_result("memory", facts, run_id=item.id)

        memory_type = _arg_text(args, "type")
        content = _arg_text(args, "content")
        if not memory_type or not content:
            raise SchemaMismatch("memory.add requires type and content.")
        metadata = args.get("metadata") if isinstance(args.get("metadata"), dict) else {}
        reason = _arg_text(args, "reason")
        provenance = _arg_text(args, "provenance")
        if not reason or not provenance:
            raise SchemaMismatch("memory.add requires reason and provenance.")
        requested_scope = _arg_text(args, "scope")
        scope = (
            resolve_memory_scope(
                requested_scope,
                source=context.source,
                peer_id=context.peer_id,
                sender_id=context.sender_id,
                session_id=context.session_id or "",
                workspace=context.workspace,
            )
            if requested_scope
            else default_memory_scope(
                source=context.source,
                peer_id=context.peer_id,
                sender_id=context.sender_id,
                session_id=context.session_id or "",
                workspace=context.workspace,
            )
        )
        if scope not in allowed_scopes:
            raise PermissionDenied("memory.add scope is outside the caller policy envelope.")
        try:
            item = MemoryStore(self.home).add_item(
                memory_type,
                content,
                source=_arg_text(args, "source") or context.source or "api",
                scope=scope,
                status=_arg_text(args, "status") or "proposed",
                confidence=_confidence(args.get("confidence")),
                metadata=metadata,
                reason=reason,
                provenance=provenance,
            )
        except ValueError as exc:
            raise SchemaMismatch(str(exc)) from exc
        item_facts = asdict(item)
        facts = {
            **_transition_facts("memory_item", item.id, "created"),
            "memory_id": item.id,
            "item": item_facts,
        }
        facts["reason"] = reason
        facts["provenance"] = provenance
        return _fact_result(
            "memory",
            facts,
            run_id=item.id,
        )


def _confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.5


def _is_local_memory_admin(context: CapabilityContext) -> bool:
    return (
        context.execution_context == API_CONTEXT
        and context.source in {"cli", "local"}
        and context.peer_id == context.source
        and context.sender_id == context.source
    )
