from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    aliases: tuple[str, ...]
    kind: str
    default_model: str
    default_base_url: str
    api_key_env: tuple[str, ...] = ()

    def matches(self, provider: str) -> bool:
        return provider == self.name or provider in self.aliases


PROVIDER_SPECS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        name="mock",
        aliases=("local-mock",),
        kind="mock",
        default_model="mock",
        default_base_url="",
    ),
    ProviderSpec(
        name="openai-compatible",
        aliases=("openai",),
        kind="openai-compatible",
        default_model="gpt-4o-mini",
        default_base_url="https://api.openai.com/v1",
        api_key_env=("NAVI_MODEL_API_KEY", "OPENAI_API_KEY"),
    ),
    ProviderSpec(
        name="deepseek",
        aliases=(),
        kind="openai-compatible",
        default_model="deepseek-v4-pro",
        default_base_url="https://api.deepseek.com",
        api_key_env=("DEEPSEEK_API_KEY", "NAVI_MODEL_API_KEY"),
    ),
)


def get_provider_spec(provider: str) -> ProviderSpec:
    for spec in PROVIDER_SPECS:
        if spec.matches(provider):
            return spec
    raise ValueError(f"Unsupported model provider: {provider}")


def list_provider_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": spec.name,
            "aliases": list(spec.aliases),
            "kind": spec.kind,
            "default_model": spec.default_model,
            "default_base_url": spec.default_base_url,
            "api_key_env": list(spec.api_key_env),
        }
        for spec in PROVIDER_SPECS
    ]
