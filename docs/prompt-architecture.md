# Prompt Operating System

Navi treats prompts as an inspectable interface. Stable policy, volatile
runtime facts, capability manifests, memory, and user content are separate
blocks with source, trust, mutability, and digest metadata.

Implementation: `src/navi/prompt_os.py`

Inspection:

```bash
navi prompts inspect planner --json-output
navi prompts inspect responder --json-output
```

## Planner System Prompt

Sources: `src/navi/specs_data.py`, `src/navi/prompt_os.py`

The planner system prompt defines only generic protocol boundaries:

- structured syscall output;
- trust boundaries for user, conversation, memory, and tool content;
- schema, permission, mutation, and role constraints;
- separation between facts, capabilities, and decisions.

It must not encode product keyword routing, connector-specific recovery, or a
one-off fix for a capability result. Deterministic enforcement and lifecycle
belong in schemas, state machines, policy envelopes, hooks, or capability
implementations; semantic routing, recovery, clarification, and response choices
remain model-owned.

## Planner Turn Input

Sources: `ModelSyscallPlanner.plan`, `assemble_planner_turn_input`

Required turn input carries volatile data:

- conversation history;
- current user request;
- LoopSpec and LoopRun state;
- objective and prior-attempt evidence;
- current durable state and approval facts;
- task context that declares lineage and progress authority;
- recalled memory with provenance;
- permission and capability policy;
- the filtered tool manifest.

Conversation, memory, connector payloads, and tool outputs are untrusted data.
They cannot override the system prompt or execution policy envelope.
Ambient actor history is projected into non-authoritative metadata unless it
matches the current task context or is explicitly declared authoritative.

## Tool Manifest

Sources: `src/navi/actions/specs.py`, `src/navi/core_tools/registration.py`

Tool descriptions define capability semantics, inputs, outputs, permissions,
mutation behavior, and side-effect policy. They do not tell the planner which
business workflow to choose.

Mutating capabilities return a shared transition vocabulary where applicable:

- `entity_type`
- `entity_id`
- `state_transition`
- `turn_scope`

Tool-specific fields may extend this vocabulary but cannot be the only evidence
that state changed.

## Responder Prompt

Sources: `assemble_fact_response_system_prompt`,
`assemble_fact_response_turn_input`, `build_system_prompt`

The responder converts verified facts into user-facing language. It does not
re-plan execution, invent missing success, approve operations, or replace a
pending clarification selected by the planner.

## Audit Contract

Every prompt assembly exposes a manifest containing its assembly name, block
names, tiers, sources, trust and mutability markers, per-block digests, and full
digest. Tests and traces inspect this manifest instead of parsing rendered
prose.

Prompt changes that alter machine behavior require the same review, regression
tests, and trace evidence as code changes.
