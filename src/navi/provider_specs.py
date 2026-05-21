from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .spec_loader import load_spec


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    kind: str
    default_model: str
    default_base_url: str
    api_key_env: tuple[str, ...] = ()


PROVIDER_SPECS: tuple[ProviderSpec, ...] = tuple(
    ProviderSpec(
        name=str(item["name"]),
        kind=str(item["kind"]),
        default_model=str(item["default_model"]),
        default_base_url=str(item["default_base_url"]),
        api_key_env=tuple(item.get("api_key_env") or ()),
    )
    for item in load_spec("model_providers.yaml")
)


def get_provider_spec(provider: str) -> ProviderSpec:
    for spec in PROVIDER_SPECS:
        if provider == spec.name:
            return spec
    raise ValueError(f"Unsupported model provider: {provider}")


def list_provider_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": spec.name,
            "kind": spec.kind,
            "default_model": spec.default_model,
            "default_base_url": spec.default_base_url,
            "api_key_env": list(spec.api_key_env),
        }
        for spec in PROVIDER_SPECS
    ]
