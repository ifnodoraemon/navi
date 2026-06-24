from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any, Protocol

import httpx
import logging

from .config import ModelConfig
from .provider_specs import (
    ProviderSpec,
    get_provider_spec,
    list_provider_specs as _list_provider_specs,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str


class ChatProvider(Protocol):
    async def complete(
        self, messages: list[ChatMessage], *, output_schema: dict[str, Any] | None = None
    ) -> str: ...


ProviderFactory = Callable[[ModelConfig, ProviderSpec], ChatProvider]


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

    async def complete(
        self, messages: list[ChatMessage], *, output_schema: dict[str, Any] | None = None
    ) -> str:
        if not self.config.api_key:
            env_hint = " or ".join(self.spec.api_key_env) or "NAVI_MODEL_API_KEY"
            raise RuntimeError(f"{env_hint} is required for {self.config.provider} provider")
        payload = {
            "model": self.config.model,
            "messages": [{"role": msg.role, "content": msg.content} for msg in messages],
            "temperature": 0,
            "max_tokens": 65536,
        }
        structured_format = _structured_response_format(self.spec, output_schema)
        if structured_format:
            payload["response_format"] = structured_format
        outbound_messages = _messages_for_response_format(
            messages, structured_format, output_schema
        )
        payload["messages"] = [
            {"role": msg.role, "content": msg.content} for msg in outbound_messages
        ]
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        async with httpx.AsyncClient(
            timeout=self.config.timeout_seconds, transport=self.transport
        ) as client:
            response = await client.post(
                f"{self.config.api_base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
        return _extract_openai_content(data)


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

    async def complete(
        self, messages: list[ChatMessage], *, output_schema: dict[str, Any] | None = None
    ) -> str:
        if not self.config.api_key:
            env_hint = " or ".join(self.spec.api_key_env) or "ANTHROPIC_API_KEY"
            raise RuntimeError(f"{env_hint} is required for {self.config.provider} provider")
        payload = _anthropic_payload(self.config.model, messages)
        structured_tool = _anthropic_structured_tool(self.spec, output_schema)
        if structured_tool:
            payload["tools"] = [structured_tool]
            payload["tool_choice"] = {"type": "tool", "name": structured_tool["name"]}
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
            response.raise_for_status()
            data = response.json()
        return _extract_anthropic_content(
            data,
            tool_name=structured_tool["name"] if structured_tool else "",
        )


class FallbackProvider:
    def __init__(self, providers: list[ChatProvider]):
        self.providers = providers

    async def complete(
        self, messages: list[ChatMessage], *, output_schema: dict[str, Any] | None = None
    ) -> str:
        import asyncio
        import httpx

        from .safeguards import redact_secrets

        errors: list[str] = []
        max_retries = 3

        for provider in self.providers:
            for attempt in range(max_retries):
                try:
                    return await _complete_with_optional_schema(
                        provider, messages, output_schema=output_schema
                    )
                except Exception as exc:
                    if isinstance(exc, httpx.HTTPStatusError):
                        status = exc.response.status_code
                        if status not in (429, 500, 502, 503, 504):
                            errors.append(
                                redact_secrets(f"{provider.__class__.__name__}: {exc}")
                            )
                            break  # don't retry on 400, 401, 403 etc.

                    if attempt == max_retries - 1:
                        errors.append(
                            redact_secrets(f"{provider.__class__.__name__}: {exc}")
                        )
                    else:
                        logger.warning(
                            f"Provider {provider.__class__.__name__} failed (attempt {attempt + 1}/{max_retries}): {exc}. Retrying..."
                        )
                        await asyncio.sleep(2**attempt)  # 1s, 2s

        raise RuntimeError("all model providers failed: " + "; ".join(errors))


class ModelPool:
    def __init__(self, *, default: ChatProvider, routes: dict[str, ChatProvider] | None = None):
        self.default = default
        self.routes = routes or {}

    async def complete_for(
        self,
        role: str,
        messages: list[ChatMessage],
        *,
        output_schema: dict[str, Any] | None = None,
    ) -> str:
        provider = self.routes.get(role, self.default)
        return await _complete_with_optional_schema(provider, messages, output_schema=output_schema)

    def list_roles(self) -> list[str]:
        # "default" is this pool's own routing fallback key; the agent role names
        # come from the declared AGENT_ROLES_SPEC rather than a hardcoded list.
        from .agent_roles import list_agent_role_names

        return sorted({"default", *list_agent_role_names(), *self.routes})


PROVIDER_ADAPTERS: tuple[ProviderAdapter, ...] = (
    ProviderAdapter(
        "openai-compatible", lambda config, spec: OpenAICompatibleProvider(config, spec)
    ),
    ProviderAdapter(
        "anthropic-compatible", lambda config, spec: AnthropicCompatibleProvider(config, spec)
    ),
)


def build_provider(config: ModelConfig) -> ModelPool:
    default = _build_fallback_chain(config)
    return ModelPool(
        default=default,
        routes={
            role: _build_fallback_chain(route_config)
            for role, route_config in config.routes.items()
        },
    )


def _build_fallback_chain(config: ModelConfig) -> ChatProvider:
    providers = [
        _build_single_provider(config),
        *[_build_single_provider(item) for item in config.fallbacks],
    ]
    # Always wrap in FallbackProvider, even for a single provider. The
    # FallbackProvider retries on transport-layer failures
    # (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError,
    # httpx.ReadTimeout) up to 3x with exponential backoff. Without this
    # wrapper, a single-provider config has no retry — a transient
    # ``peer closed connection without sending complete message body``
    # becomes a terminal planner failure.
    return FallbackProvider(providers)


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
    api_key = config.api_key or _first_env(spec.api_key_env)
    return ModelConfig(
        provider=spec.name,
        model=model,
        api_base_url=api_base_url,
        api_key=api_key,
        kind=spec.kind,
        timeout_seconds=config.timeout_seconds,
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
            api_key_env=("NAVI_MODEL_API_KEY",),
            structured_output=_default_structured_output(config.kind),
        )


def _first_env(names: tuple[str, ...]) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return ""


async def _complete_with_optional_schema(
    provider: ChatProvider,
    messages: list[ChatMessage],
    *,
    output_schema: dict[str, Any] | None,
) -> str:
    if output_schema is None:
        return await provider.complete(messages)
    try:
        result = await provider.complete(messages, output_schema=output_schema)
    except TypeError as exc:
        if "output_schema" not in str(exc) and "unexpected keyword" not in str(exc):
            raise
        result = await provider.complete(messages)
    # Post-hoc schema validation (principle 14/16). For json_object-only
    # providers the schema is prompt-only, so the runtime validates the
    # returned JSON against the declared schema and rejects malformed output
    # as a tool-call parse failure rather than trusting prompt instructions.
    _validate_structured_output(result, output_schema)
    return result


def _validate_structured_output(content: str, output_schema: dict[str, Any]) -> None:
    schema = output_schema.get("schema", output_schema)
    if not isinstance(schema, dict):
        return
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"structured output is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("structured output must be a JSON object")
    required = schema.get("required")
    if isinstance(required, list) and required:
        missing = [key for key in required if key not in parsed]
        if missing:
            raise RuntimeError(
                f"structured output missing required keys: {missing}"
            )


def _extract_openai_content(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"Provider response did not include choices: {data}")
    choice = choices[0]
    message = choice.get("message") or {}
    content = message.get("content")

    # Some OpenAI-compatible wrappers (like Gemini) might return structured output as a tool call
    if not content or not str(content).strip():
        tool_calls = message.get("tool_calls")
        if tool_calls and isinstance(tool_calls, list) and len(tool_calls) > 0:
            function = tool_calls[0].get("function") or {}
            arguments = function.get("arguments")
            if arguments:
                return str(arguments)

    if content is None:
        raise RuntimeError(f"Provider response did not include message content: {data}")

    content_str = str(content).strip()
    if not content_str:
        reasoning_content = str(message.get("reasoning_content") or "").strip()
        if reasoning_content:
            from .json_utils import parse_first_json_object

            structured = parse_first_json_object(reasoning_content)
            if structured is not None:
                return json.dumps(structured, ensure_ascii=False)
        finish_reason = choice.get("finish_reason", "unknown")
        raise RuntimeError(
            f"Provider response content is empty. Finish reason: {finish_reason}. Raw data: {data}"
        )
    return str(content)


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
    output_schema: dict[str, Any] | None = None,
) -> list[ChatMessage]:
    if not response_format or response_format.get("type") != "json_object":
        return messages
    instructions = ["JSON mode is enabled for this API request."]
    if output_schema:
        instructions.append(
            "You MUST return ONLY a JSON object that strictly matches the following JSON Schema:"
        )
        instructions.append(json.dumps(output_schema, ensure_ascii=False, indent=2))
    return [
        ChatMessage(
            "system",
            "\n\n".join(instructions),
        ),
        *messages,
    ]


def _anthropic_payload(model: str, messages: list[ChatMessage]) -> dict[str, Any]:
    system_parts: list[str] = []
    conversation: list[dict[str, str]] = []
    for message in messages:
        if message.role == "system":
            system_parts.append(message.content)
            continue
        role = "assistant" if message.role == "assistant" else "user"
        conversation.append({"role": role, "content": message.content})
    return {
        "model": model,
        "max_tokens": 65536,
        "system": "\n\n".join(system_parts),
        "messages": conversation or [{"role": "user", "content": ""}],
    }


def _extract_anthropic_content(data: dict[str, Any], *, tool_name: str = "") -> str:
    blocks = data.get("content") or []
    if tool_name:
        for block in blocks:
            if block.get("type") == "tool_use" and block.get("name") == tool_name:
                tool_input = block.get("input")
                if isinstance(tool_input, dict):
                    return json.dumps(tool_input, ensure_ascii=False)
        raise RuntimeError(f"Provider response did not include tool output {tool_name}: {data}")
    text = "\n".join(
        str(block.get("text") or "") for block in blocks if block.get("type") == "text"
    ).strip()
    if not text:
        raise RuntimeError(f"Provider response did not include text content: {data}")
    return text


def _extract_planner_user_message(content: str) -> str:
    tagged = re.search(r"<user_message>\s*(.*?)\s*</user_message>", content, re.DOTALL)
    if tagged:
        return tagged.group(1).strip()
    return content.strip()


def _extract_planner_conversation_context(content: str) -> str:
    tagged = re.search(
        r"<conversation_history>\s*(.*?)\s*</conversation_history>", content, re.DOTALL
    )
    return tagged.group(1).strip() if tagged else ""


def _extract_planner_observations(content: str) -> str:
    tagged = re.search(r"<observed_facts>\s*(.*?)\s*</observed_facts>", content, re.DOTALL)
    return tagged.group(1).strip() if tagged else ""


def _extract_required_execution_phase(system: str) -> str:
    match = re.search(r"`navi_execution\.phase` must be `([^`]+)`", system)
    return match.group(1) if match else "execute"


def _execution_phase_from_output_schema(output_schema: dict[str, Any] | None) -> str:
    if not output_schema:
        return ""
    name = str(output_schema.get("name") or "")
    match = re.fullmatch(r"navi_(prepare|execute)_execution", name)
    if match:
        return match.group(1)
    schema = output_schema.get("schema")
    if not isinstance(schema, dict):
        return ""
    navi_execution = (schema.get("properties") or {}).get("navi_execution")
    if not isinstance(navi_execution, dict):
        return ""
    phase = ((navi_execution.get("properties") or {}).get("phase") or {}).get("enum")
    if isinstance(phase, list) and phase:
        return str(phase[0])
    return ""


def _extract_run_id(user: str) -> str:
    match = re.search(r"^Run id:\s*(\S+)", user, flags=re.MULTILINE)
    return match.group(1) if match else ""
