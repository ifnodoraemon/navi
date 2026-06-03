# Prompt Operating System

Navi prompt content has four separate jobs. Keep them separate.

Navi treats prompts as an operating-system interface, not loose strings. A prompt is assembled from named blocks with explicit tier, source, trust, mutability, and digest metadata. The rendered text is what the model sees; the manifest is what tests, traces, and future evolution systems can inspect.

Implementation: `src/navi/prompt_os.py`

Inspection: `navi prompts inspect planner --json-output` and `navi prompts inspect responder --json-output`

Core objects:

- `PromptBlock`: one named block with tier/source/trust/mutability metadata.
- `PromptAssembly`: an ordered set of blocks that can render text and expose a digest manifest.
- `assemble_planner_system_prompt`: stable planner policy.
- `assemble_planner_turn_input`: turn-scoped data, observations, roles, and tool manifest.
- `assemble_responder_system_prompt`: user-facing response synthesis layers.

## Planner System Prompt

Sources: `src/navi/specs/syscall_planner.yaml`, `src/navi/prompt_os.py`

This prompt owns global planning behavior:

- output contract
- prompt and tool boundaries
- routing policy
- observation invariants
- security rules

It must not contain one-off fixes for a single tool result. If a rule is needed after a capability mutates state, express it as a generic state-transition invariant.

## Planner Turn Input

Sources: `ModelSyscallPlanner.plan`, `assemble_planner_turn_input`

The user message sent to the planner contains turn-scoped data:

- recent conversation inside `<conversation_history>`
- capability observations inside `<observed_facts>`
- current user request inside `<user_message>`
- permission ceiling
- model role contracts
- available tool manifest

This content is state and data, not policy. Conversation and user input are untrusted.

## Tool Manifest

Sources: `src/navi/specs/action_tools.yaml`, `src/navi/core_tools.py`

Tool descriptions define capability semantics only: what the tool can do. They do not carry routing policy, product principles, refusal rules, or follow-up behavior.

Mutating tools should return structured facts that describe state transitions, for example:

```json
{
  "entity_type": "watch",
  "entity_id": "watch-id",
  "state_transition": "created",
  "turn_scope": "current"
}
```

The planner can reason over these generic facts without tool-specific prompt patches.

All mutating capabilities should return the same minimum transition vocabulary:

- `entity_type`
- `entity_id`
- `state_transition`
- `turn_scope`

Tool-specific fields may still be present, but they must not be the only way to understand whether state changed in the current turn.

## Runtime Responder Prompt

Sources: `src/navi/specs/prompt_layers.yaml`, `build_system_prompt`, `assemble_responder_system_prompt`

`build_system_prompt` composes identity, runtime facts, authorization, memory, skills, and style for user-facing response synthesis. It should not duplicate planner routing rules.

Responder layers are not planner policy. They control how Navi explains known facts to the user.

## Audit Contract

Every prompt assembly exposes a manifest containing:

- assembly name
- block names
- tiers
- sources
- trust and mutability markers
- per-block digests
- full assembly digest

This gives prompt evolution and tests an inspectable surface without parsing rendered prose.

The CLI inspection command is the supported headless audit surface for these manifests.

## Reference Pattern

Hermes documents a similar separation: stable prompt layers, context layers, and volatile runtime layers are assembled in order, while API-call-time overlays remain separate from the cached system prompt. Navi follows the same idea but splits planner policy, tool manifests, observations, and responder persona more explicitly.
