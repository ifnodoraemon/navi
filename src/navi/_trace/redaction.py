"""Trace redaction helpers."""

from __future__ import annotations

import json
from typing import Any


def _redact(value: Any) -> Any:
    from ..safeguards import redact_personal_data, _REDACT_FIELD_NAMES

    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if key_text in _REDACT_FIELD_NAMES:
                redacted[key] = "[redacted]"
            else:
                redacted[key] = _redact(item)
        return redacted
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return redact_personal_data(value)
    return value


def _redact_json_text(text: str) -> str:
    try:
        parsed = json.loads(text or "{}")
    except json.JSONDecodeError:
        return str(_redact(text or ""))
    return json.dumps(_redact(parsed), ensure_ascii=False, sort_keys=True, default=str)
