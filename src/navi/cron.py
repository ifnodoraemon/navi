from __future__ import annotations

import time
from datetime import datetime, timedelta

def next_cron_time(expression: str, *, now: float | None = None) -> float:
    """Return the next time for a simple 5-field cron expression."""
    base = datetime.fromtimestamp(now or time.time()).replace(second=0, microsecond=0)
    minute, hour, day, month, weekday = expression.split()
    for offset in range(1, 366 * 24 * 60):
        candidate = base + timedelta(minutes=offset)
        if (
            _matches(minute, candidate.minute, 0, 59)
            and _matches(hour, candidate.hour, 0, 23)
            and _matches(day, candidate.day, 1, 31)
            and _matches(month, candidate.month, 1, 12)
            and _matches(weekday, (candidate.weekday() + 1) % 7, 0, 6)
        ):
            return candidate.timestamp()
    raise ValueError(f"Cron expression has no run in the next year: {expression}")

def validate_cron(expression: str) -> None:
    parts = expression.split()
    if len(parts) != 5:
        raise ValueError("Cron expression must have 5 fields")
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    for part, (min_value, max_value) in zip(parts, ranges):
        _parse_field(part, min_value, max_value)

def _parse_field(field: str, min_value: int, max_value: int) -> set[int]:
    if field == "*":
        return set(range(min_value, max_value + 1))
    if field.startswith("*/"):
        step = int(field[2:])
        return set(range(min_value, max_value + 1, step))
    if "," in field:
        return {int(x) for x in field.split(",")}
    return {int(field)}

def _matches(field: str, value: int, min_value: int, max_value: int) -> bool:
    return value in _parse_field(field, min_value, max_value)
