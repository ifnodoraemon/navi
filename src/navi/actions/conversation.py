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


@capability("respond")
class RespondCapability(BaseCapability):

    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        message = _arg_text(args, "message")
        if not message:
            raise SchemaMismatch("respond requires message")
        options = args.get("options")
        has_options = isinstance(options, list) and len(options) > 0
        private_evidence = args.get("private_evidence")
        facts: dict[str, Any] = {}

        if has_options:
            facts["options"] = options
        if isinstance(private_evidence, dict) and private_evidence:
            facts["private_evidence"] = private_evidence
            facts["private_evidence_provenance"] = "respond.private_evidence"

        return CapabilityResult(
            ok=True,
            action=CONVERSATION_ACTION_CHAT,
            message=message,
            terminal=True,
            facts=facts or None,
        )


@capability("ask_user")
class AskUserCapability(BaseCapability):
    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        message = _arg_text(args, "message")
        if not message:
            raise SchemaMismatch("ask.user requires message")
        options = args.get("options")
        facts: dict[str, Any] = {}
        if isinstance(options, list) and options:
            facts["options"] = options
        return CapabilityResult(
            ok=True,
            action=CONVERSATION_ACTION_ASK,
            message=message,
            terminal=True,
            yields_control=True,
            facts=facts,
        )
