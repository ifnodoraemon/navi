from __future__ import annotations

from typing import Any

from ..capabilities_types import (
    BaseCapability,
    CapabilityContext,
    CapabilityResult,
    capability,
)
from ..identity import IdentityStore
from ..result import Conflict, SchemaMismatch, guarded
from .helpers import arg_text as _arg_text
from .helpers import fact_result as _fact_result
from .helpers import transition_facts as _transition_facts


@capability("identity_state")
class IdentityStateCapability(BaseCapability):
    @guarded
    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        store = IdentityStore(self.home)
        identity_id = store.resolve(
            source=context.source,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
        )
        aliases = store.aliases(identity_id) if identity_id else ()
        facts = {
            **_transition_facts("identity", identity_id or "unlinked", "observed"),
            "linked": bool(identity_id),
            "identity_id": identity_id,
            "aliases": list(aliases),
        }
        return _fact_result("identity", facts, run_id=identity_id)


@capability("identity_link")
class IdentityLinkCapability(BaseCapability):
    @guarded
    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        operation = _arg_text(args, "operation").lower()
        if not all((context.source, context.peer_id, context.sender_id)):
            raise SchemaMismatch("identity.link requires the current channel identity")
        store = IdentityStore(self.home)
        try:
            if operation == "request":
                other_source = _arg_text(args, "other_source")
                other_peer_id = _arg_text(args, "other_peer_id")
                other_sender_id = _arg_text(args, "other_sender_id")
                if not all((other_source, other_peer_id, other_sender_id)):
                    raise SchemaMismatch(
                        "identity.link request requires the complete target channel identity"
                    )
                request = store.request_link(
                    current_source=context.source,
                    current_peer_id=context.peer_id,
                    current_sender_id=context.sender_id,
                    other_source=other_source,
                    other_peer_id=other_peer_id,
                    other_sender_id=other_sender_id,
                )
                facts = {
                    **_transition_facts("identity", request.request_id, "confirmation_pending"),
                    "request_id": request.request_id,
                    "verification_code": request.verification_code,
                    "expires_at": request.expires_at,
                    "reason": _arg_text(args, "reason"),
                }
                return _fact_result("identity", facts, run_id=request.request_id)
            if operation == "confirm":
                linked = store.confirm_link(
                    source=context.source,
                    peer_id=context.peer_id,
                    sender_id=context.sender_id,
                    verification_code=_arg_text(args, "verification_code"),
                )
            elif operation == "unlink":
                removed = store.unlink_current(
                    source=context.source,
                    peer_id=context.peer_id,
                    sender_id=context.sender_id,
                )
                facts = {
                    **_transition_facts("identity", "current_alias", "unlinked"),
                    "unlinked": removed,
                    "reason": _arg_text(args, "reason"),
                }
                return _fact_result("identity", facts)
            else:
                raise SchemaMismatch(
                    "identity.link operation must be request, confirm, or unlink"
                )
        except ValueError as exc:
            if "different people" in str(exc):
                raise Conflict(str(exc)) from exc
            raise SchemaMismatch(str(exc)) from exc
        facts = {
            **_transition_facts("identity", linked.identity_id, "aliases_linked"),
            "identity_id": linked.identity_id,
            "aliases": list(linked.aliases),
            "migrated_memory_count": linked.migrated_memory_count,
            "reason": _arg_text(args, "reason"),
        }
        return _fact_result("identity", facts, run_id=linked.identity_id)
