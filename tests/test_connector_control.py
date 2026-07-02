from __future__ import annotations

import pytest

from navi.connector_router import ConnectorRouter
from navi.connector_runtime import ConnectorMessage
from navi.event_bus import EventBus


@pytest.mark.asyncio
async def test_connector_approval_command_returns_unresolved_fact(tmp_path):
    router = ConnectorRouter(tmp_path, EventBus())

    response = await router.route(
        ConnectorMessage(
            message_id="msg-approval",
            peer_id="peer-1",
            sender_id="sender-1",
            text="批准 123456",
            source="weixin",
            session_alias_prefix="connector:weixin",
        )
    )

    assert "approval_not_resolved" in response
    assert "approval_code_store_unavailable" in response
