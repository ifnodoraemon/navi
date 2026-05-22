from __future__ import annotations

import json
from typing import Any


def parse_first_json_object(text: str) -> dict[str, Any] | None:
    """Return the first valid JSON object embedded in text."""
    search_from = 0
    while True:
        start = text.find("{", search_from)
        if start < 0:
            return None
        depth = 0
        in_string = False
        escape_next = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape_next:
                escape_next = False
                continue
            if ch == "\\":
                if in_string:
                    escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        search_from = start + 1
                        break
                    return data if isinstance(data, dict) else None
        else:
            return None
