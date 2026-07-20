from __future__ import annotations

from typing import Any

from ..account_usage import fetch_account_usage
from ..capabilities_types import (
    BaseCapability,
    CapabilityContext,
    CapabilityResult,
    capability,
)
from .helpers import arg_text as _arg_text


@capability("account_usage")
class AccountUsageCapability(BaseCapability):
    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        provider = _arg_text(args, "provider") or "openai-codex"
        timeout_seconds = _timeout_seconds(args.get("timeout_seconds"))
        snapshot = fetch_account_usage(
            provider,
            home=self.home,
            timeout_seconds=timeout_seconds,
        )
        facts = snapshot.to_facts()
        return CapabilityResult(
            ok=True,
            action="account_usage",
            facts=facts,
            terminal=False,
        )


def _timeout_seconds(value: Any) -> float:
    try:
        return max(1.0, min(30.0, float(value)))
    except (TypeError, ValueError):
        return 15.0
