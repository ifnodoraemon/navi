from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..capabilities_types import CapabilityContext, CapabilityResult
from ..tools import ToolSpec
from ..trace import TraceStore
from .helpers import arg_text as _arg_text
from .helpers import fact_result as _fact_result
from .helpers import transition_facts as _transition_facts


class TraceEvaluateCapability:
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
        trace_id = _arg_text(args, "trace_id")
        if not trace_id:
            return CapabilityResult(
                ok=False,
                action="trace",
                observation="trace.evaluate requires trace_id.",
                message="trace.evaluate requires trace_id.",
                terminal=False,
                error_reason="schema_mismatch",
            )
        evaluation = TraceStore(self.home).evaluate_trace(trace_id)
        evaluation_facts = asdict(evaluation)
        facts = {
            **_transition_facts("trace_evaluation", evaluation.id, "created"),
            "trace_id": trace_id,
            "evaluation_id": evaluation.id,
            "evaluation": evaluation_facts,
        }
        return _fact_result("trace", facts, run_id=trace_id)
