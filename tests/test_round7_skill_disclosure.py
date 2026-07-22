"""Round-7: skill progressive disclosure (Claude Code SKILL.md inspiration).

``render_prompt`` injects a name+description catalog, not full skill bodies, so
the base prompt stays bounded as the skill library grows. The agent fetches a
skill's full instructions on demand via the ``skills.view`` tool. This also
keeps skill bodies (especially unverified ones) out of the system prompt — they
are read, if at all, as untrusted tool data.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.skills import SkillStore


def _write_skill(home: Path, name: str, description: str, body: str) -> None:
    skill_dir = home / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\npermission: read\n---\n{body}\n",
        encoding="utf-8",
    )


def test_render_prompt_is_catalog_not_full_body(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "demo",
        "Demo skill summary line",
        "BODY_MARKER full step-by-step instructions that must not bloat the prompt.",
    )
    rendered = SkillStore(tmp_path).render_prompt(permission_ceiling="read")

    assert "demo" in rendered  # skill is discoverable
    assert "Demo skill summary line" in rendered  # description is in the catalog
    assert "BODY_MARKER" not in rendered  # full body is NOT injected
    assert "skills.view" in rendered  # agent is told how to fetch the body


@pytest.mark.asyncio
async def test_skill_tools_cannot_bypass_context_skill_permission_ceiling(
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / "skills" / "write_skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: write_skill\ndescription: write-only workflow\npermission: write\n---\n"
        "WRITE_SKILL_BODY\n",
        encoding="utf-8",
    )
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    read_context = CapabilityContext(
        home=tmp_path,
        workspace=str(tmp_path),
        permission_ceiling="write",
        skill_permission_ceiling="read",
    )
    write_context = CapabilityContext(
        home=tmp_path,
        workspace=str(tmp_path),
        permission_ceiling="write",
        skill_permission_ceiling="write",
    )

    hidden = await registry.invoke(
        "skills.list", {}, permission="read", context=read_context
    )
    denied = await registry.invoke(
        "skills.view", {"name": "write_skill"}, permission="read", context=read_context
    )
    visible = await registry.invoke(
        "skills.view", {"name": "write_skill"}, permission="read", context=write_context
    )

    assert "write_skill" not in {item["name"] for item in hidden.facts["skills"]}
    assert denied.ok is False
    assert denied.error_reason == "not_found"
    assert visible.ok is True
    assert "WRITE_SKILL_BODY" in visible.facts["content"]
