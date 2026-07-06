from __future__ import annotations

import asyncio
from typing import Any


def pytest_configure(config: Any) -> None:
    loop = asyncio.new_event_loop()
    asyncio.get_event_loop_policy().set_event_loop(loop)
    config._navi_default_event_loop = loop


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    loop = getattr(session.config, "_navi_default_event_loop", None)
    if loop is not None and not loop.is_running() and not loop.is_closed():
        loop.close()
    asyncio.get_event_loop_policy().set_event_loop(None)
