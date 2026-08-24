from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .operating_context import OperatingContext, PromptLayer, permission_allows
from .model_facts import project_model_facts
from .provider import ChatMessage
from .specs_data import PROMPT_ASSEMBLIES_SPEC, SYSCALL_PLANNER_SPEC
from .tools import ToolSpec

PLANNER_RUNTIME_FACT_MAX_DEPTH = 8


@dataclass(frozen=True)
class PromptBlock:
    name: str
    tier: str
    source: str
    content: str
    trusted: bool = True
    mutable: bool = False

    def digest(self) -> str:
        payload = json.dumps(
            {
                "name": self.name,
                "tier": self.tier,
                "source": self.source,
                "content": self.content,
                "trusted": self.trusted,
                "mutable": self.mutable,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def to_manifest(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tier": self.tier,
            "source": self.source,
            "trusted": self.trusted,
            "mutable": self.mutable,
            "digest": self.digest(),
            "chars": len(self.content),
        }


@dataclass(frozen=True)
class PromptAssembly:
    name: str
    blocks: tuple[PromptBlock, ...]

    def render(self) -> str:
        return render_prompt_blocks(self.blocks)

    def manifest(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "blocks": [block.to_manifest() for block in self.blocks],
            "digest": self.digest(),
        }

    def digest(self) -> str:
        payload = json.dumps(
            [block.to_manifest() for block in self.blocks], ensure_ascii=False, sort_keys=True
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def render_prompt_blocks(blocks: Iterable[PromptBlock]) -> str:
    rendered = []
    for block in blocks:
        content = block.content.strip()
        if not content:
            continue
        tag = _block_xml_tag(block.name)
        if _is_json_document(content):
            rendered.append(f"<{tag}>\n<![CDATA[{content}]]>\n</{tag}>")
        elif block.trusted:
            rendered.append(f"<{tag}>\n{_escape_xml_text(content)}\n</{tag}>")
        else:
            rendered.append(
                f'<untrusted_input name="{tag}">\n{_escape_xml_text(content)}\n'
                "</untrusted_input>"
            )
    return "\n\n".join(rendered)


def _block_xml_tag(block_name: str) -> str:
    """Derive a fixed, small XML element name from a block name."""
    tag = re.sub(r"[^a-z0-9]+", "_", str(block_name or "").strip().lower()).strip("_")
    return tag or "block"


def _escape_xml_text(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _is_json_document(text: str) -> bool:
    stripped = text.strip()
    return stripped[:1] in {"{", "["} and stripped[-1:] in {"}", "]"}


def assemble_planner_system_prompt() -> PromptAssembly:
    spec = SYSCALL_PLANNER_SPEC or {}
    blocks = [
        PromptBlock(
            "PLANNER SYSTEM",
            "stable",
            "syscall_planner.system_lines",
            "\n".join(str(line) for line in spec.get("system_lines") or []),
        ),
        _list_block(
            "PROMPT BOUNDARIES",
            "stable",
            "syscall_planner.prompt_boundaries",
            spec.get("prompt_boundaries"),
        ),
        _list_block(
            "SECURITY GUIDELINE",
            "stable",
            "syscall_planner.security_guidelines",
            spec.get("security_guidelines"),
        ),
    ]
    return PromptAssembly(
        "planner_system", tuple(block for block in blocks if block.content.strip())
    )


def assemble_planner_turn_input(
    text: str,
    *,
    conversation_context: str = "",
    runtime_facts: dict[str, Any] | None = None,
    permission_ceiling: str = "write",
    durable_constraints: str = "",
    memory_context: str = "",
) -> PromptAssembly:
    blocks: list[PromptBlock] = []
    if conversation_context.strip():
        blocks.append(
            PromptBlock(
                "CONVERSATION HISTORY",
                "turn_input",
                "conversation_context",
                conversation_context.strip(),
                trusted=False,
                mutable=True,
            )
        )
    blocks.extend(
        [
            PromptBlock(
                "USER MESSAGE",
                "turn_input",
                "current_user_message",
                text,
                trusted=False,
                mutable=True,
            ),
            PromptBlock(
                "PERMISSION CEILING",
                "turn_input",
                "operating_context.permission_ceiling",
                permission_ceiling,
            ),
        ]
    )

    if runtime_facts:
        # Runtime facts may already contain bounded typed projections (for
        # example attempt_history -> facts -> windows -> item). Preserve those
        # ordinary records at the final assembly boundary instead of truncating
        # them a second time solely because they are nested.
        projected_runtime_facts = project_model_facts(
            runtime_facts,
            max_depth=PLANNER_RUNTIME_FACT_MAX_DEPTH,
        )
        blocks.append(
            PromptBlock(
                "RUNTIME FACTS",
                "turn_input",
                "runtime.facts",
                json.dumps(
                    projected_runtime_facts,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ),
                trusted=False,
                mutable=True,
            )
        )

    if memory_context.strip():
        blocks.append(
            PromptBlock(
                "MEMORY RECALL",
                "turn_input",
                "memory.recall",
                memory_context.strip(),
                trusted=False,
                mutable=True,
            )
        )

    if durable_constraints.strip():
        # Durable constraints are reloaded from the governed memory
        # store every turn so they survive context compression. They are trusted
        # runtime state (Navi's own store), not untrusted conversation text, and
        # rank above conversation history as a must/must-not boundary.
        blocks.append(
            PromptBlock(
                "DURABLE CONSTRAINTS",
                "turn_input",
                "memory.constraints",
                durable_constraints.strip(),
            )
        )

    return PromptAssembly("planner_turn_input", tuple(blocks))


def assemble_planner_tool_manifest(tools: list[ToolSpec]) -> PromptAssembly:
    """Build the stable capability prefix separately from mutable turn facts."""
    return PromptAssembly(
        "planner_tool_manifest",
        (
            PromptBlock(
                "TOOL MANIFEST",
                "manifest",
                "capability_registry",
                json.dumps(
                    [_planner_tool_manifest_entry(tool) for tool in tools],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        ),
    )


def _planner_tool_manifest_entry(tool: ToolSpec) -> dict[str, Any]:
    """Project the registry contract needed for planning, not runtime internals.

    The full ToolSpec remains authoritative in the registry and executor.  In
    particular, embedding every output JSON Schema and governance field in
    every planner call made the prompt larger than the task facts themselves.
    A planner needs valid inputs, permission/effect boundaries, and the names
    of facts it can observe; the executor still validates the complete schema.
    """
    output_properties = tool.output_schema.get("properties")
    output_fields = (
        sorted(str(key) for key in output_properties) if isinstance(output_properties, dict) else []
    )
    side_effect = tool.side_effect_policy.to_dict()
    return {
        "name": tool.name,
        "capability_class": tool.capability_class,
        "description": tool.description,
        "input_schema": _planner_input_schema(tool.input_schema),
        "permission": tool.permission,
        "facts_only": tool.facts_only,
        "mutates": tool.mutates,
        "side_effect": {
            "mode": side_effect.get("mode", "none"),
            "commit_tool": side_effect.get("commit_tool", ""),
            "compensate_tool": side_effect.get("compensate_tool", ""),
        },
        "output_fields": output_fields,
    }


def _planner_input_schema(value: Any) -> Any:
    """Remove prose duplicated by the tool description; retain validation shape."""
    if isinstance(value, dict):
        return {
            str(key): _planner_input_schema(item)
            for key, item in value.items()
            if not (str(key) == "description" and not isinstance(item, dict))
        }
    if isinstance(value, list):
        return [_planner_input_schema(item) for item in value]
    return value


def assemble_responder_system_prompt(
    layers: Iterable[PromptLayer],
    context: OperatingContext,
) -> PromptAssembly:
    blocks = []
    for layer in layers:
        if not context.allows_prompt_layer(layer.name):
            continue
        if not permission_allows(layer.minimum_permission, context.permission_ceiling):
            continue
        content = layer.content.strip()
        if not content:
            continue
        tier = _responder_tier(layer.name)
        blocks.append(PromptBlock(layer.name, tier, f"prompt_layer.{layer.name}", content))
    return PromptAssembly("responder_system", tuple(blocks))


def assemble_fact_response_system_prompt() -> PromptAssembly:
    return PromptAssembly("fact_response_system", _prompt_spec_blocks("fact_response_system"))


def assemble_fact_response_turn_input(
    *,
    user_text: str,
    facts: dict[str, Any],
) -> PromptAssembly:
    return PromptAssembly(
        "fact_response_turn_input",
        (
            PromptBlock(
                "USER MESSAGE",
                "turn_input",
                "current_user_message",
                user_text,
                trusted=False,
                mutable=True,
            ),
            PromptBlock(
                "VERIFIED FACTS",
                "turn_input",
                "runtime.final_facts",
                json.dumps(
                    project_model_facts(facts),
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ),
                trusted=False,
                mutable=True,
            ),
        ),
    )


def assemble_notification_system_prompt() -> PromptAssembly:
    return PromptAssembly("notification_system", _prompt_spec_blocks("notification_system"))


def assemble_notification_turn_input(*, facts: dict[str, Any]) -> PromptAssembly:
    return PromptAssembly(
        "notification_turn_input",
        (
            PromptBlock(
                "VERIFIED BACKGROUND FACTS",
                "turn_input",
                "runtime.background_facts",
                json.dumps(
                    project_model_facts(facts),
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ),
                trusted=False,
                mutable=True,
            ),
        ),
    )


def assemble_semantic_checker_messages(
    *,
    objective: str,
    acceptance_criteria: list[str],
    conversation_context: dict[str, Any],
    current_time: dict[str, Any],
    trigger_facts: dict[str, Any],
    task_context: dict[str, Any],
    evaluation_contract: dict[str, Any],
    attempt: int,
    max_attempts: int,
    last_capability: dict[str, Any],
    observed_capability_evidence: list[dict[str, Any]],
) -> list[ChatMessage]:
    return [
        ChatMessage(
            "system",
            _prompt_spec_content("semantic_checker_messages", "SEMANTIC CHECKER SYSTEM"),
        ),
        ChatMessage(
            "user",
            json.dumps(
                {
                    "objective": objective,
                    "acceptance_criteria": acceptance_criteria,
                    "conversation_context": conversation_context,
                    "current_time": current_time,
                    "trigger_facts": trigger_facts,
                    "task_context": task_context,
                    "evaluation_contract": evaluation_contract,
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "last_capability": last_capability,
                    "observed_capability_evidence": observed_capability_evidence,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        ),
    ]


def assemble_memory_consolidation_messages(
    *,
    task_prompt: str,
    transcript: list[dict[str, Any]],
    active_memory: list[dict[str, Any]],
    scope: str,
) -> list[ChatMessage]:
    system = PromptAssembly(
        "memory_consolidation_system",
        (
            PromptBlock(
                "MEMORY CONSOLIDATOR TASK",
                "evolvable",
                "prompt_layer.task_memory_consolidator",
                task_prompt,
            ),
            *_prompt_spec_blocks("memory_consolidation_messages"),
        ),
    )
    user = PromptAssembly(
        "memory_consolidation_input",
        (
            PromptBlock(
                "MEMORY CONSOLIDATION EVIDENCE",
                "turn_input",
                "memory.consolidation_job",
                json.dumps(
                    {
                        "transcript": transcript,
                        "active_memory": active_memory,
                        "scope": scope,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ),
                trusted=False,
                mutable=True,
            ),
        ),
    )
    return [ChatMessage("system", system.render()), ChatMessage("user", user.render())]


def assemble_goal_event_compaction_messages(lines: Iterable[str]) -> list[ChatMessage]:
    system = PromptAssembly(
        "goal_event_compaction_system",
        _prompt_spec_blocks("goal_event_compaction_messages"),
    )
    user = PromptAssembly(
        "goal_event_compaction_input",
        (
            PromptBlock(
                "GOAL EVENTS",
                "turn_input",
                "goal.event_log",
                "\n".join(lines),
                trusted=False,
                mutable=True,
            ),
        ),
    )
    return [ChatMessage("system", system.render()), ChatMessage("user", user.render())]


def planner_prompt_manifest() -> dict[str, Any]:
    return assemble_planner_system_prompt().manifest()


def _iterable_prompt_values(values: object) -> list[object]:
    if isinstance(values, (list, tuple, set, frozenset)):
        return list(values)
    return []


def _list_block(name: str, tier: str, source: str, values: object) -> PromptBlock:
    items = [str(item) for item in _iterable_prompt_values(values)]
    return PromptBlock(name, tier, source, "\n".join(f"- {item}" for item in items))


def _prompt_spec_blocks(assembly_name: str) -> tuple[PromptBlock, ...]:
    data = PROMPT_ASSEMBLIES_SPEC or {}
    assembly = data.get(assembly_name) if isinstance(data, dict) else None
    if not isinstance(assembly, dict):
        return ()
    raw_blocks = assembly.get("blocks")
    if not isinstance(raw_blocks, list):
        return ()
    blocks: list[PromptBlock] = []
    for raw_block in raw_blocks:
        if not isinstance(raw_block, dict):
            continue
        name = str(raw_block.get("name") or "").strip()
        content = str(raw_block.get("content") or "")
        if not name or not content.strip():
            continue
        blocks.append(
            PromptBlock(
                name,
                str(raw_block.get("tier") or "stable"),
                str(raw_block.get("source") or f"prompt_specs.{assembly_name}.{name}"),
                content,
                trusted=bool(raw_block.get("trusted", True)),
                mutable=bool(raw_block.get("mutable", False)),
            )
        )
    return tuple(blocks)


def _prompt_spec_content(assembly_name: str, block_name: str) -> str:
    for block in _prompt_spec_blocks(assembly_name):
        if block.name == block_name:
            return block.content
    raise RuntimeError(f"missing prompt spec block: {assembly_name}.{block_name}")


def _responder_tier(layer_name: str) -> str:
    if layer_name in {"identity", "authorization", "style"}:
        return "stable"
    if layer_name in {"runtime", "memory", "skills"}:
        return "volatile"
    return "context"
