# Prompt Architecture

Navi prompt content has four separate jobs. Keep them separate.

## Planner System Prompt

Source: `src/navi/specs/syscall_planner.yaml`

This prompt owns global planning behavior:

- output contract
- prompt and tool boundaries
- routing policy
- observation invariants
- security rules

It must not contain one-off fixes for a single tool result. If a rule is needed after a capability mutates state, express it as a generic state-transition invariant.

## Planner Turn Input

Source: `ModelSyscallPlanner.plan`

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

## Runtime Responder Prompt

Source: `src/navi/specs/prompt_layers.yaml`

`build_system_prompt` composes identity, runtime facts, authorization, memory, skills, and style for user-facing response synthesis. It should not duplicate planner routing rules.

## Reference Pattern

Hermes documents a similar separation: stable prompt layers, context layers, and volatile runtime layers are assembled in order, while API-call-time overlays remain separate from the cached system prompt. Navi follows the same idea but splits planner policy, tool manifests, observations, and responder persona more explicitly.
