"""Core tool handlers."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..operating_context import permission_allows
from ..skills import SkillStore
from ..tools import ToolResult

SKILL_FILE_MAX_BYTES = 200_000

def _skills_list(home: Path, args: dict[str, Any], *, workspace: Path) -> ToolResult:
    permission_ceiling = str(args.get("_skill_permission_ceiling") or "read")
    skills = SkillStore(home).list_skills(
        permission_ceiling=permission_ceiling,
        workspace=workspace,
    )
    return ToolResult(
        tool="skills.list",
        ok=True,
        facts={
            "category": "skills",
            "definition": "procedural guidance packages loaded into Navi's prompt context",
            "not_tools": True,
            "prompt_permission_ceiling": permission_ceiling,
            "skills": [
                {
                    "name": skill.name,
                    "description": skill.description,
                    "source": skill.source,
                    "scope": skill.scope,
                    "permission": skill.permission,
                    "injectable_with_current_ceiling": permission_allows(
                        skill.permission, permission_ceiling
                    ),
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
        return ToolResult(
            tool="skills.view",
            ok=False,
            error="name is required",
            error_reason="missing_required_argument",
        )
    relative = str(args.get("relative_path") or "SKILL.md").strip() or "SKILL.md"
    store = SkillStore(home)
    permission_ceiling = str(args.get("_skill_permission_ceiling") or "read")
    skills = store.list_skills(permission_ceiling=permission_ceiling, workspace=workspace)
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
            tool="skills.view",
            ok=False,
            error="skill not found",
            facts={"name": name},
            error_reason="not_found",
        )
    base_dir = skill.path.parent.resolve()
    target = (base_dir / relative).resolve()
    if base_dir != target and base_dir not in target.parents:
        return ToolResult(
            tool="skills.view",
            ok=False,
            error="relative_path must stay inside the skill directory",
            error_reason="resource_scope_violation",
        )
    if not target.exists() or not target.is_file():
        return ToolResult(
            tool="skills.view",
            ok=False,
            error="skill file not found",
            facts={"path": str(target)},
            error_reason="not_found",
        )
    data = target.read_bytes()
    if len(data) > SKILL_FILE_MAX_BYTES:
        return ToolResult(
            tool="skills.view",
            ok=False,
            error="skill file exceeds the complete-read safety limit",
            facts={
                "name": skill.name,
                "path": str(target),
                "relative_path": str(target.relative_to(base_dir)),
                "size": len(data),
                "max_size": SKILL_FILE_MAX_BYTES,
                "complete": False,
            },
            error_reason="resource_limit",
        )
    content = data.decode("utf-8", errors="replace")
    return ToolResult(
        tool="skills.view",
        ok=True,
        facts={
            "name": skill.name,
            "description": skill.description,
            "permission": skill.permission,
            "injectable_with_current_ceiling": permission_allows(
                skill.permission, permission_ceiling
            ),
            "path": str(target),
            "relative_path": str(target.relative_to(base_dir)),
            "size": len(data),
            "complete": True,
            "content": content,
        },
    )
