from __future__ import annotations

from typing import Any

from ..capabilities_types import (
    BaseCapability,
    CapabilityContext,
    CapabilityResult,
    capability,
)
from ..result import SchemaMismatch, guarded
from ..trace import TraceStore
from .helpers import arg_text as _arg_text
from .helpers import fact_result as _fact_result
from .helpers import transition_facts as _transition_facts


@capability("trace_evaluate")
class TraceEvaluateCapability(BaseCapability):

    @guarded
    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        trace_id = _arg_text(args, "trace_id")
        if not trace_id:
            raise SchemaMismatch("trace.evaluate requires trace_id.")
        evaluation = TraceStore(self.home).evaluate_trace(trace_id)
        facts = {
            **_transition_facts("trace_evaluation", evaluation.id, "created"),
            "trace_id": trace_id,
            "evaluation_id": evaluation.id,
            "evaluation": evaluation.to_dict(),
        }
        return _fact_result("trace", facts, run_id=trace_id)


@capability("trace_delete")
class TraceDeleteCapability(BaseCapability):

    @guarded
    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        trace_id = _arg_text(args, "trace_id")
        delete_all = args.get("all") is True
        if bool(trace_id) == delete_all:
            raise SchemaMismatch("trace.delete requires exactly one of trace_id or all=true.")
        specs = {
            True: (None, "all", "trace_collection", "all"),
            False: (trace_id, trace_id, "trace", "single"),
        }
        delete_arg, entity_id, entity_type, scope = specs[delete_all]
        deletion = TraceStore(self.home).delete_traces(delete_arg)
        facts = {
            **_transition_facts(entity_type, entity_id, "deleted"),
            "scope": scope,
            **deletion,
        }
        return _fact_result("trace", facts, run_id=trace_id)
