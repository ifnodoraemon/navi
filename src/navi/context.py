"""Context management for Navi conversation history.

Provides token-budget-aware conversation context building that replaces
the previous hardcoded ``limit=8`` message retrieval.  Supports CJK-aware
token estimation and a sliding window that keeps the most recent turns
verbatim while condensing older messages.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Protocol

__all__ = ["ContextManager"]

# ---------------------------------------------------------------------------
# CJK detection helpers
# ---------------------------------------------------------------------------

# Regex matching a single CJK Unified Ideograph (common + extension A/B).
_CJK_RE = re.compile(
    r"[\u4e00-\u9fff"     # CJK Unified Ideographs
    r"\u3400-\u4dbf"      # CJK Unified Ideographs Extension A
    r"\U00020000-\U0002a6df"  # CJK Unified Ideographs Extension B
    r"\u3000-\u303f"      # CJK Symbols and Punctuation
    r"\uff00-\uffef"      # Halfwidth and Fullwidth Forms
    r"\u3040-\u309f"      # Hiragana
    r"\u30a0-\u30ff]"     # Katakana
)

_CJK_THRESHOLD = 0.30  # Fraction of CJK chars to trigger CJK mode.


def is_cjk_char(ch: str) -> bool:
    """Return *True* if *ch* is a CJK ideograph or kana character."""
    return bool(_CJK_RE.match(ch))


def cjk_ratio(text: str) -> float:
    """Return the fraction of characters in *text* that are CJK."""
    if not text:
        return 0.0
    cjk_count = sum(1 for ch in text if is_cjk_char(ch))
    return cjk_count / len(text)


def is_cjk_heavy(text: str) -> bool:
    """Return *True* if more than 30 % of *text* consists of CJK characters."""
    return cjk_ratio(text) > _CJK_THRESHOLD


# ---------------------------------------------------------------------------
# Minimal structural protocol for stored messages
# ---------------------------------------------------------------------------

class _MessageLike(Protocol):
    """Minimal interface expected from memory message objects."""

    @property
    def role(self) -> str: ...

    @property
    def content(self) -> str: ...


# ---------------------------------------------------------------------------
# ContextManager
# ---------------------------------------------------------------------------

class ContextManager:
    """Build token-budget-aware conversation context for the engine.

    Parameters
    ----------
    max_context_tokens:
        Hard ceiling on the total tokens the assembled context may occupy.
    recent_turns:
        Number of most-recent messages to keep **verbatim**.
    summary_budget_tokens:
        Token budget reserved for the condensed summary of older messages.
    """

    def __init__(
        self,
        *,
        max_context_tokens: int = 80_000,
        recent_turns: int = 6,
        summary_budget_tokens: int = 2_000,
    ) -> None:
        self.max_context_tokens = max_context_tokens
        self.recent_turns = recent_turns
        self.summary_budget_tokens = summary_budget_tokens

    # ------------------------------------------------------------------
    # Token counting
    # ------------------------------------------------------------------

    @staticmethod
    def count_tokens(text: str) -> int:
        """Estimate the number of tokens in *text*.

        * ASCII-heavy text: ``len(text) // 4``
        * CJK-heavy text (>30 % CJK chars): ``len(text) // 2``

        This is a fast heuristic; it intentionally avoids importing a
        tokenizer so the module stays lightweight.
        """
        if not text:
            return 0
        if is_cjk_heavy(text):
            return max(1, len(text) // 2)
        return max(1, len(text) // 4)

    # ------------------------------------------------------------------
    # Context assembly
    # ------------------------------------------------------------------

    def build_conversation_context(
        self,
        messages: list[Any],
        *,
        system_budget: int = 4_000,
    ) -> str:
        """Assemble a conversation context string from *messages*.

        Parameters
        ----------
        messages:
            A list of objects with ``.role`` and ``.content`` attributes
            (typically ``StoredMessage`` instances from Navi's memory layer).
        system_budget:
            Tokens reserved for the system prompt; subtracted from
            ``max_context_tokens`` to obtain the available context window.

        Returns
        -------
        str
            A formatted conversation context string ready for inclusion in
            the prompt.
        """
        if not messages:
            return ""

        available_tokens = self.max_context_tokens - system_budget

        # -- 1. Recent turns (verbatim) ----------------------------------
        recent = messages[-self.recent_turns:] if len(messages) > self.recent_turns else messages
        older = messages[:-self.recent_turns] if len(messages) > self.recent_turns else []

        recent_lines: list[str] = []
        recent_token_total = 0
        for msg in recent:
            line = f"{msg.role}: {msg.content}"
            recent_token_total += self.count_tokens(line)
            recent_lines.append(line)

        # If even the recent messages exceed the budget, truncate from the
        # oldest of the recent messages until we fit.
        while recent_lines and recent_token_total > available_tokens:
            removed = recent_lines.pop(0)
            recent_token_total -= self.count_tokens(removed)

        # -- 2. Older messages (condensed) --------------------------------
        remaining_budget = available_tokens - recent_token_total
        condensed_budget = min(remaining_budget, self.summary_budget_tokens)

        condensed_lines: list[str] = []
        condensed_tokens = 0
        for msg in older:
            # Produce a short one-liner per message: keep the first 120 chars.
            snippet = msg.content[:120].replace("\n", " ")
            if len(msg.content) > 120:
                snippet += "…"
            line = f"[{msg.role}] {snippet}"
            line_tokens = self.count_tokens(line)
            if condensed_tokens + line_tokens > condensed_budget:
                break
            condensed_lines.append(line)
            condensed_tokens += line_tokens

        # -- 3. Assemble --------------------------------------------------
        parts: list[str] = []
        if condensed_lines:
            parts.append("--- earlier context (condensed) ---")
            parts.extend(condensed_lines)
            parts.append("--- recent messages ---")
        parts.extend(recent_lines)
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Prompt-level estimation
    # ------------------------------------------------------------------

    def estimate_prompt_tokens(self, system: str, user: str, context: str) -> int:
        """Estimate the total token cost of the three major prompt sections."""
        return (
            self.count_tokens(system)
            + self.count_tokens(user)
            + self.count_tokens(context)
        )
