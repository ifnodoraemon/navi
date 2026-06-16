from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..capabilities_types import CapabilityContext, CapabilityResult
from ..memory import MemoryStore
from ..tools import ToolSpec
from .helpers import arg_text as _arg_text
from .helpers import fact_result as _fact_result
from .helpers import transition_facts as _transition_facts


class MemoryAddCapability:
    def __init__(self, spec: ToolSpec, *, home: Path):
        self.spec = spec
        self.home = home

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
            return CapabilityResult(
                ok=False,
                action="memory",
                observation="memory.add requires type and content.",
                message="memory.add requires type and content.",
                terminal=False,
                error_reason="schema_mismatch",
            )
        metadata = args.get("metadata") if isinstance(args.get("metadata"), dict) else {}
        reason = _arg_text(args, "reason")
        provenance = _arg_text(args, "provenance")
        if not reason or not provenance:
            return CapabilityResult(
                ok=False,
                action="memory",
                observation="memory.add requires reason and provenance.",
                message="memory.add requires reason and provenance.",
                terminal=False,
                error_reason="schema_mismatch",
            )
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
            return CapabilityResult(
                ok=False,
                action="memory",
                observation=str(exc),
                message=str(exc),
                terminal=False,
                error_reason="invalid_operation",
            )
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
