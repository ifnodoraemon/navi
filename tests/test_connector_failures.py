from __future__ import annotations

import json
from pathlib import Path

import pytest

from navi.runtime import AgentRuntime
from navi.telegram.config import TelegramConfig
from navi.telegram.service import TelegramService
from navi.weixin.config import WeixinConfig
from navi.weixin.service import WeixinService


class NoModelCalls:
    def list_roles(self) -> list[str]:
        return []


class FailingTelegramClient:
    def __init__(self) -> None:
        self.calls = 0

    async def get_updates(self, *, offset=None):
        del offset
        self.calls += 1
        raise RuntimeError("telegram unavailable")


class FailingWeixinClient:
    def __init__(self) -> None:
        self.calls = 0

    async def get_updates(self, account_id: str, *, sync_buf: str):
        del account_id, sync_buf
        self.calls += 1
        raise RuntimeError("weixin unavailable")


@pytest.mark.asyncio
async def test_telegram_poll_failure_is_not_retried(tmp_path: Path) -> None:
    client = FailingTelegramClient()
    service = TelegramService(
        home=tmp_path,
        config=TelegramConfig(enabled=True, bot_token="token"),
        runtime=AgentRuntime(home=tmp_path, provider=NoModelCalls()),
        project_dir=tmp_path,
        client=client,
    )

    with pytest.raises(RuntimeError, match="telegram unavailable"):
        await service.run(once=True)

    status = json.loads((tmp_path / "telegram" / "status.json").read_text(encoding="utf-8"))
    assert client.calls == 1
    assert status["status"] == "fatal"
    assert status["error"] == "telegram unavailable"


@pytest.mark.asyncio
async def test_weixin_poll_failure_is_not_retried(tmp_path: Path) -> None:
    client = FailingWeixinClient()
    service = WeixinService(
        home=tmp_path,
        config=WeixinConfig(enabled=True, account_id="acct", token="token"),
        runtime=AgentRuntime(home=tmp_path, provider=NoModelCalls()),
        project_dir=tmp_path,
        client=client,
    )

    with pytest.raises(RuntimeError, match="weixin unavailable"):
        await service.run(once=True)

    status = json.loads((tmp_path / "weixin" / "status.json").read_text(encoding="utf-8"))
    assert client.calls == 1
    assert status["status"] == "fatal"
    assert status["error"] == "weixin unavailable"
