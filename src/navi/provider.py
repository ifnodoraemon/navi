from __future__ import annotations

import inspect
import json
import re
from collections.abc import AsyncGenerator, AsyncIterable, Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from .config import ModelConfig
from .json_utils import json_schema_errors
from .provider_specs import (
    ProviderSpec,
    get_provider_spec,
    list_provider_specs as _list_provider_specs,
)
from .resource_gateway import (
    GlobalResourceGateway,
    ResourceLimitError,
    ResourceLimits,
    ResourceRequest,
)


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str


@dataclass(frozen=True)
class ProviderUsage:
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    def to_facts(self, *, role: str = "") -> dict[str, Any]:
        facts = {
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "prompt_tokens": self.input_tokens,
            "completion_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }
        if role:
            facts["role"] = role
        if self.raw:
            facts["raw"] = self.raw
        return facts


class ChatProvider(Protocol):
    last_usage: ProviderUsage | None

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        output_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        request_options: dict[str, Any] | None = None,
    ) -> str: ...

    def stream(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        request_options: dict[str, Any] | None = None,
    ) -> AsyncGenerator[str]: ...


ProviderFactory = Callable[[ModelConfig, ProviderSpec], ChatProvider]


class ProviderHTTPError(RuntimeError):
    """A bounded, credential-free projection of an upstream HTTP failure."""

    def __init__(
        self,
        *,
        status_code: int,
        error_type: str = "",
        error_code: str = "",
        error_param: str = "",
        provider_message: str = "",
        request_chars: int = 0,
        max_output_tokens: int = 0,
        retry_after_seconds: float = 0.0,
    ) -> None:
        self.status_code = int(status_code)
        self.error_type = error_type
        self.error_code = error_code
        self.error_param = error_param
        self.provider_message = provider_message
        self.request_chars = max(0, int(request_chars))
        self.max_output_tokens = max(0, int(max_output_tokens))
        self.retry_after_seconds = max(0.0, float(retry_after_seconds))
        fields = [f"provider HTTP {self.status_code}"]
        if error_type:
            fields.append(f"type={error_type}")
        if error_code:
            fields.append(f"code={error_code}")
        if error_param:
            fields.append(f"param={error_param}")
        if provider_message:
            fields.append(f"message={provider_message}")
        if self.request_chars:
            fields.append(f"request_chars={self.request_chars}")
        if self.max_output_tokens:
            fields.append(f"max_output_tokens={self.max_output_tokens}")
        super().__init__(" ".join(fields))


class StructuredOutputError(RuntimeError):
    """The provider returned content that failed the declared output contract."""


class ProviderResponseError(RuntimeError):
    """The provider returned a response without the required protocol shape."""


@dataclass(frozen=True)
class ProviderAdapter:
    kind: str
    build: ProviderFactory


class OpenAICompatibleProvider:
    def __init__(
        self,
        config: ModelConfig,
        spec: ProviderSpec,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.config = resolve_model_config(config)
        self.spec = spec
        self.transport = transport
        self.last_usage: ProviderUsage | None = None

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        output_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        request_options: dict[str, Any] | None = None,
    ) -> str:
        if not self.config.api_key:
            raise RuntimeError(f"model.api_key is required for {self.config.provider} provider")
        self.last_usage = None
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [{"role": msg.role, "content": msg.content} for msg in messages],
            "temperature": 0 if temperature is None else temperature,
            "max_tokens": 32768 if max_tokens is None else max_tokens,
        }
        if self.config.response_transport == "sse":
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
        structured_format = _structured_response_format(self.spec, output_schema)
        if structured_format:
            payload["response_format"] = structured_format
        outbound_messages = _messages_for_response_format(messages, structured_format)
        payload["messages"] = [
            {"role": msg.role, "content": msg.content} for msg in outbound_messages
        ]
        effective_options = _merge_request_options(
            self.config.request_options, request_options
        )
        payload = _apply_provider_request_options(payload, effective_options)
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        async with httpx.AsyncClient(
            timeout=self.config.timeout_seconds, transport=self.transport
        ) as client:
            response = await client.post(
                f"{self.config.api_base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
            _raise_provider_http_error(
                response,
                request_chars=sum(len(msg.content) for msg in outbound_messages),
                max_output_tokens=int(payload["max_tokens"]),
            )
            if self.config.response_transport == "sse":
                data = _openai_sse_response_json(response)
            else:
                data = _provider_response_json(response)
        self.last_usage = _openai_usage_facts(self.config, data)
        return _extract_openai_content(data)

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        request_options: dict[str, Any] | None = None,
    ) -> AsyncGenerator[str]:
        """Yield content-delta tokens via SSE (OpenAI-compatible streaming)."""
        if not self.config.api_key:
            raise RuntimeError(f"model.api_key is required for {self.config.provider} provider")
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [{"role": msg.role, "content": msg.content} for msg in messages],
            "temperature": 0 if temperature is None else temperature,
            "max_tokens": 32768 if max_tokens is None else max_tokens,
            "stream": True,
        }
        effective_options = _merge_request_options(
            self.config.request_options, request_options
        )
        payload = _apply_provider_request_options(payload, effective_options)
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        async with httpx.AsyncClient(timeout=None, transport=self.transport) as client:
            async with client.stream(
                "POST",
                f"{self.config.api_base_url}/chat/completions",
                json=payload,
                headers=headers,
            ) as response:
                if response.is_error:
                    await response.aread()
                _raise_provider_http_error(
                    response,
                    request_chars=sum(len(msg.content) for msg in messages),
                    max_output_tokens=int(payload["max_tokens"]),
                )
                async for raw in _iter_sse_event_data(response.aiter_lines()):
                    if raw == "[DONE]":
                        return
                    chunk = _provider_stream_event_json(raw, protocol="openai")
                    token, _, _ = _openai_sse_chunk_facts(chunk)
                    if token:
                        yield token
                raise ProviderResponseError(
                    "Provider openai stream ended without a DONE event"
                )


class AnthropicCompatibleProvider:
    def __init__(
        self,
        config: ModelConfig,
        spec: ProviderSpec,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.config = resolve_model_config(config)
        self.spec = spec
        self.transport = transport
        self.last_usage: ProviderUsage | None = None

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        output_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        request_options: dict[str, Any] | None = None,
    ) -> str:
        if not self.config.api_key:
            raise RuntimeError(f"model.api_key is required for {self.config.provider} provider")
        self.last_usage = None
        payload = _anthropic_payload(
            self.config.model, messages, temperature=temperature, max_tokens=max_tokens
        )
        structured_tool = _anthropic_structured_tool(self.spec, output_schema)
        if structured_tool:
            payload["tools"] = [structured_tool]
            payload["tool_choice"] = {"type": "tool", "name": structured_tool["name"]}
        effective_options = _merge_request_options(
            self.config.request_options, request_options
        )
        payload = _apply_provider_request_options(payload, effective_options)
        headers = {
            "x-api-key": self.config.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        async with httpx.AsyncClient(
            timeout=self.config.timeout_seconds, transport=self.transport
        ) as client:
            response = await client.post(
                f"{self.config.api_base_url}/messages",
                json=payload,
                headers=headers,
            )
            _raise_provider_http_error(
                response,
                request_chars=sum(len(msg.content) for msg in messages),
                max_output_tokens=int(payload["max_tokens"]),
            )
            data = _provider_response_json(response)
        self.last_usage = _anthropic_usage_facts(self.config, data)
        return _extract_anthropic_content(
            data,
            tool_name=structured_tool["name"] if structured_tool else "",
        )

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        request_options: dict[str, Any] | None = None,
    ) -> AsyncGenerator[str]:
        """Yield content-delta tokens via Anthropic streaming."""
        if not self.config.api_key:
            raise RuntimeError(f"model.api_key is required for {self.config.provider} provider")
        payload = _anthropic_payload(
            self.config.model, messages, temperature=temperature, max_tokens=max_tokens
        )
        payload["stream"] = True
        effective_options = _merge_request_options(
            self.config.request_options, request_options
        )
        payload = _apply_provider_request_options(payload, effective_options)
        headers = {
            "x-api-key": self.config.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        async with (
            httpx.AsyncClient(timeout=None, transport=self.transport) as client,
            client.stream(
                "POST",
                f"{self.config.api_base_url}/messages",
                json=payload,
                headers=headers,
            ) as response,
        ):
            _raise_provider_http_error(
                response,
                request_chars=sum(len(msg.content) for msg in messages),
                max_output_tokens=int(payload["max_tokens"]),
            )
            event_type = ""
            async for line in response.aiter_lines():
                line = line.strip()
                if not line:
                    event_type = ""
                    continue
                if line.startswith("event:"):
                    event_type = line[len("event:") :].strip()
                    if event_type == "message_stop":
                        return
                    continue
                if line.startswith("data:"):
                    if event_type != "content_block_delta":
                        continue
                    raw = line[len("data:") :].strip()
                    chunk = _provider_stream_event_json(raw, protocol="anthropic")
                    delta = chunk.get("delta") or {}
                    token = delta.get("text")
                    if token:
                        yield token


def _raise_provider_http_error(
    response: httpx.Response,
    *,
    request_chars: int = 0,
    max_output_tokens: int = 0,
) -> None:
    if not response.is_error:
        return
    error_type = ""
    error_code = ""
    error_param = ""
    provider_message = ""
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        payload = {}
    if isinstance(payload, dict):
        raw_error = payload.get("error", payload)
        if isinstance(raw_error, dict):
            error_type = _bounded_error_text(raw_error.get("type"), limit=100)
            error_code = _bounded_error_text(raw_error.get("code"), limit=100)
            error_param = _bounded_error_text(raw_error.get("param"), limit=100)
            provider_message = _bounded_error_text(
                raw_error.get("message") or raw_error.get("detail") or raw_error.get("errmsg"),
                limit=500,
            )
        elif isinstance(raw_error, str):
            provider_message = _bounded_error_text(raw_error, limit=500)
    raise ProviderHTTPError(
        status_code=response.status_code,
        error_type=error_type,
        error_code=error_code,
        error_param=error_param,
        provider_message=provider_message,
        request_chars=request_chars,
        max_output_tokens=max_output_tokens,
        retry_after_seconds=_retry_after_seconds(response),
    )


def _provider_response_json(response: httpx.Response) -> dict[str, Any]:
    """Decode one successful provider response without exposing its body.

    A 2xx response is not a valid provider result until its protocol envelope
    is a JSON object.  Keep malformed bodies at the provider boundary so the
    loop can persist a typed provider failure instead of treating a JSON
    decoder implementation detail as a StateGraph crash.  The body itself may
    contain echoed prompts or credentials and is therefore never copied into
    the exception.
    """
    try:
        payload = response.json()
    except ValueError as exc:
        content_type = _bounded_error_text(response.headers.get("content-type"), limit=100)
        raise ProviderResponseError(
            "Provider response body was not valid JSON "
            f"(status_code={response.status_code}, content_type={content_type or 'unknown'}, "
            f"body_bytes={len(response.content)})"
        ) from exc
    if not isinstance(payload, dict):
        raise ProviderResponseError(
            "Provider response JSON must be an object "
            f"(status_code={response.status_code}, json_type={type(payload).__name__})"
        )
    return payload


def _provider_stream_event_json(raw: str, *, protocol: str) -> dict[str, Any]:
    """Decode one provider stream event without silently dropping corruption."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderResponseError(
            f"Provider {protocol} stream event was not valid JSON "
            f"(event_characters={len(raw)})"
        ) from exc
    if not isinstance(payload, dict):
        raise ProviderResponseError(
            f"Provider {protocol} stream event JSON must be an object "
            f"(json_type={type(payload).__name__})"
        )
    return payload


def _openai_sse_response_json(response: httpx.Response) -> dict[str, Any]:
    """Assemble one explicitly configured SSE completion into its JSON envelope."""
    events = _sse_event_data(response.text)
    if not events:
        raise ProviderResponseError(
            "Provider openai SSE response did not include data events "
            f"(status_code={response.status_code}, body_bytes={len(response.content)})"
        )
    content_parts: list[str] = []
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    completed = False
    for raw in events:
        if raw == "[DONE]":
            completed = True
            break
        chunk = _provider_stream_event_json(raw, protocol="openai")
        token, chunk_finish_reason, chunk_usage = _openai_sse_chunk_facts(chunk)
        if token:
            content_parts.append(token)
        if chunk_finish_reason is not None:
            finish_reason = chunk_finish_reason
        if chunk_usage is not None:
            usage = chunk_usage
    if not completed:
        raise ProviderResponseError("Provider openai SSE response ended without a DONE event")
    assembled: dict[str, Any] = {
        "choices": [
            {
                "message": {"content": "".join(content_parts)},
                "finish_reason": finish_reason,
            }
        ]
    }
    if usage is not None:
        assembled["usage"] = usage
    return assembled


def _openai_sse_chunk_facts(
    chunk: dict[str, Any],
) -> tuple[str, str | None, dict[str, Any] | None]:
    choices = chunk.get("choices")
    if choices is None:
        choices = []
    if not isinstance(choices, list) or len(choices) > 1:
        raise ProviderResponseError(
            "Provider openai stream choices must contain at most one item"
        )
    usage = chunk.get("usage")
    if usage is not None and not isinstance(usage, dict):
        raise ProviderResponseError("Provider openai stream usage must be an object")
    if not choices:
        return "", None, usage
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ProviderResponseError("Provider openai stream choice must be an object")
    delta = choice.get("delta")
    if delta is None:
        delta = {}
    if not isinstance(delta, dict):
        raise ProviderResponseError("Provider openai stream delta must be an object")
    content = delta.get("content")
    if content is None:
        content = ""
    if not isinstance(content, str):
        raise ProviderResponseError("Provider openai stream content must be a string")
    finish_reason = choice.get("finish_reason")
    if finish_reason is not None and not isinstance(finish_reason, str):
        raise ProviderResponseError("Provider openai stream finish reason must be a string")
    return content, finish_reason, usage


def _sse_event_data(text: str) -> list[str]:
    events: list[str] = []
    data_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if not line:
            if data_lines:
                events.append("\n".join(data_lines))
                data_lines = []
            continue
        if line.startswith("data:"):
            data_lines.append(line[len("data:") :].lstrip(" "))
            continue
        if line.startswith(("event:", "id:", "retry:", ":")):
            continue
        raise ProviderResponseError("Provider SSE response included an invalid field")
    if data_lines:
        events.append("\n".join(data_lines))
    return events


async def _iter_sse_event_data(lines: AsyncIterable[str]) -> AsyncGenerator[str]:
    data_lines: list[str] = []
    async for raw_line in lines:
        line = raw_line.rstrip("\r")
        if not line:
            if data_lines:
                yield "\n".join(data_lines)
                data_lines = []
            continue
        if line.startswith("data:"):
            data_lines.append(line[len("data:") :].lstrip(" "))
            continue
        if line.startswith(("event:", "id:", "retry:", ":")):
            continue
        raise ProviderResponseError("Provider SSE response included an invalid field")
    if data_lines:
        yield "\n".join(data_lines)


def _bounded_error_text(value: Any, *, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _retry_after_seconds(response: httpx.Response) -> float:
    raw_value = str(response.headers.get("retry-after") or "").strip()
    try:
        return max(0.0, float(raw_value))
    except ValueError:
        return 0.0


class ModelPool:
    def __init__(
        self,
        *,
        default: ChatProvider,
        routes: dict[str, ChatProvider] | None = None,
        config: ModelConfig | None = None,
        resource_gateway: GlobalResourceGateway | None = None,
    ):
        self.default = default
        self.routes = routes or {}
        self.config = config
        self.resource_gateway = resource_gateway or GlobalResourceGateway(ResourceLimits())
        self._resource_gateway_context: ContextVar[GlobalResourceGateway | None] = ContextVar(
            f"navi_model_pool_gateway_{id(self)}", default=None
        )
        self._usage_by_role: dict[str, dict[str, Any]] = {}

    def current_resource_gateway(self) -> GlobalResourceGateway:
        return self._resource_gateway_context.get() or self.resource_gateway

    @contextmanager
    def bind_resource_gateway(self, gateway: GlobalResourceGateway):
        token = self._resource_gateway_context.set(gateway)
        try:
            yield
        finally:
            self._resource_gateway_context.reset(token)

    async def complete_for(
        self,
        role: str,
        messages: list[ChatMessage],
        *,
        output_schema: dict[str, Any] | None = None,
    ) -> str:
        provider = self.routes.get(role, self.default)
        params = self.config.get_role_params(role) if self.config else {}
        temperature = params.get("temperature")
        max_tokens = params.get("max_tokens")
        role_request_options = params.get("request_options")
        gateway = self.current_resource_gateway()
        prompt_tokens = _estimate_prompt_tokens(messages)
        output_token_limit = max(0, int(max_tokens if max_tokens is not None else 32768))
        estimated_tokens = prompt_tokens + output_token_limit
        grant = gateway.request(
            ResourceRequest(
                kind=f"llm:{role}",
                estimated_tokens=estimated_tokens,
                estimated_cost=_model_request_cost(
                    params,
                    input_tokens=prompt_tokens,
                    output_tokens=output_token_limit,
                ),
                units=1,
            )
        )
        if not grant.allowed:
            raise ResourceLimitError(grant)
        try:
            result = await _complete_with_optional_schema(
                provider,
                messages,
                output_schema=output_schema,
                temperature=temperature,
                max_tokens=max_tokens,
                request_options=role_request_options,
            )
        except (ProviderHTTPError, httpx.HTTPError):
            gateway.release(grant_id=grant.grant_id)
            raise
        except Exception:
            gateway.release(grant_id=grant.grant_id)
            raise
        usage = provider.last_usage
        if usage is not None:
            facts = usage.to_facts(role=role)
            facts["messages"] = [{"role": m.role, "content": m.content} for m in messages]
            facts["response"] = result
            self._usage_by_role[role] = facts
            gateway.release(
                grant_id=grant.grant_id,
                actual_tokens=usage.total_tokens or usage.input_tokens + usage.output_tokens,
                actual_cost=_model_request_cost(
                    params,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                ),
            )
        else:
            self._usage_by_role.pop(role, None)
            gateway.release(grant_id=grant.grant_id)
        return result

    async def stream_for(
        self,
        role: str,
        messages: list[ChatMessage],
    ) -> AsyncGenerator[str]:
        """Route to the right provider by role and return its stream."""
        provider = self.routes.get(role, self.default)
        params = self.config.get_role_params(role) if self.config else {}
        temperature = params.get("temperature")
        max_tokens = params.get("max_tokens")
        role_request_options = params.get("request_options")
        gateway = self.current_resource_gateway()
        prompt_tokens = _estimate_prompt_tokens(messages)
        output_token_limit = max(0, int(max_tokens if max_tokens is not None else 32768))
        estimated_tokens = prompt_tokens + output_token_limit
        grant = gateway.request(
            ResourceRequest(
                kind=f"llm:{role}",
                estimated_tokens=estimated_tokens,
                estimated_cost=_model_request_cost(
                    params,
                    input_tokens=prompt_tokens,
                    output_tokens=output_token_limit,
                ),
                units=1,
            )
        )
        if not grant.allowed:
            raise ResourceLimitError(grant)
        try:
            async for token in provider.stream(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                request_options=role_request_options,
            ):
                yield token
        finally:
            usage = provider.last_usage
            gateway.release(
                grant_id=grant.grant_id,
                actual_tokens=(
                    usage.total_tokens or usage.input_tokens + usage.output_tokens
                    if usage is not None
                    else None
                ),
                actual_cost=(
                    _model_request_cost(
                        params,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                    )
                    if usage is not None
                    else None
                ),
            )

    def list_roles(self) -> list[str]:
        # "default" is this pool's declared default route; the agent role names
        # come from the declared AGENT_ROLES_SPEC rather than a hardcoded list.
        from .agent_roles import list_agent_role_names

        return sorted({"default", *list_agent_role_names(), *self.routes})

    def usage_for(self, role: str) -> dict[str, Any]:
        return dict(self._usage_by_role.get(role) or {})


PROVIDER_ADAPTERS: tuple[ProviderAdapter, ...] = (
    ProviderAdapter(
        "openai-compatible", lambda config, spec: OpenAICompatibleProvider(config, spec)
    ),
    ProviderAdapter(
        "anthropic-compatible", lambda config, spec: AnthropicCompatibleProvider(config, spec)
    ),
)


def build_provider(config: ModelConfig) -> ModelPool:
    return ModelPool(
        default=_build_single_provider(config),
        routes={
            role: _build_single_provider(route_config)
            for role, route_config in config.routes.items()
        },
        config=config,
    )


def _build_single_provider(config: ModelConfig) -> ChatProvider:
    resolved = resolve_model_config(config)
    spec = _provider_spec(resolved)
    for adapter in PROVIDER_ADAPTERS:
        if adapter.kind == spec.kind:
            return adapter.build(resolved, spec)
    raise ValueError(f"Unsupported provider kind: {spec.kind}")


def list_provider_specs() -> list[dict[str, Any]]:
    return _list_provider_specs()


def resolve_model_config(config: ModelConfig) -> ModelConfig:
    spec = _provider_spec(config)
    model = config.model or spec.default_model
    api_base_url = (config.api_base_url or spec.default_base_url).rstrip("/")
    return ModelConfig(
        provider=spec.name,
        model=model,
        api_base_url=api_base_url,
        api_key=config.api_key,
        kind=spec.kind,
        timeout_seconds=config.timeout_seconds,
        response_transport=config.response_transport,
        request_options=dict(config.request_options),
    )


def _provider_spec(config: ModelConfig) -> ProviderSpec:
    try:
        return get_provider_spec(config.provider)
    except ValueError:
        if not config.kind:
            raise
        return ProviderSpec(
            name=config.provider,
            kind=config.kind,
            default_model=config.model,
            default_base_url=config.api_base_url,
            structured_output=_default_structured_output(config.kind),
        )


def _apply_provider_request_options(
    payload: dict[str, Any],
    request_options: dict[str, Any],
) -> dict[str, Any]:
    """Add explicit adapter options without allowing protocol-field overrides."""
    conflicts = sorted(set(payload).intersection(request_options))
    if conflicts:
        raise ValueError(
            "model.request_options cannot override runtime fields: " + ", ".join(conflicts)
        )
    return {**payload, **request_options}


def _merge_request_options(
    global_options: dict[str, Any],
    role_options: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge role-specific request_options over global ones.

    A ``None`` value in *role_options* removes the key from the merged result,
    allowing a role to opt out of a global option (e.g. ``reasoning_effort``).
    """
    if not role_options:
        return dict(global_options)
    merged = dict(global_options)
    for key, value in role_options.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def _estimate_prompt_tokens(messages: list[ChatMessage]) -> int:
    # Cheap deterministic guardrail estimate; provider usage remains the source
    # of truth after the call.
    return max(1, sum(max(1, len(message.content) // 4) for message in messages))


def _model_request_cost(
    params: dict[str, Any],
    *,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """Calculate cost only from explicit configuration; zero means unknown."""
    try:
        input_rate = float(params.get("input_cost_per_million") or 0.0)
        output_rate = float(params.get("output_cost_per_million") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return (
        max(0, input_tokens) * input_rate / 1_000_000
        + max(0, output_tokens) * output_rate / 1_000_000
    )


async def _complete_with_optional_schema(
    provider: ChatProvider,
    messages: list[ChatMessage],
    *,
    output_schema: dict[str, Any] | None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    request_options: dict[str, Any] | None = None,
) -> str:
    requested_kwargs: dict[str, Any] = {}
    if output_schema is not None:
        requested_kwargs["output_schema"] = output_schema
    if temperature is not None:
        requested_kwargs["temperature"] = temperature
    if max_tokens is not None:
        requested_kwargs["max_tokens"] = max_tokens
    if request_options is not None:
        requested_kwargs["request_options"] = request_options
    accepted_kwargs = _accepted_complete_kwargs(provider, requested_kwargs)
    if output_schema is None:
        return await provider.complete(messages, **accepted_kwargs)
    result = await provider.complete(messages, **accepted_kwargs)
    # Post-hoc schema validation. For json_object-only
    # providers the schema is prompt-only, so the runtime validates the
    # returned JSON against the declared schema and rejects malformed output
    # as a tool-call parse failure rather than trusting prompt instructions.
    _validate_structured_output(result, output_schema)
    return result


def _accepted_complete_kwargs(
    provider: ChatProvider,
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    try:
        parameters = inspect.signature(provider.complete).parameters
    except (TypeError, ValueError):
        return kwargs
    if any(param.kind is param.VAR_KEYWORD for param in parameters.values()):
        return kwargs
    return {key: value for key, value in kwargs.items() if key in parameters}


def _validate_structured_output(content: str, output_schema: dict[str, Any]) -> None:
    schema = output_schema.get("schema", output_schema)
    if not isinstance(schema, dict):
        return
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise StructuredOutputError(f"structured output is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise StructuredOutputError("structured output must be a JSON object")
    errors = json_schema_errors(parsed, schema)
    if errors:
        detail = "; ".join(errors[:5])
        raise StructuredOutputError(f"structured output schema mismatch: {detail}")


def _extract_openai_content(data: dict[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderResponseError(
            f"Provider response did not include choices. Response shape: {_provider_response_shape(data)}"
        )
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ProviderResponseError(
            f"Provider response choice was not an object. Response shape: {_provider_response_shape(data)}"
        )
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ProviderResponseError(
            f"Provider response did not include a message object. Response shape: {_provider_response_shape(data)}"
        )

    if "content" not in message or message.get("content") is None:
        raise ProviderResponseError(
            "Provider response did not include message content. "
            f"Response shape: {_provider_response_shape(data)}"
        )

    content = message["content"]
    content_text = _openai_content_text(content)
    content_str = content_text.strip()
    if not content_str:
        finish_reason = choice.get("finish_reason", "unknown")
        raise ProviderResponseError(
            "Provider response content is empty. "
            f"Finish reason: {finish_reason}. Response shape: {_provider_response_shape(data)}"
        )
    return content_text


def _openai_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list) or not content:
        raise ProviderResponseError(
            "Provider response message content must be a non-empty string or text-block list"
        )
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict) or str(item.get("type") or "") not in {
            "text",
            "output_text",
        }:
            raise ProviderResponseError(
                "Provider response message content included a non-text block"
            )
        text = item.get("text")
        if not isinstance(text, str):
            raise ProviderResponseError(
                "Provider response message text block did not include string text"
            )
        parts.append(text)
    return "".join(parts)


def _openai_usage_facts(config: ModelConfig, data: dict[str, Any]) -> ProviderUsage | None:
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    input_tokens = _int_usage(usage.get("prompt_tokens"), field="prompt_tokens")
    output_tokens = _int_usage(usage.get("completion_tokens"), field="completion_tokens")
    total_tokens = _int_usage(usage.get("total_tokens"), field="total_tokens")
    return ProviderUsage(
        provider=config.provider,
        model=config.model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        raw={str(key): value for key, value in usage.items()},
    )


def _anthropic_usage_facts(config: ModelConfig, data: dict[str, Any]) -> ProviderUsage | None:
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    input_tokens = _int_usage(usage.get("input_tokens"), field="input_tokens")
    output_tokens = _int_usage(usage.get("output_tokens"), field="output_tokens")
    return ProviderUsage(
        provider=config.provider,
        model=config.model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        raw={str(key): value for key, value in usage.items()},
    )


def _int_usage(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProviderResponseError(
            f"Provider usage field {field} must be a non-negative integer"
        )
    return value


def _structured_response_format(
    spec: ProviderSpec, output_schema: dict[str, Any] | None
) -> dict[str, Any] | None:
    if not output_schema:
        return None
    mode = spec.structured_output
    if mode == "json_schema":
        return {
            "type": "json_schema",
            "json_schema": {
                "name": str(output_schema.get("name") or "navi_output")[:64],
                "strict": bool(output_schema.get("strict", False)),
                "schema": output_schema.get("schema") or {},
            },
        }
    if mode == "json_object":
        return {"type": "json_object"}
    return None


def _default_structured_output(kind: str) -> str:
    if kind == "openai-compatible":
        return "json_schema"
    if kind == "anthropic-compatible":
        return "tool_schema"
    return "none"


def _schema_name(output_schema: dict[str, Any] | None, *, default: str = "navi_output") -> str:
    if not output_schema:
        return default
    raw_name = str(output_schema.get("name") or default)
    name = re.sub(r"[^a-zA-Z0-9_-]", "_", raw_name)[:64].strip("_")
    return name or default


def _anthropic_structured_tool(
    spec: ProviderSpec,
    output_schema: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not output_schema or spec.structured_output != "tool_schema":
        return None
    schema = output_schema.get("schema")
    if not isinstance(schema, dict):
        schema = {"type": "object", "properties": {}}
    return {
        "name": _schema_name(output_schema),
        "description": "Return Navi machine-readable output.",
        "input_schema": schema,
    }


def _messages_for_response_format(
    messages: list[ChatMessage],
    response_format: dict[str, Any] | None,
) -> list[ChatMessage]:
    if not response_format or response_format.get("type") != "json_object":
        return messages
    instructions = ["JSON mode is enabled for this API request. Return a JSON object."]
    return [
        ChatMessage(
            "system",
            "\n\n".join(instructions),
        ),
        *messages,
    ]


def _anthropic_payload(
    model: str,
    messages: list[ChatMessage],
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    system_parts: list[str] = []
    conversation: list[dict[str, str]] = []
    for message in messages:
        if message.role == "system":
            system_parts.append(message.content)
            continue
        role = "assistant" if message.role == "assistant" else "user"
        conversation.append({"role": role, "content": message.content})
    payload = {
        "model": model,
        "max_tokens": 32768 if max_tokens is None else max_tokens,
        "system": "\n\n".join(system_parts),
        "messages": conversation or [{"role": "user", "content": ""}],
    }
    if temperature is not None:
        payload["temperature"] = temperature
    return payload


def _extract_anthropic_content(
    data: dict[str, Any],
    *,
    tool_name: str = "",
) -> str:
    blocks = data.get("content")
    if not isinstance(blocks, list) or any(not isinstance(block, dict) for block in blocks):
        raise ProviderResponseError(
            "Provider response content must be a list of objects. "
            f"Response shape: {_provider_response_shape(data)}"
        )
    if tool_name:
        for block in blocks:
            if block.get("type") == "tool_use" and block.get("name") == tool_name:
                tool_input = block.get("input")
                if isinstance(tool_input, dict):
                    return json.dumps(tool_input, ensure_ascii=False)
                raise ProviderResponseError(
                    f"Provider structured tool output {tool_name} was not an object. "
                    f"Response shape: {_provider_response_shape(data)}"
                )
        raise ProviderResponseError(
            f"Provider response did not include tool output {tool_name}. "
            f"Response shape: {_provider_response_shape(data)}"
        )
    text = "\n".join(
        str(block.get("text") or "") for block in blocks if block.get("type") == "text"
    ).strip()
    if not text:
        raise ProviderResponseError(
            "Provider response did not include text content. "
            f"Response shape: {_provider_response_shape(data)}"
        )
    return text


def _provider_response_shape(data: dict[str, Any]) -> dict[str, Any]:
    """Return protocol structure only; provider values can contain user secrets."""
    shape: dict[str, Any] = {
        "has_choices": "choices" in data,
        "has_content": "content" in data,
        "has_usage": "usage" in data,
    }
    choices = data.get("choices")
    shape["choices_type"] = type(choices).__name__
    if isinstance(choices, list):
        shape["choice_count"] = len(choices)
        if choices:
            shape["first_choice_type"] = type(choices[0]).__name__
            if isinstance(choices[0], dict):
                message = choices[0].get("message")
                shape["has_message"] = "message" in choices[0]
                shape["message_type"] = type(message).__name__
                if isinstance(message, dict):
                    message_content = message.get("content")
                    shape["has_message_content"] = "content" in message
                    shape["message_content_type"] = type(message_content).__name__
    content = data.get("content")
    shape["content_type"] = type(content).__name__
    if isinstance(content, list):
        shape["content_block_count"] = len(content)
        shape["content_block_types"] = [
            (
                str(block.get("type"))
                if isinstance(block, dict)
                and block.get("type") in {"text", "tool_use", "thinking", "redacted_thinking"}
                else "unknown"
            )
            for block in content[:10]
        ]
    return shape
