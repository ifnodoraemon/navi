from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .operating_context import permission_allows


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path
    permission: str = "read"
    source: str = "local"
    tags: tuple[str, ...] = ()


class SkillStore:
    def __init__(self, home: Path):
        self.skills_dir = home / "skills"
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.builtin_skills_dir = Path(__file__).resolve().parent / "skills"
        self.builtin_skills_dir.mkdir(parents=True, exist_ok=True)

    def list_skills(
        self,
        *,
        permission_ceiling: str = "write",
        sources: set[str] | None = None,
    ) -> list[Skill]:
        skills: list[Skill] = []
        seen_names: set[str] = set()

        # 1. User-defined skills (override/highest priority)
        for path in sorted(self.skills_dir.glob("*/SKILL.md")):
            metadata = self._frontmatter(path)
            name = str(metadata.get("name") or path.parent.name)
            skill = Skill(
                name=name,
                description=str(metadata.get("description") or ""),
                path=path,
                permission=str(metadata.get("permission") or "read").strip().lower(),
                source=str(metadata.get("source") or "local").strip().lower(),
                tags=_metadata_tuple(metadata.get("tags")),
            )
            if sources is not None and skill.source not in sources:
                continue
            if not permission_allows(skill.permission, permission_ceiling):
                continue
            skills.append(skill)
            seen_names.add(name)

        # 2. Built-in skills (loaded if not overridden)
        for path in sorted(self.builtin_skills_dir.glob("*/SKILL.md")):
            metadata = self._frontmatter(path)
            name = str(metadata.get("name") or path.parent.name)
            if name in seen_names:
                continue
            skill = Skill(
                name=name,
                description=str(metadata.get("description") or ""),
                path=path,
                permission=str(metadata.get("permission") or "read").strip().lower(),
                source=str(metadata.get("source") or "local").strip().lower(),
                tags=_metadata_tuple(metadata.get("tags")),
            )
            if sources is not None and skill.source not in sources:
                continue
            if not permission_allows(skill.permission, permission_ceiling):
                continue
            skills.append(skill)
            seen_names.add(name)

        return skills

    def render_prompt(
        self,
        *,
        permission_ceiling: str = "read",
        sources: set[str] | None = None,
    ) -> str:
        chunks = []
        for skill in self.list_skills(permission_ceiling=permission_ceiling, sources=sources):
            content = skill.path.read_text(encoding="utf-8").strip()
            if content:
                chunks.append(content)
        return "\n\n".join(chunks)

    @staticmethod
    def _frontmatter(path: Path) -> dict[str, Any]:
        content = path.read_text(encoding="utf-8")
        if not content.startswith("---"):
            return {}
        parts = content.split("---", 2)
        if len(parts) < 3:
            return {}
        data = yaml.safe_load(parts[1]) or {}
        return data if isinstance(data, dict) else {}


def _metadata_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(item).strip().lower() for item in value if str(item).strip())
    if isinstance(value, str):
        return tuple(item.strip().lower() for item in value.split(",") if item.strip())
    return ()
