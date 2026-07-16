from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from navi.runtime import AgentRuntime
from navi.weixin.config import load_weixin_config
from navi.weixin.service import WeixinService


_SEND_AUTH_PHRASE = "send-real-connector-smoke"


class _NoModelCalls:
    async def complete_for(self, role, messages, **kwargs):
        raise AssertionError(f"live connector smoke does not call model role={role}")

    def list_roles(self) -> list[str]:
        return ["planner", "checker", "responder"]

    def usage_for(self, role: str) -> dict:
        del role
        return {}


def _live_smoke_settings() -> tuple[Path, str]:
    if os.environ.get("NAVI_LIVE_CONNECTOR_SMOKE") != "1":
        pytest.skip("set NAVI_LIVE_CONNECTOR_SMOKE=1 to run live connector smoke")
    connector = os.environ.get("NAVI_LIVE_CONNECTOR", "weixin").strip().lower()
    if connector != "weixin":
        pytest.skip(f"live connector smoke only supports weixin, got {connector!r}")
    recipient = os.environ.get("NAVI_LIVE_CONNECTOR_RECIPIENT", "").strip()
    if not recipient:
        pytest.skip("set NAVI_LIVE_CONNECTOR_RECIPIENT to the explicit target peer_id")
    send_auth = os.environ.get("NAVI_LIVE_CONNECTOR_SEND_AUTH", "").strip()
    if send_auth != _SEND_AUTH_PHRASE:
        pytest.skip(
            "set NAVI_LIVE_CONNECTOR_SEND_AUTH=send-real-connector-smoke "
            "to authorize a real connector message"
        )
    home = Path(
        os.environ.get("NAVI_LIVE_CONNECTOR_HOME") or os.environ.get("NAVI_HOME") or ".navi"
    )
    return home.expanduser().resolve(), recipient


@pytest.mark.live_connector
@pytest.mark.asyncio
async def test_opt_in_weixin_live_connector_delivery_smoke() -> None:
    home, recipient = _live_smoke_settings()
    config = load_weixin_config(home)
    if not config.account_id and not (home / "weixin").exists():
        pytest.skip("weixin account is not configured for the requested NAVI home")
    service = WeixinService(
        home=home,
        config=config,
        runtime=AgentRuntime(home=home, provider=_NoModelCalls()),
        project_dir=Path.cwd(),
    )
    account = service._resolve_account()
    text = f"Navi live connector smoke {uuid.uuid4().hex[:8]}: explicit opt-in delivery path check."

    receipt = await service._send_reply(
        account=account,
        peer_id=recipient,
        text=text,
        action="chat",
        facts={},
        context_token="",
    )

    assert receipt["media_count"] == 0
    assert receipt["text_preview"] == text[:120]
