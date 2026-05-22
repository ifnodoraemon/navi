from __future__ import annotations


def truncate_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head_limit = max(1, limit // 2)
    tail_limit = max(1, limit - head_limit)
    omitted = len(text) - head_limit - tail_limit
    return (
        f"{text[:head_limit]}\n"
        f"... [truncated {omitted} chars from the middle] ...\n"
        f"{text[-tail_limit:]}"
    )
