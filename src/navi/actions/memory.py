from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..capabilities_types import (
    BaseCapability,
    CapabilityContext,
    CapabilityResult,
    capability,
)
from ..memory import MemoryStore
from ..result import SchemaMismatch, guarded
from ..tools import ToolSpec
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
        memory_type = _arg_text(args, "type")
        content = _arg_text(args, "content")
        if not memory_type or not content:
            raise SchemaMismatch("memory.add requires type and content.")
        metadata = args.get("metadata") if isinstance(args.get("metadata"), dict) else {}
        reason = _arg_text(args, "reason")
        provenance = _arg_text(args, "provenance")
        if not reason or not provenance:
            raise SchemaMismatch("memory.add requires reason and provenance.")
        try:
            item = MemoryStore(self.home).add_item(
                memory_type,
                content,
                source=_arg_text(args, "source") or context.source or "api",
                scope=_arg_text(args, "scope") or "global",
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
