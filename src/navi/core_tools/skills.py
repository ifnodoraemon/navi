"""Core tool handlers."""
from __future__ import annotations
from pathlib import Path
from typing import Any
from ..operating_context import permission_allows
from ..skills import SkillStore
from ..tools import ToolResult
from .utils import _positive_int

def _skills_list(home: Path, *, workspace: Path) -> ToolResult:
    skills = SkillStore(home).list_skills(permission_ceiling="write", workspace=workspace)
    return ToolResult(
        tool="skills.list",
        ok=True,
        facts={
            "category": "skills",
            "definition": "procedural guidance packages loaded into Navi's prompt context",
            "not_tools": True,
            "prompt_permission_ceiling": "read",
            "skills": [
                {
                    "name": skill.name,
                    "description": skill.description,
                    "source": skill.source,
                    "scope": skill.scope,
                    "permission": skill.permission,
                    "injectable_with_read_ceiling": permission_allows(skill.permission, "read"),
                    "verified": skill.verified,
                    "tags": list(skill.tags),
                }
                for skill in skills
            ],
            "count": len(skills),
        },
    )


def _skills_view(home: Path, args: dict[str, Any], *, workspace: Path) -> ToolResult:
    name = str(args.get("name") or "").strip().lower()
    if not name:
        return ToolResult(tool="skills.view", ok=False, error="name is required")
    relative = str(args.get("relative_path") or "SKILL.md").strip() or "SKILL.md"
    limit = _positive_int(args.get("max_bytes"), default=50000, maximum=200000)
    store = SkillStore(home)
    skills = store.list_skills(permission_ceiling="write", workspace=workspace)
    skill = next(
        (
            item
            for item in skills
            if item.name.lower() == name or item.path.parent.name.lower() == name
        ),
        None,
    )
    if skill is None:
        return ToolResult(
            tool="skills.view", ok=False, error="skill not found", facts={"name": name}
        )
    base_dir = skill.path.parent.resolve()
    target = (base_dir / relative).resolve()
    if base_dir != target and base_dir not in target.parents:
        return ToolResult(
            tool="skills.view", ok=False, error="relative_path must stay inside the skill directory"
        )
    if not target.exists() or not target.is_file():
        return ToolResult(
            tool="skills.view", ok=False, error="skill file not found", facts={"path": str(target)}
        )
    data = target.read_bytes()
    truncated = len(data) > limit
    content = data[:limit].decode("utf-8", errors="replace")
    return ToolResult(
        tool="skills.view",
        ok=True,
        facts={
            "name": skill.name,
            "description": skill.description,
            "permission": skill.permission,
            "injectable_with_read_ceiling": permission_allows(skill.permission, "read"),
            "path": str(target),
            "relative_path": str(target.relative_to(base_dir)),
            "size": len(data),
            "truncated": truncated,
            "content": content,
        },
    )


