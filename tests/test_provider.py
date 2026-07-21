from unittest.mock import Mock, patch

import pytest

from navi.config import ModelConfig
from navi.provider import (
    ChatMessage,
    FallbackProvider,
    ModelPool,
    OpenAICompatibleProvider,
    ProviderUsage,
    _messages_for_response_format,
    _validate_structured_output,
)
from navi.resource_gateway import ResourceRequest
from navi.resource_gateway import GlobalResourceGateway, ResourceLimits


def test_model_config_role_params_use_global_defaults_and_overrides():
    config = ModelConfig(role_params={"planner": {"max_tokens": 1234}})

    assert config.get_role_params("planner") == {"temperature": 0.0, "max_tokens": 1234}
    assert config.get_role_params("unknown") == {"temperature": 0.3, "max_tokens": 8192}


@patch("navi.provider.resolve_model_config")
def test_openai_provider_init(mock_resolve):
    mock_config = Mock()
    mock_spec = Mock()
    
    mock_resolve.return_value = mock_config
    
    provider = OpenAICompatibleProvider(mock_config, mock_spec)
    
    assert provider.spec == mock_spec
    mock_resolve.assert_called_once_with(mock_config)


def test_structured_output_validation_checks_json_schema_types():
    output_schema = {
        "name": "planner_decision",
        "schema": {
            "type": "object",
            "required": ["tool", "args"],
            "properties": {
                "tool": {"type": "string"},
                "args": {
                    "type": "object",
                    "required": ["limit"],
                    "properties": {"limit": {"type": "integer"}},
                },
            },
        },
    }

    _validate_structured_output('{"tool":"search","args":{"limit":3}}', output_schema)

    with pytest.raises(RuntimeError, match=r"structured output schema mismatch"):
        _validate_structured_output('{"tool":"search","args":{"limit":"3"}}', output_schema)


def test_structured_output_validation_checks_array_items():
    output_schema = {
        "schema": {
            "type": "object",
            "required": ["tool_calls"],
            "properties": {
                "tool_calls": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["tool"],
                        "properties": {"tool": {"type": "string"}},
                    },
                }
            },
        },
    }

    _validate_structured_output('{"tool_calls":[{"tool":"shell.run"}]}', output_schema)

    with pytest.raises(RuntimeError, match=r"tool_calls"):
        _validate_structured_output('{"tool_calls":[{"tool":404}]}', output_schema)


def test_json_object_mode_does_not_inject_schema_into_prompt():
    messages = [ChatMessage("user", "hi")]
    outbound = _messages_for_response_format(messages, {"type": "json_object"})

    assert outbound[0].role == "system"
    assert "JSON mode is enabled" in outbound[0].content
    assert "JSON Schema" not in outbound[0].content
    assert "strictly matches" not in outbound[0].content
    assert outbound[1:] == messages


class _UsageProvider:
    last_usage: ProviderUsage | None = None

    async def complete(self, messages: list[ChatMessage], **kwargs) -> str:
        self.last_usage = ProviderUsage(
            provider="openai-compatible",
            model="usage-model",
            input_tokens=11,
            output_tokens=7,
            total_tokens=18,
            raw={"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        )
        return "ok"


class _EmptyFailureProvider:
    last_usage: ProviderUsage | None = None

    async def complete(self, messages: list[ChatMessage], **kwargs) -> str:
        del messages, kwargs
        raise RuntimeError()


@pytest.mark.asyncio
async def test_fallback_provider_preserves_empty_exception_type(monkeypatch):
    import asyncio

    async def no_sleep(delay: float) -> None:
        del delay

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    provider = FallbackProvider([_EmptyFailureProvider()])

    with pytest.raises(
        RuntimeError,
        match=r"all model providers failed: _EmptyFailureProvider: RuntimeError",
    ):
        await provider.complete([ChatMessage("user", "hi")])


@pytest.mark.asyncio
async def test_model_pool_exposes_provider_usage_by_role():
    pool = ModelPool(default=_UsageProvider())

    result = await pool.complete_for("planner", [ChatMessage("user", "hi")])

    assert result == "ok"
    assert pool.usage_for("planner") == {
        "role": "planner",
        "provider": "openai-compatible",
        "model": "usage-model",
        "input_tokens": 11,
        "output_tokens": 7,
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
        "messages": [{"role": "user", "content": "hi"}],
        "response": "ok",
        "raw": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }


@pytest.mark.asyncio
async def test_model_pool_blocks_when_resource_gateway_pauses():
    pool = ModelPool(default=_UsageProvider())
    held = pool.resource_gateway.request(ResourceRequest(kind="held-llm"))
    assert held.allowed is True

    with pytest.raises(RuntimeError, match="resource gateway pause: concurrency_limit"):
        await pool.complete_for("planner", [ChatMessage("user", "hi")])


@pytest.mark.asyncio
async def test_model_pool_releases_resource_gateway_after_completion():
    pool = ModelPool(default=_UsageProvider())

    first = await pool.complete_for("planner", [ChatMessage("user", "one")])
    second = await pool.complete_for("planner", [ChatMessage("user", "two")])

    assert first == "ok"
    assert second == "ok"


@pytest.mark.asyncio
async def test_model_pool_reserves_declared_output_tokens_before_calling_provider():
    provider = _UsageProvider()
    pool = ModelPool(
        default=provider,
        config=ModelConfig(role_params={"planner": {"max_tokens": 90}}),
        resource_gateway=GlobalResourceGateway(
            ResourceLimits(token_budget=100, max_concurrent=1)
        ),
    )

    with pytest.raises(RuntimeError, match="token_budget_exhausted"):
        await pool.complete_for("planner", [ChatMessage("user", "x" * 80)])
