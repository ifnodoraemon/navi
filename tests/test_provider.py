import json
from unittest.mock import Mock, patch

import httpx
import pytest

from navi.config import ModelConfig, _model_config
from navi.provider import (
    AnthropicCompatibleProvider,
    ChatMessage,
    ModelPool,
    OpenAICompatibleProvider,
    ProviderHTTPError,
    ProviderResponseError,
    ProviderUsage,
    StructuredOutputError,
    _complete_with_optional_schema,
    _extract_anthropic_content,
    _extract_openai_content,
    _messages_for_response_format,
    _openai_usage_facts,
    _provider_stream_event_json,
    _validate_structured_output,
)
from navi.provider_specs import ProviderSpec
from navi.resource_gateway import (
    GlobalResourceGateway,
    ResourceLimits,
    ResourceRequest,
    SQLiteResourceLedger,
)
from navi.syscalls import provider_failure_facts


def test_model_config_role_params_use_global_defaults_and_overrides():
    config = ModelConfig(role_params={"planner": {"max_tokens": 1234}})

    assert config.get_role_params("planner") == {"temperature": 0.0, "max_tokens": 1234}
    assert config.get_role_params("unknown") == {"temperature": 0.3, "max_tokens": 8192}


def test_model_config_rejects_provider_fallbacks():
    with pytest.raises(ValueError, match="model.fallbacks is unsupported"):
        _model_config({"fallbacks": []})


def test_model_config_preserves_explicit_model_across_provider_names() -> None:
    config = _model_config(
        {
            "provider": "anthropic",
            "model": "qwen3.5-397b-a17b",
        }
    )

    assert config.model == "qwen3.5-397b-a17b"


def test_model_config_preserves_explicit_provider_request_options() -> None:
    config = _model_config(
        {
            "request_options": {
                "chat_template_kwargs": {"enable_thinking": False},
            }
        }
    )

    assert config.request_options == {
        "chat_template_kwargs": {"enable_thinking": False},
    }


def test_provider_failure_facts_do_not_reclassify_programming_errors() -> None:
    implementation_failure = provider_failure_facts(
        AssertionError("provider adapter implementation bug")
    )
    response_failure = provider_failure_facts(
        ProviderResponseError("provider response shape is invalid")
    )

    assert implementation_failure["provider_call_failure"] is False
    assert implementation_failure["retryable"] is False
    assert response_failure["provider_call_failure"] is True
    assert response_failure["retryable"] is False


def test_openai_usage_rejects_invalid_or_missing_canonical_counts() -> None:
    config = ModelConfig(provider="openai-compatible", model="usage-model")
    with pytest.raises(ProviderResponseError, match="prompt_tokens"):
        _openai_usage_facts(
            config,
            {
                "usage": {
                    "prompt_tokens": "unknown",
                    "completion_tokens": 2,
                    "total_tokens": 2,
                }
            },
        )
    with pytest.raises(ProviderResponseError, match="completion_tokens"):
        _openai_usage_facts(
            config,
            {"usage": {"prompt_tokens": 1, "total_tokens": 1}},
        )


@patch("navi.provider.resolve_model_config")
def test_openai_provider_init(mock_resolve):
    mock_config = Mock()
    mock_spec = Mock()

    mock_resolve.return_value = mock_config

    provider = OpenAICompatibleProvider(mock_config, mock_spec)

    assert provider.spec == mock_spec
    mock_resolve.assert_called_once_with(mock_config)


@pytest.mark.asyncio
async def test_openai_provider_preserves_bounded_http_error_facts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            request=request,
            json={
                "error": {
                    "type": "invalid_request_error",
                    "code": "context_length_exceeded",
                    "param": "messages",
                    "message": "maximum context length exceeded",
                    "ignored": "must not leak into the exception",
                }
            },
        )

    provider = OpenAICompatibleProvider(
        ModelConfig(
            provider="openai-compatible",
            model="model",
            api_base_url="https://provider.example/v1",
            api_key="secret-token",
        ),
        ProviderSpec(
            name="openai-compatible",
            kind="openai-compatible",
            default_model="model",
            default_base_url="https://provider.example/v1",
        ),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ProviderHTTPError) as captured:
        await provider.complete([ChatMessage("user", "hello")])

    error = captured.value
    assert error.status_code == 400
    assert error.error_code == "context_length_exceeded"
    assert error.error_param == "messages"
    assert error.provider_message == "maximum context length exceeded"
    assert error.request_chars == 5
    assert error.max_output_tokens == 32768
    assert "secret-token" not in str(error)
    assert "ignored" not in str(error)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_class", "provider_kind"),
    [
        (OpenAICompatibleProvider, "openai-compatible"),
        (AnthropicCompatibleProvider, "anthropic-compatible"),
    ],
)
async def test_provider_rejects_non_json_success_as_bounded_protocol_fact(
    provider_class,
    provider_kind: str,
) -> None:
    leaked_body = "upstream proxy page with secret-token"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/html; charset=utf-8"},
            text=leaked_body,
        )

    provider = provider_class(
        ModelConfig(
            provider=provider_kind,
            kind=provider_kind,
            model="model",
            api_base_url="https://provider.example/v1",
            api_key="secret-token",
        ),
        ProviderSpec(
            name=provider_kind,
            kind=provider_kind,
            default_model="model",
            default_base_url="https://provider.example/v1",
        ),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ProviderResponseError) as captured:
        await provider.complete([ChatMessage("user", "hello")])

    error = captured.value
    assert "status_code=200" in str(error)
    assert "content_type=text/html; charset=utf-8" in str(error)
    assert f"body_bytes={len(leaked_body)}" in str(error)
    assert leaked_body not in str(error)
    assert "secret-token" not in str(error)


@pytest.mark.asyncio
async def test_provider_rejects_non_object_success_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, json=[])

    provider = OpenAICompatibleProvider(
        ModelConfig(
            provider="openai-compatible",
            model="model",
            api_base_url="https://provider.example/v1",
            api_key="secret-token",
        ),
        ProviderSpec(
            name="openai-compatible",
            kind="openai-compatible",
            default_model="model",
            default_base_url="https://provider.example/v1",
        ),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ProviderResponseError, match="json_type=list"):
        await provider.complete([ChatMessage("user", "hello")])


@pytest.mark.asyncio
async def test_openai_provider_assembles_explicit_sse_response_and_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["stream"] is True
        assert payload["stream_options"] == {"include_usage": True}
        assert payload["chat_template_kwargs"] == {"enable_thinking": False}
        body = "\n\n".join(
            [
                'data: {"choices":[{"delta":{"content":"{\\"tool\\":"}}]}',
                'data: {"choices":[{"delta":{"content":"\\"ok\\"}"},"finish_reason":"stop"}]}',
                'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}',
                "data: [DONE]",
                "",
            ]
        )
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "application/json"},
            text=body,
        )

    provider = OpenAICompatibleProvider(
        ModelConfig(
            provider="openai-compatible",
            model="model",
            api_base_url="https://provider.example/v1",
            api_key="secret-token",
            response_transport="sse",
            request_options={"chat_template_kwargs": {"enable_thinking": False}},
        ),
        ProviderSpec(
            name="openai-compatible",
            kind="openai-compatible",
            default_model="model",
            default_base_url="https://provider.example/v1",
        ),
        transport=httpx.MockTransport(handler),
    )

    result = await provider.complete([ChatMessage("user", "hello")])

    assert result == '{"tool":"ok"}'
    assert provider.last_usage is not None
    assert provider.last_usage.to_facts() == {
        "provider": "openai-compatible",
        "model": "model",
        "input_tokens": 3,
        "output_tokens": 2,
        "prompt_tokens": 3,
        "completion_tokens": 2,
        "total_tokens": 5,
        "raw": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }


@pytest.mark.asyncio
async def test_openai_stream_parses_sse_without_trusting_media_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "application/json"},
            text=(
                'data: {"choices":[{"delta":{"content":"one"}}]}\n\n'
                'data: {"choices":[{"delta":{"content":"two"}}]}\n\n'
                "data: [DONE]\n\n"
            ),
        )

    provider = OpenAICompatibleProvider(
        ModelConfig(
            provider="openai-compatible",
            model="model",
            api_base_url="https://provider.example/v1",
            api_key="secret-token",
        ),
        ProviderSpec(
            name="openai-compatible",
            kind="openai-compatible",
            default_model="model",
            default_base_url="https://provider.example/v1",
        ),
        transport=httpx.MockTransport(handler),
    )

    tokens = [token async for token in provider.stream([ChatMessage("user", "hello")])]

    assert tokens == ["one", "two"]


@pytest.mark.asyncio
async def test_openai_sse_completion_requires_done_event() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            text='data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
        )

    provider = OpenAICompatibleProvider(
        ModelConfig(
            provider="openai-compatible",
            model="model",
            api_base_url="https://provider.example/v1",
            api_key="secret-token",
            response_transport="sse",
        ),
        ProviderSpec(
            name="openai-compatible",
            kind="openai-compatible",
            default_model="model",
            default_base_url="https://provider.example/v1",
        ),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ProviderResponseError, match="without a DONE event"):
        await provider.complete([ChatMessage("user", "hello")])


@pytest.mark.parametrize("raw", ["secret-token is not json", "[]"])
def test_provider_rejects_malformed_stream_events_without_leaking_content(raw: str) -> None:
    with pytest.raises(ProviderResponseError) as captured:
        _provider_stream_event_json(raw, protocol="openai")

    assert raw not in str(captured.value)
    assert "secret-token" not in str(captured.value)


@pytest.mark.parametrize(
    ("extract", "payload", "expected"),
    [
        (
            _extract_openai_content,
            {"choices": [], "secret-token": "must not leak"},
            "did not include choices",
        ),
        (
            _extract_anthropic_content,
            {"content": [{"type": "redacted", "text": "secret-token"}]},
            "did not include text content",
        ),
    ],
)
def test_provider_shape_failures_do_not_echo_response_values(
    extract, payload: dict[str, object], expected: str
) -> None:
    with pytest.raises(ProviderResponseError, match=expected) as captured:
        extract(payload)

    assert "secret-token" not in str(captured.value)


class _ReadErrorProvider:
    last_usage = None

    async def complete(self, messages, **kwargs):
        del messages, kwargs
        raise httpx.ReadError("upstream connection reset")


@pytest.mark.asyncio
async def test_model_pool_releases_reservation_on_transport_error(tmp_path) -> None:
    ledger = SQLiteResourceLedger(tmp_path)
    gateway = GlobalResourceGateway(
        ResourceLimits(max_concurrent=1),
        ledger=ledger,
        scope_id="transport-pause",
    )
    pool = ModelPool(default=_ReadErrorProvider())

    with pool.bind_resource_gateway(gateway), pytest.raises(httpx.ReadError):
        await pool.complete_for("planner", [ChatMessage("user", "hello")])

    assert ledger.usage("transport-pause").active == 0


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

    with pytest.raises(StructuredOutputError, match=r"structured output schema mismatch"):
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


class _FailureProvider:
    last_usage: ProviderUsage | None = None

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages: list[ChatMessage], **kwargs) -> str:
        del messages, kwargs
        self.calls += 1
        raise RuntimeError("provider unavailable")


class _SchemaFailureProvider:
    last_usage: ProviderUsage | None = None

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages: list[ChatMessage], **kwargs) -> str:
        del messages, kwargs
        self.calls += 1
        raise TypeError("output_schema is unsupported")


@pytest.mark.asyncio
async def test_model_pool_propagates_provider_failure_without_retry():
    provider = _FailureProvider()
    pool = ModelPool(default=provider)

    with pytest.raises(RuntimeError, match="provider unavailable"):
        await pool.complete_for("planner", [ChatMessage("user", "hi")])

    assert provider.calls == 1


@pytest.mark.asyncio
async def test_structured_call_propagates_schema_failure_without_retry():
    provider = _SchemaFailureProvider()

    with pytest.raises(TypeError, match="output_schema is unsupported"):
        await _complete_with_optional_schema(
            provider,
            [ChatMessage("user", "hi")],
            output_schema={"type": "object"},
        )

    assert provider.calls == 1


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
        resource_gateway=GlobalResourceGateway(ResourceLimits(token_budget=100, max_concurrent=1)),
    )

    with pytest.raises(RuntimeError, match="token_budget_exhausted"):
        await pool.complete_for("planner", [ChatMessage("user", "x" * 80)])
