from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .prompt_os import (
    assemble_fact_response_system_prompt,
    assemble_fact_response_turn_input,
    assemble_notification_system_prompt,
    assemble_notification_turn_input,
)
from .provider import ChatMessage
from .safeguards import redact_secrets, redact_secrets_deep


@dataclass(frozen=True)
class NotificationDecision:
    notify: bool
    message: str
    verified_facts: dict[str, Any]


async def synthesize_user_reply_from_facts(
    runtime: Any,
    *,
    user_text: str,
    facts: dict[str, Any],
) -> str:
    """Create user-facing text from verified runtime facts only."""
    return await runtime.complete(
        [
            ChatMessage("system", assemble_fact_response_system_prompt().render()),
            ChatMessage(
                "user",
                assemble_fact_response_turn_input(
                    user_text=user_text,
                    facts=facts,
                ).render(),
            ),
        ],
        role="responder",
    )


async def synthesize_background_notification(
    runtime: Any,
    *,
    facts: dict[str, Any],
    output_schema: dict[str, Any],
) -> NotificationDecision:
    """Let the notification model decide whether and how to surface an event."""
    verified_facts = redact_secrets_deep(facts)
    response = await runtime.provider.complete_for(
        "notification",
        [
            ChatMessage(
                role="system",
                content=assemble_notification_system_prompt().render(),
            ),
            ChatMessage(
                role="user",
                content=assemble_notification_turn_input(facts=verified_facts).render(),
            ),
        ],
        output_schema=output_schema,
    )
    decision = json.loads(response)
    notify = decision.get("notify")
    message = decision.get("message")
    if not isinstance(notify, bool) or not isinstance(message, str):
        raise ValueError("notification decision has invalid field types")
    return NotificationDecision(
        notify=notify,
        message=redact_secrets(message.strip()),
        verified_facts=verified_facts,
    )
