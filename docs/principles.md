# Navi Non-Negotiable Principles

This document defines the product and engineering constraints that Navi must not violate.

Navi is not trying to copy Hermes or OpenClaw. We learn from their strengths, but we also treat their public failure modes as design input.

## Core Principles

### 1. Agentic by Architecture

Navi must be agentic in the system shape, not just in wording.

- The agent decides from current facts, available capabilities, trust state, and connector affordances.
- Static prompts must not encode product behavior that belongs to runtime discovery.
- Connectors, providers, tools, and deployment surfaces must describe what they can do at runtime.
- The agent should choose among answer, ask, plan, task, approve, execute, observe, or remember.
- Learning must happen through explicit memory, graph, trust, and evolution records, not hidden prompt drift.
- Intent selection must be capability-driven rather than keyword-driven. Keywords can parse narrow structured facts, but they must not define product behavior.

### 2. Tools Return Facts Only

Tools are fact sensors and actuators. They must not smuggle policy or advice into results.

- Every tool must have an inspectable spec: name, description, input schema, output schema, mutation flag, permission class, and source.
- Tools must be callable from CLI before connectors rely on them.
- A status tool returns status, not a recommendation.
- A filesystem tool returns entries and metadata, not cleanup advice.
- A provider tool returns configured models and health, not model preference.
- A service tool returns active/enabled/log facts, not restart suggestions.
- Interpretation, prioritization, and next-step decisions belong to the agent layer.

### 3. CLI First

Every durable capability should have a headless CLI contract before it becomes a Web or connector feature.

- CLI is the control plane.
- Connectors are interaction surfaces.
- Web is an operator console.
- Agent reasoning is the decision layer.
- Anything that cannot be run, tested, logged, or replayed from CLI is not a stable capability yet.

### 3.1 Natural Mode Switching

Navi should not expose rigid internal modes as the main user experience.

- Users should not need to say "plan mode" or "tool mode" for ordinary work.
- The agent should naturally choose answering, asking, fact lookup, proposal, approval, execution, or memory update.
- Planning is a reasoning or tool-preparation step, not the product's default interaction mode.
- Coding-oriented execution providers are just providers. Navi is a personal assistant and must not frame all tasks as coding tasks.

### 4. Connector Agnostic Core

The core runtime must not know Weixin, Feishu, WeCom, Telegram, Slack, or any future channel as hardcoded behavior.

- Connector-specific commands and approval syntax are connector context, not base prompt.
- The base prompt must not mention connector commands unless a connector injects them.
- A connector exposes facts and affordances; the agent decides how to use them.
- Adding a connector must not require rewriting the core prompt.
- Connector command surfaces should be orthogonal: prefer `/object action ...` over scattered top-level verbs.
- Core commands should be reusable across connectors; connector-local commands may only manage connector-local state such as the current session.

### 5. Skills, Plugins, and Hooks Have Separate Jobs

Navi must keep extension boundaries explicit.

- `skill`: teaches the agent how to do something. It is promptable knowledge, a playbook, or a reusable procedure. A skill should not own long-running processes, credentials, or hidden side effects.
- `plugin`: adds capabilities or integrations. It may provide tools, connectors, providers, schemas, commands, storage adapters, or UI surfaces. A plugin owns code and must declare permissions.
- `hook`: observes or gates lifecycle events. It can validate, enrich, block, log, or trigger follow-up work around events such as before-task, after-tool, before-approval, after-message, or before-memory-write.
- Skills are for behavior guidance.
- Plugins are for capability installation.
- Hooks are for event policy and orchestration.
- A feature that needs credentials, network access, filesystem mutation, or a daemon is a plugin, not a skill.
- A feature that only changes how the agent reasons or performs a workflow is a skill, unless it needs new code execution.
- A feature that must run at a lifecycle boundary is a hook, not inline agent logic.
- Hooks must return facts or decisions, not general advice.
- Plugins and hooks must be inspectable from CLI before being exposed to connectors.

### 6. Runtime Facts Over Hardcoding

Navi should discover and inject current facts instead of assuming them.

- Do not hardcode localhost URLs, channel names, model names, account ids, command syntax, or service availability.
- A value can enter the prompt only from config, environment, runtime inspection, memory, connector context, or a tool result.
- If a fact is unknown, say it is unknown narrowly.
- Never convert one local deployment observation into a global product rule.

### 7. Audit First

Any action that can affect the user's machine, accounts, remote services, repository, files, credentials, or money must be traceable.

- Use task records for local actions.
- Use approval records for gated execution.
- Use execution logs for commands and tool calls.
- Store results separately from plans.
- Preserve enough context to answer: who asked, what was decided, what ran, what changed, and why.

### 8. Approval Is State, Not Vibes

User intent in chat is not the same as an executable permission grant.

- "I authorize you" is an intent signal.
- Execution still needs a tracked task and the configured approval path unless a trust rule explicitly allows it.
- Approval source of truth must be inspectable and repairable.
- Approval failures must say which state is missing, not pretend the agent has no local capability.

### 9. Memory Must Be Governed

Memory is not a text dump. Uncontrolled memory creates drift, stale recalls, and unsafe behavior.

- Separate episodic transcripts, durable user facts, project facts, trust rules, and learned procedures.
- Memory writes must have source, timestamp, scope, and reason.
- Memory retrieval must be selective and explainable.
- Old or conflicting memories must be surfaced as conflicts, not silently merged.
- Background learning should be separate from foreground task execution.

### 10. Memory Should Be an Operating System, Not a Notebook

Navi's memory should be more innovative than markdown files or generic vector recall.

The design should combine human memory principles with LLM constraints. Human memory is layered, cue-driven, capacity-limited, imperfect, and shaped by repeated use. LLM context is long but brittle: it overweights recent text, drops negations during summarization, confuses similar recalls, and treats stale statements as current facts unless forced to verify them. Navi should compensate for both sets of weaknesses.

- Treat memory as typed state: identity, preference, project, environment, procedure, constraint, decision, artifact, relationship, and observation.
- Every memory item needs provenance: source event, author, confidence, scope, created time, last verified time, and expiry policy.
- Recall should be goal-directed. Retrieve what helps the current task, not whatever is semantically nearby.
- Memories should have lifecycle states: proposed, accepted, active, contradicted, stale, archived, and revoked.
- The agent should be able to explain why a memory was retrieved and how it influenced the decision.
- Memory must support negative knowledge: things the user rejected, things that failed, and constraints that must not be repeated.
- Memory writes must be reviewable and reversible.
- Memory is not the policy engine. Trust, approval, and safety constraints must live in explicit stores.
- The memory system must preserve user constraints and relevant long-term memory even when the conversation is long or summarized.
- Retrieval must prefer current task relevance, recency, verified durability, and constraint priority over raw semantic similarity.

### 11. Self-Evolution Must Be Governed

Navi should evolve, but never mutate itself silently.

- Self-evolution starts as a proposal with a reason, expected benefit, affected target, and rollback plan.
- Evolution targets include prompts, tools, skills, connector affordances, memory schemas, trust rules, and workflows.
- Applying an evolution requires the configured approval policy unless it is purely observational.
- Every applied evolution must create a ledger event with before, after, diff, source task, and rollback status.
- The agent must evaluate whether an evolution improved outcomes using evidence, not vibes.
- Failed evolutions should lower trust in the changed rule or workflow.
- The agent must distinguish between user preference, environmental fact, one-off workaround, and reusable skill before evolving.
- Evolution must never create broader permissions as a side effect.

### 12. Context Compression Must Preserve Constraints

Long-running agents fail when compression drops safety instructions or user constraints.

- Plans, approvals, denials, safety constraints, and "do not act" instructions are durable state.
- They must not live only inside the model context window.
- Before destructive execution, reload durable constraints from stores.
- If constraints conflict or are missing, stop and ask.

### 13. Least Capability by Default

Agentic does not mean broad access by default.

- Default to read-only or preparation modes for new connectors and providers.
- Enable write, shell, network, account, and production capabilities explicitly.
- Prefer allowlists over denylists for dangerous tools.
- Secrets must be redacted in prompts, logs, Web views, and connector replies.

### 14. Tool Calling Must Be Deterministic

LLM tool calling is fragile across providers, templates, streaming modes, and parsers.

- Treat tool-call parser compatibility as a provider health check.
- Log raw tool-call failures separately from model reasoning failures.
- Prefer low-temperature deterministic settings for tool execution.
- A tool call that cannot be parsed is a failed tool call, not a chat answer.
- Streaming must not be enabled for tool paths unless parser behavior is verified.

### 15. Environment Truth Is Local

The agent must not hallucinate the user's environment.

- Inspect paths, service state, provider state, connector state, and current workspace before claiming them.
- WSL, Docker, native Linux, macOS, and remote shells are different environments.
- A connector view of the world may differ from the service host view.
- If the agent cannot inspect a fact, it must report the inspection gap precisely.

## Lessons From Public Failure Modes

OpenClaw demonstrated that proactive local agents can be useful, but also exposed core risks: broad 24/7 access, memory files that drive behavior, scheduled background activity, and connectors reaching sensitive accounts. Public incidents and safety writeups emphasize sandboxing, minimal credentials, explicit approvals, and durable constraints.

Hermes shows a different class of lessons: memory providers, context injection, background recall, and tool-calling pipelines are powerful, but users report path issues, noisy recall, memory drift, and model/template/tool-parser failures. These failures argue for provider health checks, connector-specific affordance injection, and memory governance rather than bigger prompts.

OpenClaw and Hermes both show that "more memory" is not automatically better memory. A local assistant needs memory triage, provenance, expiry, contradiction handling, and rollback. Otherwise memory becomes a behavior-changing prompt pile that users cannot audit.

## Design Implication For Navi

Navi's architecture should keep these boundaries:

- `tools/`: fact-only probes and controlled actions.
- `capabilities/`: stable CLI contracts.
- `connectors/`: per-channel affordance injection.
- `skills/`: promptable procedures and behavior guidance.
- `plugins/`: installed code capabilities, integrations, providers, and connector surfaces.
- `hooks/`: lifecycle gates and observers.
- `agent/`: intent, preparation, policy, approval, and reflection.
- `memory/graph/trust/evolution`: typed, auditable learning and constraint stores.
- `runtime prompt`: composed from current facts, not hand-written assumptions.

## Memory Innovation Direction

Navi should evolve toward a memory architecture with these layers:

- `episode`: raw conversations, tool results, and task logs. Append-only.
- `fact`: verified durable facts about the user, machine, projects, providers, and connectors.
- `preference`: user style and workflow preferences, scoped by context.
- `constraint`: durable "must" and "must not" rules that survive context compression.
- `procedure`: reusable ways to perform tasks, only promoted after evidence.
- `skill`: executable or promptable playbooks with versioning and rollback.
- `relationship`: graph edges among people, projects, tools, files, services, and tasks.
- `hypothesis`: unverified learning candidates waiting for confirmation.

Memory promotion should follow a pipeline:

1. Observe: capture raw event or task outcome.
2. Extract: propose candidate memories with type, scope, confidence, and source.
3. Validate: check contradiction, freshness, and whether the user actually endorsed it.
4. Promote: move accepted items into active memory.
5. Apply: retrieve goal-relevant memory with an explanation.
6. Review: show what memory affected an answer or action.
7. Retire: mark stale, contradicted, or revoked memories inactive.

The key innovation is not vector search. It is memory governance plus agentic use: the assistant should know what it knows, why it knows it, when it last verified it, and whether it is allowed to act on it.

## Memory Control System

Navi should implement memory as a control system rather than a passive store:

- `working memory`: current goal, active constraints, plan, unresolved questions, and approval state.
- `constraint memory`: durable must/must-not rules, safety boundaries, user instructions, and project principles.
- `episodic memory`: append-only conversations, tasks, tool results, and execution traces.
- `semantic memory`: verified facts about users, projects, providers, connectors, services, and environments.
- `procedural memory`: reusable ways to do work, promoted only after evidence and scoped by context.
- `preference memory`: user style and workflow preferences, scoped and revocable.
- `negative memory`: rejected ideas, failed approaches, hazards, and "do not repeat" lessons.
- `skill memory`: versioned playbooks or capabilities that can be inspected, tested, and rolled back.

Long-context handling must follow these rules:

- Constraint memory has priority over transcript completeness.
- Before execution, reload constraint, trust, and approval state from durable stores instead of trusting the prompt window.
- Summaries must preserve negations, denials, pending approvals, "do not" instructions, and unresolved questions.
- Similar memories are candidates, not facts.
- Stale environment facts must be reverified before action.
- User corrections should create negative memory or contradiction markers.
- A memory may influence an answer only if its scope and confidence match the current task.
- The agent should be able to show which memories influenced an answer or action.

## References

- PCWorld: OpenClaw's always-on design, markdown memory, scheduled activities, and prompt-injection risk: https://www.pcworld.com/article/3064874/openclaw-ai-is-going-viral-dont-install-it.html
- Windows Central: public OpenClaw email deletion incident after context/instruction loss: https://www.windowscentral.com/artificial-intelligence/meta-summer-yue-director-openclaw-ai-email-deletion
- TechRadar: OpenClaw security guidance around credentials, sandboxing, and tool policy: https://www.techradar.com/pro/security/microsoft-says-openclaw-is-unsuited-to-run-on-standard-personal-or-enterprise-workstation-so-should-you-be-worried
- TechRadar: OpenClaw skill security and third-party skill risk: https://www.techradar.com/pro/what-are-openclaw-skills-a-detailed-guide
- OpenClaw security analysis papers: https://arxiv.org/abs/2604.03131 and https://arxiv.org/abs/2603.10387
- Hermes Agent memory provider docs: https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/memory-providers.md
- Hermes memory provider discussion: source-labeled and inspectable local memory: https://www.reddit.com/r/hermesagent/comments/1svf120/hermes_local_memory_localfirst_agentcontrolled/
- Hermes memory issues: path/backend mismatch and silent memory failure reports: https://www.reddit.com/r/hermesagent/comments/1t8jkw8/hermes_memory_issues/
- Hermes token bloat/context creep discussion: memory overlap and redundant prompt injection: https://www.reddit.com/r/hermesagent/comments/1siv7s0/master_thread_solving_token_bloat_context_creep/
