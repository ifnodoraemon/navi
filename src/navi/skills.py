from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path


class SkillStore:
    def __init__(self, home: Path):
        self.skills_dir = home / "skills"
        self.skills_dir.mkdir(parents=True, exist_ok=True)

    def list_skills(self) -> list[Skill]:
        skills: list[Skill] = []
        for path in sorted(self.skills_dir.glob("*/SKILL.md")):
            metadata = self._frontmatter(path)
            skills.append(
                Skill(
                    name=str(metadata.get("name") or path.parent.name),
                    description=str(metadata.get("description") or ""),
                    path=path,
                )
            )
        return skills

    def render_prompt(self) -> str:
        chunks = []
        for skill in self.list_skills():
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
