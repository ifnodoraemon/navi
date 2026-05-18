from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .config import ModelConfig
from .provider_specs import ProviderSpec, get_provider_spec, list_provider_specs


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str


class ChatProvider(Protocol):
    async def complete(self, messages: list[ChatMessage]) -> str:
        ...


class MockProvider:
    async def complete(self, messages: list[ChatMessage]) -> str:
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


def build_provider(config: ModelConfig) -> ChatProvider:
    resolved = resolve_model_config(config)
    spec = get_provider_spec(resolved.provider)
    if spec.kind == "mock":
        return MockProvider()
    if spec.kind == "openai-compatible":
        return OpenAICompatibleProvider(resolved, spec)
    raise ValueError(f"Unsupported provider kind: {spec.kind}")


def resolve_model_config(config: ModelConfig) -> ModelConfig:
    spec = get_provider_spec(config.provider)
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
