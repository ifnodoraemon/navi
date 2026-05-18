from __future__ import annotations

import os
from pathlib import Path


def navi_home() -> Path:
    raw = os.environ.get("NAVI_HOME")
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.cwd() / ".navi").resolve()


def ensure_home() -> Path:
    home = navi_home()
    home.mkdir(parents=True, exist_ok=True)
    return home
