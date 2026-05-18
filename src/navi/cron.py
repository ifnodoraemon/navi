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


def _matches(field: str, value: int, min_value: int, max_value: int) -> bool:
    return value in _parse_field(field, min_value, max_value)


def _parse_field(field: str, min_value: int, max_value: int) -> set[int]:
    values: set[int] = set()
    for raw in field.split(","):
        part = raw.strip()
        if not part:
            raise ValueError("Cron field includes an empty segment")
        step = 1
        if "/" in part:
            part, step_raw = part.split("/", 1)
            step = int(step_raw)
            if step <= 0:
                raise ValueError("Cron step must be positive")
        if part == "*":
            start, end = min_value, max_value
        elif "-" in part:
            start_raw, end_raw = part.split("-", 1)
            start, end = int(start_raw), int(end_raw)
        else:
            start = end = int(part)
        if start < min_value or end > max_value or start > end:
            raise ValueError(f"Cron value out of range: {field}")
        values.update(range(start, end + 1, step))
    return values
