from __future__ import annotations

from pathlib import Path
from typing import Any

from navi.runtime import AgentRuntime
from navi.weixin.config import WeixinConfig
from navi.weixin.service import WeixinService


class NoModelCalls:
    async def complete_for(self, role: str, messages: list[Any], **kwargs: Any) -> str:
        raise AssertionError(f"unexpected model call in service initialization: {role}")

    def list_roles(self) -> list[str]:
        return []

