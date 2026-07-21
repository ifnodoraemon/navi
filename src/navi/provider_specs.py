from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .specs_data import MODEL_PROVIDERS_SPEC


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    kind: str
    default_model: str
    default_base_url: str
    structured_output: str = "none"


PROVIDER_SPECS: tuple[ProviderSpec, ...] = tuple(
    ProviderSpec(
        name=str(item["name"]),
        kind=str(item["kind"]),
        default_model=str(item["default_model"]),
        default_base_url=str(item["default_base_url"]),
        structured_output=str(item.get("structured_output") or "none"),
    )
    for item in MODEL_PROVIDERS_SPEC
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
            "structured_output": spec.structured_output,
        }
        for spec in PROVIDER_SPECS
    ]
