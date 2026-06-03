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
            user_prompt = next((msg.content for msg in reversed(messages) if msg.role == "user"), "")
            text = _extract_planner_user_message(user_prompt)
            context = _extract_planner_conversation_context(user_prompt)
            observations = _extract_planner_observations(user_prompt)
            return json.dumps(_mock_planner_syscall(text, context, observations), ensure_ascii=False)
        last = next((msg.content for msg in reversed(messages) if msg.role == "user"), "")
        system = messages[0].content if messages else ""
        if "navi_execution" in system:
            phase = _extract_required_execution_phase(system)
            run_id = _extract_run_id(last)
            return json.dumps(
                {
                    "navi_execution": {
                        "version": "navi.actuator.v1",
                        "phase": phase,
                        "run_id": run_id,
                        "plan_id": f"{phase}:{run_id or 'mock'}",
                        "steps": [
                            {
                                "id": "respond",
                                "actions": [
                                    {
                                        "tool": "final.answer",
                                        "permission": "read",
                                        "args": {"message": f"Navi received: {last}"},
                                        "target": run_id,
                                    }
                                ],
                                "verification": {"checks": [], "reason": "mock provider response"},
                                "on_failure": "stop",
                            }
                        ],
                        "evidence": [
                            {
                                "kind": "mock_provider",
                                "summary": f"Navi received: {last}",
                            }
                        ],
                        "verification": {
                            "status": "completed",
                            "checks": ["mock provider response"],
                            "reason": "mock model execution",
                        },
                        "completion": {
                            "status": "completed",
                            "summary": f"Navi received: {last}",
                        },
                    }
                },
                ensure_ascii=False,
            )
        observation_answer = _mock_observation_answer(last)
        if observation_answer:
            return observation_answer
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
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds, transport=self.transport) as client:
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
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds, transport=self.transport) as client:
            response = await client.post(
                f"{self.config.api_base_url}/messages",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
        return _extract_anthropic_content(data)


class FallbackProvider:
    def __init__(self, providers: list[ChatProvider]):
        self.providers = providers

    async def complete(self, messages: list[ChatMessage]) -> str:
        errors: list[str] = []
        for provider in self.providers:
            try:
                return await provider.complete(messages)
            except Exception as exc:
                errors.append(f"{provider.__class__.__name__}: {exc}")
        raise RuntimeError("all model providers failed: " + "; ".join(errors))


class ModelPool:
    def __init__(self, *, default: ChatProvider, routes: dict[str, ChatProvider] | None = None):
        self.default = default
        self.routes = routes or {}

    async def complete_for(self, role: str, messages: list[ChatMessage]) -> str:
        return await self.routes.get(role, self.default).complete(messages)

    def list_roles(self) -> list[str]:
        return sorted({"default", "planner", "responder", "notification", *self.routes})


PROVIDER_ADAPTERS: tuple[ProviderAdapter, ...] = (
    ProviderAdapter("mock", lambda config, spec: MockProvider()),
    ProviderAdapter("openai-compatible", lambda config, spec: OpenAICompatibleProvider(config, spec)),
    ProviderAdapter("anthropic-compatible", lambda config, spec: AnthropicCompatibleProvider(config, spec)),
)


def build_provider(config: ModelConfig) -> ModelPool:
    default = _build_fallback_chain(config)
    return ModelPool(
        default=default,
        routes={role: _build_fallback_chain(route_config) for role, route_config in config.routes.items()},
    )


def _build_fallback_chain(config: ModelConfig) -> ChatProvider:
    providers = [_build_single_provider(config), *[_build_single_provider(item) for item in config.fallbacks]]
    if len(providers) == 1:
        return providers[0]
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
    tagged = re.search(r"<user_message>\s*(.*?)\s*</user_message>", content, re.DOTALL)
    if tagged:
        return tagged.group(1).strip()
    return content.strip()


def _extract_planner_conversation_context(content: str) -> str:
    tagged = re.search(r"<conversation_history>\s*(.*?)\s*</conversation_history>", content, re.DOTALL)
    return tagged.group(1).strip() if tagged else ""


def _extract_planner_observations(content: str) -> str:
    tagged = re.search(r"<observed_facts>\s*(.*?)\s*</observed_facts>", content, re.DOTALL)
    return tagged.group(1).strip() if tagged else ""


def _mock_planner_syscall(text: str, context: str = "", observations: str = "") -> dict[str, Any]:
    combined = f"{context}\n{observations}\n{text}"
    run_id = _extract_any_run_id(combined)

    if '"capability": "skills.list"' in observations or '"capability": "tools.list"' in observations:
        return _mock_syscall(
            "final.answer",
            "read",
            {"message": _mock_observation_answer(observations)},
            "mock inventory facts are sufficient",
        )
    if run_id and '"status": "awaiting_approval"' in observations:
        return _mock_syscall(
            "final.answer",
            "read",
            {"message": f"Delegation run {run_id} is prepared and awaiting approval."},
            "mock follow-up reports approval needed",
        )
    if run_id and '"approval_status": "approved"' in observations and '"run_status": "queued"' in observations:
        return _mock_syscall(
            "final.answer",
            "read",
            {"message": f"Delegation run {run_id} has been approved and queued."},
            "mock follow-up reports queued task",
        )
    if '"watch_id":' in observations:
        return _mock_syscall(
            "final.answer",
            "read",
            {"message": "Recurring watch has been created."},
            "mock follow-up reports created watch",
        )
    if '"cleanup_complete": true' in observations:
        return _mock_syscall(
            "final.answer",
            "read",
            {"message": "Failed delegation records have been cleaned up."},
            "mock follow-up reports completed cleanup",
        )
    if run_id and '"status": "prepared"' in observations:
        return _mock_syscall("approval.request", "prepare", {"run_id": run_id}, "mock follow-up requests approval")
    if run_id and '"status": "pending"' in observations:
        return _mock_syscall("delegate.prepare", "prepare", {"run_id": run_id}, "mock follow-up prepares spawned task")

    return _mock_syscall(
        "final.answer",
        "read",
        {"message": f"Navi received: {text}"},
        "mock planner fallback",
    )


def _mock_observation_answer(text: str) -> str:
    if '"capability": "skills.list"' in text:
        names = _extract_json_string_values(text, "name")
        descriptions = _extract_json_string_values(text, "description")
        pairs = [f"{name}: {descriptions[index]}" for index, name in enumerate(names) if name and index < len(descriptions)]
        detail = "; ".join(pairs) if pairs else "no installed skills"
        return f"Skills are procedural guidance packages, separate from callable tools. Installed skills: {detail}."
    if '"capability": "tools.list"' in text:
        names = _extract_json_string_values(text, "name")
        selected = [name for name in names if name in {"watch.create", "delegate.spawn", "delegate.list", "delegate.status", "service.status", "skills.list", "tools.list"}]
        if not selected:
            selected = names[:8]
        return (
            "Tools are callable capabilities, separate from skills. Available tools include "
            f"{', '.join(selected)}. watch.create supports kind=once for one-shot reminders and "
            "kind=recurring with cron for explicit recurring schedules."
        )
    return ""


def _extract_json_string_values(text: str, key: str) -> list[str]:
    pattern = re.compile(rf'"{re.escape(key)}"\s*:\s*"([^"]*)"')
    return [match.group(1) for match in pattern.finditer(text)]


def _mock_syscall(tool: str, permission: str, args: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "tool": tool,
        "permission": permission,
        "args": args,
        "model_role": "responder",
        "confidence": 1.0,
        "reason": reason,
    }


def _extract_any_run_id(text: str) -> str:
    marked = re.search(r"\bdelegation\s+run\s+([a-f0-9]{32})\b", text)
    if marked:
        return marked.group(1)
    match = re.search(r"\b[a-f0-9]{32}\b", text)
    return match.group(0) if match else ""


def _extract_required_execution_phase(system: str) -> str:
    match = re.search(r"`navi_execution\.phase` must be `([^`]+)`", system)
    return match.group(1) if match else "execute"


def _extract_run_id(user: str) -> str:
    match = re.search(r"^Run id:\s*(\S+)", user, flags=re.MULTILINE)
    return match.group(1) if match else ""
