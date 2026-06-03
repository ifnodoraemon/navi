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
    match = re.search(r"^Current user message:\s*(.*?)(?:\nPermission ceiling:|\nAvailable tools:|\Z)", content, re.DOTALL)
    if match:
        return match.group(1).strip()
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
    code = _extract_approval_code(text)
    is_fix_follow_up = _has(text, "\u4fee\u590d", "\u5982\u4f55\u4fee", "fix")
    mentions_execution_protocol = _has(combined, "execution protocol", "navi_execution")

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
    if run_id and '"status": "prepared"' in observations:
        return _mock_syscall("approval.request", "prepare", {"run_id": run_id}, "mock follow-up requests approval")
    if run_id and '"status": "pending"' in observations:
        return _mock_syscall("delegate.prepare", "prepare", {"run_id": run_id}, "mock follow-up prepares spawned task")

    if _has(text, "\u6279\u51c6") and code:
        return _mock_syscall("approval.resolve", "write", {"decision": "approve", "code": code}, "mock approval decision")
    if _has(text, "\u62d2\u7edd") and code:
        return _mock_syscall("approval.resolve", "write", {"decision": "reject", "code": code}, "mock approval decision")

    if _has(text, "\u5168\u5c40\u6587\u4ef6\u7cfb\u7edf", "\u65e0\u4eba\u786e\u8ba4"):
        return _mock_syscall("clarify.ask", "read", {"message": "Please confirm the safe scope and approval boundary."}, "mock safety clarification")
    if _has(text, "\u660e\u5929"):
        return _mock_syscall("clarify.ask", "read", {"message": "Please provide an exact recurring schedule or reminder capability."}, "mock schedule clarification")
    if _looks_like_inventory_query(text) and _has(text.lower(), "skill"):
        return _mock_syscall("skills.list", "read", {}, "mock skills facts route")
    if _has(text.lower(), "browser operator") and _has(text, "\u8bf4\u660e"):
        return _mock_syscall("skills.view", "read", {"name": "Browser Operator"}, "mock skill view route")
    if (_looks_like_inventory_query(text) and _has(text, "\u5de5\u5177")) or _has(text, "\u53ef\u4ee5\u505a\u4ec0\u4e48"):
        return _mock_syscall("tools.list", "read", {}, "mock tools facts route")
    if _has(text, "\u8bb0\u5fc6") and _has(text, "\u5217\u51fa"):
        return _mock_syscall("memory.list", "read", {"status": "active"}, "mock memory list route")
    if _has(text, "\u56de\u5fc6") and _has(text, "\u8bb0\u5fc6"):
        return _mock_syscall("memory.recall", "read", {"query": text}, "mock memory recall route")
    url = _extract_first_url(text)
    if url and _has(text, "\u6293\u53d6"):
        return _mock_syscall("web.fetch", "read", {"url": url}, "mock web fetch route")
    if _has(text.lower(), "html") and _has(text, "\u63d0\u53d6"):
        return _mock_syscall("web.extract", "read", {"content": text}, "mock web extract route")
    if url and _has(text, "\u622a\u56fe", "screenshot"):
        return _mock_syscall("browser.screenshot", "read", {"url": url, "path": "example.png"}, "mock browser screenshot route")

    if mentions_execution_protocol and is_fix_follow_up:
        return _mock_syscall("delegate.spawn", "prepare", {"prompt": combined.strip()}, "mock execution protocol repair route")

    if _looks_like_one_shot_time(text):
        return _mock_syscall("watch.create", "prepare", {"kind": "once", "run_at_text": text, "prompt": text}, "mock one-shot watch route")

    if _has(text, "\u6bcf\u5929") and _has(text, "\u665a") and "8" in text:
        return _mock_syscall("watch.create", "prepare", {"kind": "recurring", "cron": "0 20 * * *", "prompt": text}, "mock watch route")

    if _has(text, "Telegram"):
        return _mock_syscall("connector.telegram.status", "read", {}, "mock connector status route")
    if _has(text, "\u5fae\u4fe1", "\u7f51\u5173"):
        return _mock_syscall("connector." + "weixin.status", "read", {}, "mock connector status route")
    if _has(text, "\u4e0d\u8981\u51c6\u5907\u6267\u884c"):
        return _mock_syscall("delegate.spawn", "prepare", {"prompt": text}, "mock delegation spawn route")
    if _has(text, "provider", "API key") or (_has(text, "\u8def\u7531") and not _has(text, "\u6f02\u79fb")):
        return _mock_syscall("provider.config", "read", {}, "mock provider config route")
    service_name = _extract_service_name(text)
    if service_name:
        return _mock_syscall("service.status", "read", {"name": service_name}, "mock service fact route")
    if _has(text, "README.md"):
        return _mock_syscall("file.read", "read", {"path": "README.md"}, "mock file read route")
    path = _extract_markdown_path(text)
    if path and _has(text, "\u5199\u5165"):
        return _mock_syscall("file.write", "write", {"path": path}, "mock file write route")
    if _has(text, "Python", "\u7248\u672c"):
        return _mock_syscall("shell.run", "write", {}, "mock shell route")
    if _has(text, "\u6d4b\u8bd5\u5957\u4ef6"):
        return _mock_syscall("test.run", "write", {}, "mock test route")
    if _has(text, "\u4ed3\u5e93") and _has(text, "\u5206\u652f", "\u672a\u63d0\u4ea4"):
        return _mock_syscall("git.status", "read", {}, "mock git fact route")
    if _has(text, "\u672c\u673a\u7684\u76ee\u5f55"):
        return _mock_syscall("filesystem.list", "read", {}, "mock filesystem fact route")

    if _has(text, "\u5220\u9664") and _has(text, "\u5b9a\u65f6"):
        watch_id = _extract_watch_id(combined)
        return _mock_syscall("watch.delete", "write", {"watch_id": watch_id}, "mock watch delete route")
    if _has(text, "\u6e05\u7406"):
        return _mock_syscall("delegate.delete", "write", {"status": "failed", "source": "watch"}, "mock delegation cleanup route")
    if _has(text, "\u5220\u9664") and run_id:
        return _mock_syscall("delegate.delete", "write", {"run_id": run_id}, "mock delegation delete route")
    if run_id and _has(text, "\u91cd\u8bd5"):
        return _mock_syscall("delegate.retry", "write", {"run_id": run_id}, "mock delegation retry route")
    if run_id and _has(text, "\u53d1\u8d77\u5ba1\u6279"):
        return _mock_syscall("approval.request", "prepare", {"run_id": run_id}, "mock approval request route")
    if run_id and _has(text, "\u51c6\u5907\u5206\u6790"):
        return _mock_syscall("delegate.prepare", "prepare", {"run_id": run_id}, "mock delegation prepare route")
    if run_id and _has(text, "\u6267\u884c\u6388\u6743", "\u52a0\u5165\u961f\u5217"):
        return _mock_syscall("delegate.run", "write", {"run_id": run_id}, "mock delegation run route")
    if run_id and _has(text, "\u6ca1\u6709\u6267\u884c", "\u8fd8\u6ca1\u6267\u884c"):
        return _mock_syscall("delegate.status", "read", {"run_id": run_id}, "mock delegation status route")
    if _has(text, "\u54ea\u4e9b\u4efb\u52a1"):
        return _mock_syscall("delegate.list", "read", {}, "mock delegation list route")

    if _has(text, "\u6bcf\u5929"):
        return _mock_syscall("clarify.ask", "read", {"message": "Please provide the exact time."}, "mock recurring clarification")

    if _has(
        text,
        "\u4e0d\u8981\u51c6\u5907\u6267\u884c",
        "\u8c03\u67e5",
        "\u63d0\u793a\u6ce8\u5165",
        "\u5168\u9762",
        "\u5931\u8d25",
        "\u5b9a\u4f4d\u95ee\u9898",
    ):
        return _mock_syscall("delegate.spawn", "prepare", {"prompt": text}, "mock delegation spawn route")

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


def _has(text: str, *needles: str) -> bool:
    return any(needle in text for needle in needles)


def _looks_like_inventory_query(text: str) -> bool:
    lowered = text.lower()
    return _has(lowered, "list", "available") or _has(
        text,
        "\u54ea\u4e9b",
        "\u6709\u4ec0\u4e48",
        "\u5217\u4e00\u4e0b",
        "\u6e05\u5355",
    )


def _extract_any_run_id(text: str) -> str:
    marked = re.search(r"\bdelegation\s+run\s+([a-f0-9]{32})\b", text)
    if marked:
        return marked.group(1)
    match = re.search(r"\b[a-f0-9]{32}\b", text)
    return match.group(0) if match else ""


def _extract_first_url(text: str) -> str:
    match = re.search(r"https?://[^\s，。；,;]+", text)
    return match.group(0).rstrip("'\"）)") if match else ""


def _extract_watch_id(text: str) -> str:
    match = re.search(r"\bwatch\s+([a-f0-9]{32})\b", text)
    return match.group(1) if match else ""


def _looks_like_one_shot_time(text: str) -> bool:
    if _has(text, "\u6bcf\u5929", "\u6bcf\u5468", "\u6bcf\u6708", "\u6bcf\u5e74"):
        return False
    return bool(re.search(r"(^|\D)([01]?\d|2[0-3])[:：][0-5]\d", text) or re.search(r"\d{1,2}\s*(?:\u70b9|\u65f6)", text))


def _extract_approval_code(text: str) -> str:
    match = re.search(r"\b\d{6}\b", text)
    return match.group(0) if match else ""


def _extract_service_name(text: str) -> str:
    match = re.search(r"\b[\w.-]+\.service\b", text)
    return match.group(0) if match else ""


def _extract_markdown_path(text: str) -> str:
    match = re.search(r"\b[\w./-]+\.md\b", text)
    return match.group(0) if match else ""


def _extract_required_execution_phase(system: str) -> str:
    match = re.search(r"`navi_execution\.phase` must be `([^`]+)`", system)
    return match.group(1) if match else "execute"


def _extract_run_id(user: str) -> str:
    match = re.search(r"^Run id:\s*(\S+)", user, flags=re.MULTILINE)
    return match.group(1) if match else ""
