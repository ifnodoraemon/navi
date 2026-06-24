from __future__ import annotations

from typing import Any

from ..capabilities_types import (
    BaseCapability,
    CapabilityContext,
    CapabilityResult,
    capability,
)
from .helpers import arg_text as _arg_text


@capability("final_answer")
class FinalAnswerCapability(BaseCapability):
    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        message = _arg_text(args, "message")
        return CapabilityResult(
            ok=True, action="chat", observation=message, message=message, terminal=True
        )


@capability("clarify")
class ClarifyCapability(BaseCapability):

    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        message = _arg_text(args, "message")
        options = args.get("options")
        facts = {}
        if isinstance(options, list) and options:
            # Always expose options as a structured fact (rich surfaces like the
            # CLI render a menu from it) and always inline them into the message
            # text (the only field plain text channels deliver). The capability
            # must not branch on the surface name (principle 4).
            facts["options"] = options
            message += "\n" + "\n".join(f"[{i + 1}] {opt}" for i, opt in enumerate(options))

        return CapabilityResult(
            ok=True, action="ask", observation=message, message=message, terminal=True, facts=facts
        )
