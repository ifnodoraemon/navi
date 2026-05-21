from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any, Protocol

import httpx

from .config import ModelConfig
from .provider_specs import ProviderSpec, get_provider_spec, list_provider_specs as _list_provider_specs


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str


class ChatProvider(Protocol):
    async def complete(self, messages: list[ChatMessage]) -> str:
        ...


ProviderFactory = Callable[[ModelConfig, ProviderSpec], ChatProvider]


@dataclass(frozen=True)
class ProviderAdapter:
    kind: str
    build: ProviderFactory


class MockProvider:
    async def complete(self, messages: list[ChatMessage]) -> str:
        if messages and "model syscall planner" in messages[0].content:
            text = _extract_planner_user_message(
                next((msg.content for msg in reversed(messages) if msg.role == "user"), "")
            )
            return json.dumps(
                {
                    "tool": "final.answer",
                    "permission": "read",
                    "args": {"message": f"Navi received: {text}"},
                    "confidence": 1.0,
                    "reason": "mock planner fallback",
                },
                ensure_ascii=False,
            )
        last = next((msg.content for msg in reversed(messages) if msg.role == "user"), "")
        return f"Navi received: {last}"


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

    async def complete(self, messages: list[ChatMessage]) -> str:
        if not self.config.api_key:
            env_hint = " or ".join(self.spec.api_key_env) or "NAVI_MODEL_API_KEY"
            raise RuntimeError(f"{env_hint} is required for {self.config.provider} provider")
        payload = {
            "model": self.config.model,
            "messages": [{"role": msg.role, "content": msg.content} for msg in messages],
            "temperature": 0,
        }
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        async with httpx.AsyncClient(timeout=60, transport=self.transport) as client:
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

    async def complete(self, messages: list[ChatMessage]) -> str:
        if not self.config.api_key:
            env_hint = " or ".join(self.spec.api_key_env) or "ANTHROPIC_API_KEY"
            raise RuntimeError(f"{env_hint} is required for {self.config.provider} provider")
        payload = _anthropic_payload(self.config.model, messages)
        headers = {
            "x-api-key": self.config.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        async with httpx.AsyncClient(timeout=60, transport=self.transport) as client:
            response = await client.post(
                f"{self.config.api_base_url}/messages",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
        return _extract_anthropic_content(data)


PROVIDER_ADAPTERS: tuple[ProviderAdapter, ...] = (
    ProviderAdapter("mock", lambda config, spec: MockProvider()),
    ProviderAdapter("openai-compatible", lambda config, spec: OpenAICompatibleProvider(config, spec)),
    ProviderAdapter("anthropic-compatible", lambda config, spec: AnthropicCompatibleProvider(config, spec)),
)


def build_provider(config: ModelConfig) -> ChatProvider:
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
    model = config.model
    if not model or model == "mock" or (spec.name == "deepseek" and model == "deepseek-chat"):
        model = spec.default_model
    api_base_url = (config.api_base_url or spec.default_base_url).rstrip("/")
    if api_base_url in {"", "https://api.openai.com/v1"} and spec.default_base_url:
        api_base_url = spec.default_base_url
    api_key = config.api_key or _first_env(spec.api_key_env)
    return ModelConfig(
        provider=spec.name,
        model=model,
        api_base_url=api_base_url,
        api_key=api_key,
        kind=spec.kind,
    )


def _provider_spec(config: ModelConfig) -> ProviderSpec:
    try:
        return get_provider_spec(config.provider)
    except ValueError:
        if not config.kind:
            raise
        return ProviderSpec(
            name=config.provider,
            aliases=(),
            kind=config.kind,
            default_model=config.model,
            default_base_url=config.api_base_url,
            api_key_env=("NAVI_MODEL_API_KEY",),
        )


def _first_env(names: tuple[str, ...]) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return ""


def _extract_openai_content(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"Provider response did not include choices: {data}")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if content is None:
        raise RuntimeError(f"Provider response did not include message content: {data}")
    return str(content)


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
        "max_tokens": 4096,
        "system": "\n\n".join(system_parts),
        "messages": conversation or [{"role": "user", "content": ""}],
    }


def _extract_anthropic_content(data: dict[str, Any]) -> str:
    blocks = data.get("content") or []
    text = "\n".join(str(block.get("text") or "") for block in blocks if block.get("type") == "text").strip()
    if not text:
        raise RuntimeError(f"Provider response did not include text content: {data}")
    return text


def _extract_planner_user_message(content: str) -> str:
    match = re.search(r"^Current user message:\s*(.*?)(?:\nPermission ceiling:|\nAvailable tools:|\Z)", content, re.DOTALL)
    if match:
        return match.group(1).strip()
    return content.strip()
