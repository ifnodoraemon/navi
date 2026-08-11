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
from navi.core_tools.skills import SKILL_FILE_MAX_BYTES
from navi.skills import SkillStore


def _write_skill(home: Path, name: str, description: str, body: str) -> None:
    skill_dir = home / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n"
        "metadata:\n  navi.permission: read\n"
        f"---\n{body}\n",
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


def test_invalid_agent_skill_contract_is_excluded_with_a_typed_issue(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "Legacy_Name"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: Legacy Name\ndescription: legacy\npermission: read\n---\nBody\n",
        encoding="utf-8",
    )
    store = SkillStore(tmp_path)

    skills = store.list_skills()

    assert "Legacy Name" not in {skill.name for skill in skills}
    assert len(store.last_validation_issues) == 1
    assert "metadata" in store.last_validation_issues[0].reason


def test_all_builtin_skills_follow_agent_skills_name_and_directory_contract(tmp_path: Path) -> None:
    store = SkillStore(tmp_path)

    skills = store.list_skills()

    assert len(skills) == 9
    assert store.last_validation_issues == ()
    assert all(skill.name == skill.path.parent.name for skill in skills)
    assert all(skill.source == "builtin" and skill.scope == "builtin" for skill in skills)


@pytest.mark.parametrize(
    "directory_name, frontmatter, expected_reason",
    [
        (
            "invalid-contract",
            "---\nname: 123\ndescription: numeric name\n---\nInstructions.\n",
            "name must be a string",
        ),
        (
            "boolean-description",
            "---\nname: boolean-description\ndescription: true\n---\nInstructions.\n",
            "description must be a string",
        ),
        (
            "invalid-contract",
            "---name: inline-delimiter\ndescription: invalid\n---\nInstructions.\n",
            "requires YAML frontmatter",
        ),
    ],
)
def test_agent_skill_contract_rejects_non_string_fields_and_inline_delimiters(
    tmp_path: Path,
    directory_name: str,
    frontmatter: str,
    expected_reason: str,
) -> None:
    skill_dir = tmp_path / "skills" / directory_name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(frontmatter, encoding="utf-8")
    store = SkillStore(tmp_path)

    skills = store.list_skills()

    assert directory_name not in {skill.name for skill in skills}
    assert len(store.last_validation_issues) == 1
    assert expected_reason in store.last_validation_issues[0].reason


def test_workspace_skill_cannot_claim_runtime_owned_source_scope_or_trust(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    skill_dir = workspace / ".navi" / "skills" / "claim-trust"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: claim-trust\ndescription: invalid authority claim\n"
        "metadata:\n  navi.source: local\n  navi.permission: read\n---\nInstructions.\n",
        encoding="utf-8",
    )
    store = SkillStore(tmp_path)

    skills = store.list_skills(workspace=workspace)

    assert "claim-trust" not in {skill.name for skill in skills}
    assert len(store.last_validation_issues) == 1
    assert "runtime-owned authority" in store.last_validation_issues[0].reason


def test_agent_skill_metadata_rejects_non_string_values(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "invalid-metadata"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: invalid-metadata\ndescription: invalid metadata value\n"
        "metadata:\n  navi.tags: [one, two]\n---\nInstructions.\n",
        encoding="utf-8",
    )
    store = SkillStore(tmp_path)

    skills = store.list_skills()

    assert "invalid-metadata" not in {skill.name for skill in skills}
    assert len(store.last_validation_issues) == 1
    assert "keys and values must be strings" in store.last_validation_issues[0].reason


@pytest.mark.asyncio
async def test_skill_tools_cannot_bypass_context_skill_permission_ceiling(
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / "skills" / "write-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: write-skill\ndescription: write-only workflow\n"
        "metadata:\n  navi.permission: write\n---\n"
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
        "skills.view", {"name": "write-skill"}, permission="read", context=read_context
    )
    visible = await registry.invoke(
        "skills.view", {"name": "write-skill"}, permission="read", context=write_context
    )

    assert "write-skill" not in {item["name"] for item in hidden.facts["skills"]}
    assert denied.ok is False
    assert denied.error_reason == "not_found"
    assert visible.ok is True
    assert "WRITE_SKILL_BODY" in visible.facts["content"]


@pytest.mark.asyncio
async def test_skill_view_returns_one_complete_instruction_file(tmp_path: Path) -> None:
    body = "STEP\n" * 12_000
    _write_skill(tmp_path, "complete", "Complete instructions", body)
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = CapabilityContext(home=tmp_path, workspace=str(tmp_path))

    viewed = await registry.invoke(
        "skills.view",
        {"name": "complete"},
        permission="read",
        context=context,
    )

    assert viewed.ok is True
    assert viewed.facts["complete"] is True
    assert viewed.facts["size"] < SKILL_FILE_MAX_BYTES
    assert viewed.facts["content"].count("STEP") == 12_000
    assert "truncated" not in viewed.facts


@pytest.mark.asyncio
async def test_skill_view_fails_instead_of_returning_partial_instructions(
    tmp_path: Path,
) -> None:
    _write_skill(
        tmp_path,
        "oversized",
        "Oversized instructions",
        "X" * SKILL_FILE_MAX_BYTES,
    )
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = CapabilityContext(home=tmp_path, workspace=str(tmp_path))

    viewed = await registry.invoke(
        "skills.view",
        {"name": "oversized"},
        permission="read",
        context=context,
    )

    assert viewed.ok is False
    assert viewed.error_reason == "resource_limit"
    assert viewed.facts["complete"] is False
    assert "content" not in viewed.facts
