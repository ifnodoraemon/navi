from __future__ import annotations

import asyncio
import pytest

from navi.config import load_config, write_default_config, validate_config
from navi.weixin.service import WeixinService
from navi.weixin.config import WeixinConfig
from navi.weixin.connector import _status as weixin_status
from navi.telegram.service import TelegramService
from navi.telegram.config import TelegramConfig
from navi.telegram.connector import _status as telegram_status
from navi.provider import MockProvider, ModelPool
from navi.runtime import AgentRuntime
from navi.core_tools import _provider_config


def test_validate_config_basic(tmp_path):
    # Test valid configuration
    write_default_config(tmp_path)
    config = load_config(tmp_path)
    errors = validate_config(config, tmp_path)
    assert len(errors) == 0


def test_validate_config_errors(tmp_path):
    # Test invalid configuration errors
    write_default_config(tmp_path)
    config = load_config(tmp_path)
    
    # Empty provider
    config.model.provider = ""
    errors = validate_config(config, tmp_path)
    assert any("provider is empty" in e for e in errors)
    
    # Custom provider without kind
    config.model.provider = "my-custom-provider"
    config.model.kind = ""
    errors = validate_config(config, tmp_path)
    assert any("kind is required" in e for e in errors)

    # Custom provider with unsupported kind
    config.model.kind = "unsupported-kind"
    errors = validate_config(config, tmp_path)
    assert any("unsupported" in e for e in errors)

    # Empty api_key for non-mock
    config.model.kind = "openai-compatible"
    config.model.api_key = ""
    errors = validate_config(config, tmp_path)
    assert any("api_key is empty" in e for e in errors)


def test_provider_config_tool_validation_errors(tmp_path):
    write_default_config(tmp_path)
    # Write empty provider to trigger validation failure
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "\n".join([
            "model:",
            "  provider: ''",
        ]),
        encoding="utf-8",
    )
    result = _provider_config(tmp_path)
    assert result.ok is False
    assert "validation_errors" in result.facts
    assert len(result.facts["validation_errors"]) > 0


@pytest.mark.asyncio
async def test_weixin_service_status_and_adaptive_polling(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_WEIXIN_MOCK", "true")
    runtime = AgentRuntime(home=tmp_path, provider=ModelPool(default=MockProvider()))
    service = WeixinService(
        home=tmp_path,
        config=WeixinConfig(dm_policy="open", account_id="dummy", token="dummy"),
        runtime=runtime,
    )
    
    # Verify status writing and reading
    service.update_status("healthy", "no error")
    facts = weixin_status(tmp_path)
    assert facts["status"] == "healthy"
    assert facts["error"] == "no error"
    assert facts["last_update"] > 0

    # Mock get_updates to return empty batch initially
    from navi.weixin.models import WeixinUpdateBatch
    async def mock_get_updates_empty(account_id, sync_buf=None):
        return WeixinUpdateBatch(sync_buf="buf", updates=[])
    monkeypatch.setattr(service.client, "get_updates", mock_get_updates_empty)

    # Spy on asyncio.sleep
    sleep_calls = []
    async def mock_sleep(seconds):
        sleep_calls.append(seconds)
        raise asyncio.CancelledError()
    monkeypatch.setattr(asyncio, "sleep", mock_sleep)

    # 1. No activity (empty updates, no tasks) -> should sleep for 1.0
    try:
        await service.run()
    except asyncio.CancelledError:
        pass
    assert len(sleep_calls) == 1
    assert sleep_calls[0] == 1.0

    # 2. Activity present (with updates) -> should adapt sleep to 0.05
    from navi.weixin.models import WeixinUpdate
    async def mock_get_updates_active(account_id, sync_buf=None):
        return WeixinUpdateBatch(
            sync_buf="buf",
            updates=[
                WeixinUpdate(
                    message_id="msg1",
                    peer_id="peer",
                    sender_id="sender",
                    text="test",
                    context_token="tok",
                    is_group=False,
                )
            ]
        )
    monkeypatch.setattr(service.client, "get_updates", mock_get_updates_active)
    
    sleep_calls.clear()
    try:
        await service.run()
    except asyncio.CancelledError:
        pass
    assert len(sleep_calls) == 1
    assert sleep_calls[0] == 0.05

    # 3. Exception in get_updates -> should trigger backoff retry and status update
    async def mock_get_updates_error(account_id, sync_buf=None):
        raise ValueError("Network error")
    monkeypatch.setattr(service.client, "get_updates", mock_get_updates_error)
    
    sleep_calls.clear()
    try:
        await service.run()
    except asyncio.CancelledError:
        pass
    
    assert len(sleep_calls) == 1
    # First retry backoff sleep: 1.5 ** 1 = 1.5
    assert sleep_calls[0] == 1.5
    facts = weixin_status(tmp_path)
    assert facts["status"] == "retrying"
    assert "Network error" in facts["error"]


@pytest.mark.asyncio
async def test_telegram_service_status_and_adaptive_polling(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_TELEGRAM_MOCK", "true")
    runtime = AgentRuntime(home=tmp_path, provider=ModelPool(default=MockProvider()))
    service = TelegramService(
        home=tmp_path,
        config=TelegramConfig(dm_policy="open", bot_token="dummy"),
        runtime=runtime,
    )
    
    # Verify status writing and reading
    service.update_status("healthy", "no error")
    facts = telegram_status(tmp_path)
    assert facts["status"] == "healthy"
    assert facts["error"] == "no error"
    assert facts["last_update"] > 0

    # Mock get_updates to return empty list initially
    async def mock_get_updates_empty(offset=None):
        return []
    monkeypatch.setattr(service.client, "get_updates", mock_get_updates_empty)

    # Spy on asyncio.sleep
    sleep_calls = []
    async def mock_sleep(seconds):
        sleep_calls.append(seconds)
        raise asyncio.CancelledError()
    monkeypatch.setattr(asyncio, "sleep", mock_sleep)

    # 1. No activity -> should sleep for 1.0
    try:
        await service.run()
    except asyncio.CancelledError:
        pass
    assert len(sleep_calls) == 1
    assert sleep_calls[0] == 1.0

    # 2. Activity present -> should adapt sleep to 0.05
    from navi.telegram.models import TelegramUpdate
    async def mock_get_updates_active(offset=None):
        return [
            TelegramUpdate(
                update_id=1,
                message_id=10,
                chat_id="chat",
                sender_id="sender",
                text="hello",
            )
        ]
    monkeypatch.setattr(service.client, "get_updates", mock_get_updates_active)
    
    sleep_calls.clear()
    try:
        await service.run()
    except asyncio.CancelledError:
        pass
    assert len(sleep_calls) == 1
    assert sleep_calls[0] == 0.05

    # 3. Exception in get_updates -> should trigger backoff retry and status update
    async def mock_get_updates_error(offset=None):
        raise ValueError("Telegram failure")
    monkeypatch.setattr(service.client, "get_updates", mock_get_updates_error)
    
    sleep_calls.clear()
    try:
        await service.run()
    except asyncio.CancelledError:
        pass
    
    assert len(sleep_calls) == 1
    # First retry backoff sleep: 1.5 ** 1 = 1.5
    assert sleep_calls[0] == 1.5
    facts = telegram_status(tmp_path)
    assert facts["status"] == "retrying"
    assert "Telegram failure" in facts["error"]
