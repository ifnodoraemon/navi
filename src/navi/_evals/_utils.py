"""Shared helpers for evaluation datasets."""

from __future__ import annotations

import time
import uuid


def _safe_path_name(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value.strip())
    return safe or "journey"


def _eval_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
