from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


@lru_cache(maxsize=None)
def load_spec(name: str) -> Any:
    path = Path(__file__).parent / "specs" / name
    return yaml.safe_load(path.read_text(encoding="utf-8"))
