from __future__ import annotations

from dataclasses import dataclass

from .spec_loader import load_spec


@dataclass(frozen=True)
class CommandActionSpec:
    name: str
    usage: str


@dataclass(frozen=True)
class CommandObjectSpec:
    name: str
    command: str
    affordance: str
    actions: dict[str, CommandActionSpec]


@dataclass(frozen=True)
class CommandCatalog:
    objects: dict[str, CommandObjectSpec]
    help_lines: tuple[str, ...]
    help_aliases: tuple[str, ...]

    def object_by_command(self, command: str) -> CommandObjectSpec | None:
        for spec in self.objects.values():
            if spec.command == command:
                return spec
        return None

    def usage(self, object_name: str, action_name: str | None = None) -> str:
        spec = self.objects[object_name]
        if action_name:
            return spec.actions[action_name].usage
        return " | ".join(action.usage for action in spec.actions.values())

    def affordances(self) -> tuple[str, ...]:
        return tuple(spec.affordance for spec in self.objects.values())

    def help_text(self) -> str:
        return "\n".join(("Navi commands:", *self.help_lines))


def load_command_catalog() -> CommandCatalog:
    raw = load_spec("commands.yaml")
    objects: dict[str, CommandObjectSpec] = {}
    for name, item in (raw.get("objects") or {}).items():
        actions = {
            action_name: CommandActionSpec(name=action_name, usage=str(action["usage"]))
            for action_name, action in (item.get("actions") or {}).items()
        }
        objects[name] = CommandObjectSpec(
            name=name,
            command=str(item["command"]),
            affordance=str(item["affordance"]),
            actions=actions,
        )
    return CommandCatalog(
        objects=objects,
        help_lines=tuple(raw.get("help") or ()),
        help_aliases=tuple((raw.get("aliases") or {}).get("help") or ()),
    )
