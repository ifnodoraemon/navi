from __future__ import annotations

from typing import Any

from ..capabilities_types import (
    BaseCapability,
    CapabilityContext,
    CapabilityResult,
    capability,
)
from ..metrics import MetricsProjector
from .helpers import fact_result as _fact_result
from .helpers import transition_facts as _transition_facts


@capability("system_metrics")
class SystemMetricsCapability(BaseCapability):
    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        snapshot = MetricsProjector(self.home).snapshot()
        facts = {
            **_transition_facts("system_metrics", "current", "observed"),
            **snapshot.to_dict(),
        }
        return _fact_result("system_metrics", facts)
