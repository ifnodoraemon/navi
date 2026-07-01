from __future__ import annotations

from typing import Any

from ..capabilities_types import (
    BaseCapability,
    CapabilityContext,
    CapabilityResult,
    capability,
)
from ..conversation_contract import CONVERSATION_ACTION_ASK, CONVERSATION_ACTION_CHAT
from ..result import SchemaMismatch
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
        if not message:
            raise SchemaMismatch("Missing or empty 'message' argument. You must provide the actual text to send to the user.")
        return CapabilityResult(
            ok=True,
            action=CONVERSATION_ACTION_CHAT,
            observation=message,
            message=message,
            terminal=True,
        )


_EMOJI_NUMBERS = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟")

def _number_emoji(n: int) -> str:
    return _EMOJI_NUMBERS[n - 1] if 1 <= n <= len(_EMOJI_NUMBERS) else f"[{n}]"


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
        if not message:
            raise SchemaMismatch("Missing or empty 'message' argument. You must provide the actual question to ask the user.")
        options = args.get("options")
        facts = {}
        if isinstance(options, list) and options:
            # Always expose options as a structured fact (rich surfaces like the
            # CLI render a menu from it) and always inline them into the message
            # text (the only field plain text channels deliver). The capability
            # must not branch on the surface name (principle 4).
            facts["options"] = options
            
            compact_options = " / ".join(f"{i+1}.{opt}" for i, opt in enumerate(options))
            message = f"[待选择: {compact_options}]\n\n{message}"
            
            numbered = "\n".join(f"  {_number_emoji(i + 1)}  {opt}" for i, opt in enumerate(options))
            message += f"\n\n{numbered}\n\n💬 回复数字选择，或直接说明你的想法"

        return CapabilityResult(
            ok=True,
            action=CONVERSATION_ACTION_ASK,
            observation=message,
            message=message,
            terminal=True,
            facts=facts,
            yields_control=True,
        )
