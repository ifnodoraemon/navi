"""Connector-local prompt content for the Weixin adapter.

Principle 4 (Connector Agnostic Core): the core prompt spec must not know about
any channel. Weixin owns the text it uses to compose notifications instead of
registering a `weixin_notification` layer in the core PROMPT_LAYERS_SPEC.
"""

from __future__ import annotations

NOTIFICATION_SYSTEM_PROMPT = (
    "You are Navi composing a concise connector notification.\n"
    "Use only the supplied facts.\n"
    "Preserve task ids, approval codes, status, errors, and important result text.\n"
    "Do not mention connector internals, JSON, or hidden routers.\n"
)
