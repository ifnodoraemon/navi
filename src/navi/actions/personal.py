from __future__ import annotations

from typing import Any

from ..capabilities_types import (
    BaseCapability,
    CapabilityContext,
    CapabilityResult,
    capability,
)
from ..memory.scopes import default_memory_scope, memory_scopes_for_context
from ..personal_resources import PersonalResourceConflict, PersonalResourceStore
from ..result import Conflict, NotFound, SchemaMismatch, guarded
from .helpers import arg_text as _arg_text
from .helpers import fact_result as _fact_result
from .helpers import transition_facts as _transition_facts


@capability("personal_query")
class PersonalQueryCapability(BaseCapability):
    @guarded
    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        store = PersonalResourceStore(self.home)
        owner_scopes = _owner_scopes(context)
        resource_id = _arg_text(args, "resource_id")
        if resource_id:
            item = store.get(resource_id, owner_scopes=owner_scopes)
            if item is None:
                raise NotFound("personal resource not found")
            resources = [item]
        else:
            raw_kinds = args.get("kinds")
            kinds = (
                tuple(str(item).strip() for item in raw_kinds if str(item).strip())
                if isinstance(raw_kinds, list)
                else ()
            )
            try:
                resources = store.query(
                    owner_scopes=owner_scopes,
                    kinds=kinds,
                    query=_arg_text(args, "query"),
                    include_deleted=bool(args.get("include_deleted", False)),
                    limit=_positive_limit(args.get("limit")),
                )
            except ValueError as exc:
                raise SchemaMismatch(str(exc)) from exc
        facts = {
            **_transition_facts("personal_resource_collection", "current_actor", "observed"),
            "resources": [item.to_dict() for item in resources],
            "count": len(resources),
            "supported_kinds": list(store.adapters.kinds()),
            "mail_delivery_supported": False,
        }
        return _fact_result("personal_resource", facts)


@capability("personal_update")
class PersonalUpdateCapability(BaseCapability):
    @guarded
    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        operation = _arg_text(args, "operation").lower()
        if operation not in {"create", "update", "complete", "delete"}:
            raise SchemaMismatch(
                "personal.update operation must be create, update, complete, or delete"
            )
        store = PersonalResourceStore(self.home)
        owner_scopes = _owner_scopes(context)
        raw_data = args.get("data")
        data: dict[str, Any] = dict(raw_data) if isinstance(raw_data, dict) else {}
        try:
            if operation == "create":
                kind = _arg_text(args, "kind")
                if not kind:
                    raise SchemaMismatch("personal.update create requires kind")
                item = store.create(
                    kind=kind,
                    owner_scope=default_memory_scope(
                        source=context.source,
                        peer_id=context.peer_id,
                        sender_id=context.sender_id,
                        session_id=context.session_id or "",
                        workspace=context.workspace,
                        home=self.home,
                    ),
                    data=data,
                )
                transition = "created"
            else:
                resource_id = _arg_text(args, "resource_id")
                if not resource_id:
                    raise SchemaMismatch(
                        f"personal.update {operation} requires resource_id"
                    )
                expected_version = args.get("expected_version")
                if not isinstance(expected_version, int) or expected_version < 1:
                    raise SchemaMismatch(
                        "personal.update requires positive expected_version for mutation"
                    )
                target_status = {
                    "update": "",
                    "complete": "completed",
                    "delete": "deleted",
                }[operation]
                item = store.update(
                    resource_id,
                    owner_scopes=owner_scopes,
                    patch=data if operation == "update" else {},
                    expected_version=expected_version,
                    status=target_status,
                )
                transition = {
                    "update": "updated",
                    "complete": "completed",
                    "delete": "deleted",
                }[operation]
        except PersonalResourceConflict as exc:
            raise Conflict(str(exc)) from exc
        except KeyError as exc:
            raise NotFound(str(exc)) from exc
        except ValueError as exc:
            raise SchemaMismatch(str(exc)) from exc
        verified = store.get(item.id, owner_scopes=owner_scopes)
        if verified is None or verified.version != item.version:
            raise Conflict("personal resource read-back verification failed")
        facts = {
            **_transition_facts("personal_resource", item.id, transition),
            "resource": item.to_dict(),
            "verified_after": verified.to_dict(),
            "mail_delivery_supported": False,
        }
        return _fact_result("personal_resource", facts, run_id=item.id)


def _owner_scopes(context: CapabilityContext) -> set[str]:
    return {
        scope
        for scope in memory_scopes_for_context(
            source=context.source,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
            session_id=context.session_id or "",
            workspace=context.workspace,
            home=context.home,
        )
        if scope.startswith(("person:", "actor:"))
    }


def _positive_limit(value: Any) -> int:
    try:
        return max(1, min(int(value), 200))
    except (TypeError, ValueError):
        return 50
