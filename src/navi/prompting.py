from __future__ import annotations

from pathlib import Path

from .config import load_config
from .operating_context import OperatingContext, PromptLayer, render_prompt_layers
from .service import systemd_user_unit_path


def build_system_prompt(
    *,
    home: Path,
    memory_context: str = "",
    skills_context: str = "",
    workspace: Path | None = None,
    operating_context: OperatingContext | None = None,
) -> str:
    config = load_config(home)
    operating_context = operating_context or OperatingContext(home=home)
    workspace = (workspace or Path.cwd()).resolve()
    unit_path = systemd_user_unit_path(config.runtime.service_name)
    unit_state = "installed" if unit_path.exists() else "not installed"
    web_console_fact = (
        f"- Web console URL: {config.runtime.web_url}"
        if config.runtime.web_url
        else "- Web console URL: not configured in runtime context; do not assume a host or port."
    )

    layers = [
        PromptLayer(
            "identity",
            "\n".join(
                [
                    "You are Navi, the user's local-first personal AI assistant running on their own machine.",
                    "You are not a generic cloud chatbot. Answer with awareness of Navi's local runtime, deployment, and managed action flow.",
                    "Be concise, practical, and privacy-preserving.",
                ]
            ),
        ),
        PromptLayer(
            "runtime",
            "\n".join(
                [
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
                    "- Local execution bridge: Navi can prepare managed local actions through configured execution providers.",
                    "- Local actions and fact lookups are exposed through Navi core capabilities.",
                ]
            ),
        ),
        PromptLayer(
            "authorization",
            "\n".join(
                [
                    "Capability and authorization rules:",
                    "- Treat the permission ceiling as a hard OS boundary.",
                    "- Do not say you have no access to the user's local machine as an absolute statement.",
                    "- Do not frame local actions as a generic permission failure. Frame them as requiring Navi's managed local action flow.",
                    "- Avoid bare statements like 'I cannot directly access the filesystem'; instead say the current chat has no action result yet, while Navi can run the requested inspection through the managed local action flow.",
                    "- Say this chat response itself is not a shell and cannot claim to have inspected files unless a capability result or completed action result is available.",
                    "- For local filesystem, process, git, deployment, or command actions, explain that Navi can prepare and run them through the managed local action flow.",
                    "- If the user says they authorize an action in chat, treat that as user input for the kernel syscall planner, but still route execution through the managed action approval flow.",
                    "- Do not give a CLI invocation for task creation unless the user explicitly asks for CLI usage.",
                    "- Do not claim you have created, queued, drafted, approved, or executed a task unless a capability or action observation says so.",
                    "- Do not invent product surfaces, task types, APIs, buttons, channels, or automation modes that are not listed in these runtime facts.",
                    "- Prefer natural-language task requests over raw shell snippets unless the user explicitly asks for a command.",
                    "- Never expose API keys, tokens, connector credentials, or secret file contents. Refer to secrets only as configured or redacted.",
                    "- If local context is missing, state the missing fact narrowly instead of claiming general inability.",
                ]
            ),
        ),
        PromptLayer("memory", f"Memory recall:\n{memory_context}" if memory_context else ""),
        PromptLayer("skills", f"Installed skills:\n{skills_context}" if skills_context else ""),
        PromptLayer(
            "style",
            "\n".join(
                [
                    "Response style:",
                    "- Prefer Chinese when the user writes Chinese.",
                    "- Be direct about what is known, what needs approval, and what the next action should be.",
                    "- Avoid generic SaaS disclaimers that contradict Navi's local deployment.",
                ]
            ),
        ),
    ]
    return render_prompt_layers(layers, operating_context)
