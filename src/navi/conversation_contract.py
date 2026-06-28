from __future__ import annotations

CONVERSATION_ACTION_CHAT = "chat"
CONVERSATION_ACTION_ASK = "ask"
CONVERSATION_TOOL_ASK = "ask.user"
CONVERSATION_ASK_ACTIONS = frozenset(
    {
        CONVERSATION_ACTION_ASK,
        CONVERSATION_TOOL_ASK,
    }
)
