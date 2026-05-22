from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import load_config
from .operating_context import OperatingContext, PromptLayer, render_prompt_layers
from .service import systemd_user_unit_path
from .spec_loader import load_spec


class PromptLayerStore:
    def __init__(self, home: Path):
        self.home = home
        self.overrides_dir = home / "prompt_layers"

    def get(self, name: str) -> PromptLayer:
        override = self.override_path(name)
        if override.exists():
            return PromptLayer(name, override.read_text(encoding="utf-8"), self._default_permission(name))
        spec = self._default_spec(name)
        return PromptLayer(
            name,
            str(spec.get("content") or ""),
            str(spec.get("minimum_permission") or "read"),
        )

    def read(self, name: str) -> str:
        return self.get(name).content

    def write_override(self, name: str, content: str) -> Path:
        if not _valid_layer_name(name):
            raise ValueError("invalid prompt layer name")
        self.overrides_dir.mkdir(parents=True, exist_ok=True)
        path = self.override_path(name)
        path.write_text(content, encoding="utf-8")
        return path

    def delete_override(self, name: str) -> None:
        path = self.override_path(name)
        if path.exists():
            path.unlink()

    def override_path(self, name: str) -> Path:
        if not _valid_layer_name(name):
            raise ValueError("invalid prompt layer name")
        return self.overrides_dir / f"{name}.md"

    def _default_permission(self, name: str) -> str:
        return str(self._default_spec(name).get("minimum_permission") or "read")

    @staticmethod
    def _default_spec(name: str) -> dict[str, Any]:
        data = load_spec("prompt_layers.yaml") or {}
        spec = data.get(name)
        if not isinstance(spec, dict):
            return {"content": "", "minimum_permission": "read"}
        return spec


def build_system_prompt(
    *,
    home: Path,
    memory_context: str = "",
    skills_context: str = "",
    workspace: Path | None = None,
    operating_context: OperatingContext | None = None,
) -> str:
    config = load_config(home)
    prompt_store = PromptLayerStore(home)
    operating_context = operating_context or OperatingContext(home=home)
    workspace_path = Path(operating_context.workspace) if operating_context.workspace else (workspace or Path.cwd())
    workspace = workspace_path.resolve()
    unit_path = systemd_user_unit_path(config.runtime.service_name)
    unit_state = "installed" if unit_path.exists() else "not installed"
    web_console_fact = (
        f"- Web console URL: {config.runtime.web_url}"
        if config.runtime.web_url
        else "- Web console URL: not configured in runtime context; do not assume a host or port."
    )

    runtime_lines = [
        "Local runtime facts:",
        f"- Current workspace: {workspace}",
        f"- Navi state home: {home.resolve()}",
        f"- Model provider: {config.model.provider}",
        f"- Model name: {config.model.model}",
        f"- User systemd service {config.runtime.service_name}: {unit_state} at {unit_path}",
        web_console_fact,
        f"- Source: {operating_context.source}",
        f"- Permission ceiling: {operating_context.permission_ceiling}",
        f"- Skill permission ceiling: {operating_context.skill_permission_ceiling}",
    ]
    if operating_context.role:
        runtime_lines.append(f"- Active role: {operating_context.role}")
    runtime_static = prompt_store.read("runtime").strip()
    if runtime_static:
        runtime_lines.extend(runtime_static.splitlines())

    layers = [
        prompt_store.get("identity"),
        PromptLayer(
            "runtime",
            "\n".join(runtime_lines),
        ),
        prompt_store.get("authorization"),
        PromptLayer("memory", f"Memory recall:\n{memory_context}" if memory_context else ""),
        PromptLayer("skills", f"Installed skills:\n{skills_context}" if skills_context else ""),
        prompt_store.get("style"),
    ]
    return render_prompt_layers(layers, operating_context)


def _valid_layer_name(name: str) -> bool:
    return bool(name) and all(part.isalnum() or part in {"_", "-"} for part in name)
