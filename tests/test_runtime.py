from __future__ import annotations

import pytest

from navi.provider import ChatMessage, MockProvider, ModelPool
from navi.runtime import AgentRuntime
from navi.operating_context import OperatingContext


class RecordingProvider(MockProvider):
    def __init__(self):
        self.messages: list[ChatMessage] = []

    async def complete(self, messages: list[ChatMessage]) -> str:
        self.messages = messages
        return await super().complete(messages)


def _pool(provider=None) -> ModelPool:
    return ModelPool(default=provider or MockProvider())


@pytest.mark.asyncio
async def test_runtime_persists_session_messages(tmp_path):
    runtime = AgentRuntime(home=tmp_path, provider=_pool())

    reply = await runtime.chat("hello")

    assert reply.session_id
    assert reply.content == "Navi received: hello"
    messages = runtime.memory.get_messages(reply.session_id)
    assert [message.role for message in messages] == ["user", "assistant"]


def test_memory_append_and_read(tmp_path):
    runtime = AgentRuntime(home=tmp_path, provider=_pool())

    runtime.memory.append_memory("Prefers concise replies")

    assert "Prefers concise replies" in runtime.memory.read_memory()


def test_memory_recall_prioritizes_constraints_and_relevance(tmp_path):
    runtime = AgentRuntime(home=tmp_path, provider=_pool())
    runtime.memory.add_item(
        "constraint",
        "Never run destructive git commands without explicit approval.",
        source="test",
        status="active",
        confidence=1.0,
    )
    runtime.memory.add_item(
        "fact",
        "The Navi project lives under /home/ifnodoraemon/myagent/navi.",
        source="test",
        status="active",
        confidence=0.9,
        scope="project:navi",
    )
    runtime.memory.add_item(
        "fact",
        "The unrelated cooking notebook uses grams.",
        source="test",
        status="active",
        confidence=0.9,
        scope="project:cooking",
    )

    rendered = runtime.memory.render_context("inspect navi project git status")

    assert "Never run destructive git commands" in rendered
    assert "Navi project lives" in rendered
    assert "cooking notebook" not in rendered


def test_session_alias_rotation_preserves_old_messages(tmp_path):
    runtime = AgentRuntime(home=tmp_path, provider=_pool())
    first = runtime.memory.current_session_id("connector:test:peer")
    runtime.memory.add_message(first, "user", "old topic")

    second = runtime.memory.rotate_session("connector:test:peer").session_id
    runtime.memory.add_message(second, "user", "new topic")

    assert first != second
    assert runtime.memory.current_session_id("connector:test:peer") == second
    assert runtime.memory.get_messages(first)[0].content == "old topic"
    assert runtime.memory.get_messages(second)[0].content == "new topic"


@pytest.mark.asyncio
async def test_runtime_system_prompt_includes_local_deployment_contract(tmp_path):
    provider = RecordingProvider()
    runtime = AgentRuntime(home=tmp_path, provider=_pool(provider))

    await runtime.chat("列一下我本机的目录")

    system = provider.messages[0].content
    assert "running on their own machine" in system
    assert "Current workspace:" in system
    assert "Navi state home:" in system
    assert "Local execution bridge" in system
    assert "Do not say you have no access to the user's local machine as an absolute statement" in system
    assert "Do not frame local actions as a generic permission failure" in system
    assert "this chat response itself is not a shell" in system
    assert "Do not give a CLI invocation for task creation" in system
    assert "user input for the kernel syscall planner" in system
    assert "Do not invent product surfaces" in system
    assert "127.0.0.1:8765" not in system
    assert "Current conversational channel" not in system
    assert "Remote connectors:" not in system
    assert "active surface" not in system
    assert "Weixin" not in system


@pytest.mark.asyncio
async def test_runtime_system_prompt_does_not_advertise_web_surface(tmp_path):
    provider = RecordingProvider()
    runtime = AgentRuntime(home=tmp_path, provider=_pool(provider))

    await runtime.chat("hello")

    system = provider.messages[0].content
    assert "console URL" not in system


@pytest.mark.asyncio
async def test_runtime_system_prompt_uses_goal_directed_memory(tmp_path):
    provider = RecordingProvider()
    runtime = AgentRuntime(home=tmp_path, provider=_pool(provider))
    runtime.memory.add_item(
        "constraint",
        "Do not forget approval state during long context.",
        source="test",
        status="active",
        confidence=1.0,
    )
    runtime.memory.add_item(
        "fact",
        "The Navi deployment uses systemd user service.",
        source="test",
        status="active",
        confidence=0.9,
    )
    runtime.memory.add_item(
        "fact",
        "The unrelated archive is stored on cold media.",
        source="test",
        status="active",
        confidence=0.9,
    )

    await runtime.chat("检查 navi deployment service 状态")

    system = provider.messages[0].content
    assert "Memory recall:" in system
    assert "approval state" in system
    assert "systemd user service" in system
    assert "cold media" not in system


@pytest.mark.asyncio
async def test_runtime_prompt_layers_and_skill_permissions_are_scoped(tmp_path):
    read_skill = tmp_path / "skills" / "read-skill"
    read_skill.mkdir(parents=True)
    read_skill.joinpath("SKILL.md").write_text(
        "---\nname: read-skill\npermission: read\n---\nRead layer skill body.",
        encoding="utf-8",
    )
    write_skill = tmp_path / "skills" / "write-skill"
    write_skill.mkdir(parents=True)
    write_skill.joinpath("SKILL.md").write_text(
        "---\nname: write-skill\npermission: write\n---\nWrite layer skill body.",
        encoding="utf-8",
    )
    provider = RecordingProvider()
    runtime = AgentRuntime(home=tmp_path, provider=_pool(provider))

    await runtime.chat(
        "hello",
        operating_context=OperatingContext(
            home=tmp_path,
            permission_ceiling="read",
            skill_permission_ceiling="read",
            prompt_layers=("identity", "runtime", "skills"),
        ),
    )

    system = provider.messages[0].content
    assert "[identity]" in system
    assert "[runtime]" in system
    assert "[skills]" in system
    assert "[authorization]" not in system
    assert "Permission ceiling: read" in system
    assert "Read layer skill body." in system
    assert "Write layer skill body." not in system


def test_skill_store_built_in_and_override(tmp_path):
    from navi.skills import SkillStore

    # 1. Prepare mock built-in skills directory
    builtin_dir = tmp_path / "builtin"
    builtin_dir.mkdir()

    # Create built-in skill "test-skill"
    s1 = builtin_dir / "test-skill"
    s1.mkdir()
    s1.joinpath("SKILL.md").write_text("---\nname: test-skill\n---\nBuilt-in version.", encoding="utf-8")

    # Create built-in skill "only-builtin"
    s2 = builtin_dir / "only-builtin"
    s2.mkdir()
    s2.joinpath("SKILL.md").write_text("---\nname: only-builtin\n---\nOnly builtin version.", encoding="utf-8")

    # 2. Prepare user skills directory
    user_home = tmp_path / "user"
    user_skills_dir = user_home / "skills"
    user_skills_dir.mkdir(parents=True)

    # Override "test-skill" in user directory
    u1 = user_skills_dir / "test-skill"
    u1.mkdir()
    u1.joinpath("SKILL.md").write_text("---\nname: test-skill\n---\nUser override version.", encoding="utf-8")

    # 3. Instantiate SkillStore and mock its builtin_skills_dir
    store = SkillStore(user_home)
    store.builtin_skills_dir = builtin_dir

    # List and assert
    skills = store.list_skills()
    skill_map = {s.name: s for s in skills}

    assert "test-skill" in skill_map
    assert "only-builtin" in skill_map

    # Check that test-skill was overridden by the user version
    assert skill_map["test-skill"].path == u1 / "SKILL.md"
    assert skill_map["test-skill"].path.read_text(encoding="utf-8").strip().endswith("User override version.")

    # Check only-builtin is correctly loaded
    assert skill_map["only-builtin"].path == s2 / "SKILL.md"


def test_builtin_general_skills_are_available(tmp_path):
    from navi.skills import SkillStore
    from navi.tools import build_tool_gateway

    expected = {
        "Browser Operator",
        "GitHub Workflow",
        "Structured Output",
        "Systematic Debugging",
        "Test Driven Development",
        "Web Research Crawler",
    }

    store = SkillStore(tmp_path)
    skill_names = {skill.name for skill in store.list_skills(permission_ceiling="read")}

    assert expected <= skill_names

    gateway = build_tool_gateway(tmp_path, project_dir=tmp_path)
    result = gateway.call("skills.list")

    assert result.ok
    listed = {item["name"] for item in result.facts["skills"]}
    assert expected <= listed
    assert "Memory Curator" in listed
    curator = next(item for item in result.facts["skills"] if item["name"] == "Memory Curator")
    assert curator["permission"] == "write"
    assert curator["injectable_with_read_ceiling"] is False


def test_core_support_tools_expose_skills_memory_web_and_browser(tmp_path, monkeypatch):
    import httpx

    from navi.memory import MemoryStore
    from navi.tools import build_tool_gateway

    gateway = build_tool_gateway(tmp_path, project_dir=tmp_path)

    viewed = gateway.call("skills.view", {"name": "Web Research Crawler"})
    assert viewed.ok
    assert "Web Research Crawler Skill" in viewed.facts["content"]

    memory = MemoryStore(tmp_path)
    memory.add_item(
        "preference",
        "Prefer regression evals for user-visible Navi failures.",
        source="test",
        status="active",
        confidence=0.9,
    )
    listed = gateway.call("memory.list", {"type": "preference", "status": "active"})
    assert listed.ok
    assert listed.facts["items"][0]["type"] == "preference"
    recalled = gateway.call("memory.recall", {"query": "regression evals"})
    assert recalled.ok
    assert recalled.facts["count"] == 1

    html = "<html><head><title>Doc</title></head><body><h1>Main</h1><a href='/a'>A</a>Contact: hi@example.com</body></html>"
    extracted = gateway.call(
        "web.extract",
        {"content": html, "base_url": "https://example.com/root", "patterns": {"emails": r"[\w.-]+@[\w.-]+"}},
    )
    assert extracted.ok
    assert extracted.facts["title"] == "Doc"
    assert extracted.facts["links"][0]["href"] == "https://example.com/a"
    assert extracted.facts["patterns"]["emails"] == ["hi@example.com"]

    def fake_get(url, **kwargs):
        assert url == "https://example.com/page"
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<title>Fetched</title><p>Hello</p>",
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr("navi.core_tools.httpx.get", fake_get)
    monkeypatch.setattr("navi.core_tools._host_resolves_to_public_ips", lambda host: True)
    fetched = gateway.call("web.fetch", {"url": "https://example.com/page"})
    assert fetched.ok
    assert fetched.facts["title"] == "Fetched"
    assert "Hello" in fetched.facts["text"]

    monkeypatch.setattr("navi.core_tools.shutil.which", lambda name: None)
    screenshot = gateway.call("browser.screenshot", {"url": "https://example.com", "path": "shot.png"})
    assert not screenshot.ok
    assert screenshot.error == "playwright CLI not found"
