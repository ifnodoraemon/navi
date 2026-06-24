"""Round-7: skill progressive disclosure (Claude Code SKILL.md inspiration).

``render_prompt`` injects a name+description catalog, not full skill bodies, so
the base prompt stays bounded as the skill library grows. The agent fetches a
skill's full instructions on demand via the ``skills.view`` tool. This also
keeps skill bodies (especially unverified ones) out of the system prompt — they
are read, if at all, as untrusted tool data.
"""

from __future__ import annotations

from pathlib import Path

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
