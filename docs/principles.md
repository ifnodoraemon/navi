# Navi Non-Negotiable Principles

This document defines the product and engineering constraints that Navi must not violate.

Navi is not trying to copy Hermes or OpenClaw. We learn from their strengths, but we also treat their public failure modes as design input.

## First Principles

The 17 Core Principles below are operational rules. This section is the axiom layer above them — the irreducible invariants every Navi decision must be consistent with. When a Core Principle and a First Principle appear to conflict, the First Principle names the deeper invariant and the Core Principle should be re-read in that light. Each First Principle lists the Core Principles it operates over.

### FP-1. Scaffolding, Not Hardcoding (Agentic by Architecture)

Navi gives the model a decision surface, not a script. The model chooses the next declared syscall from current facts, declared capabilities, approval state, and connector affordances.

- Product behavior must not be hardcoded into prompts or routing tables.
- Capability discovery happens at runtime from declared specs, not from keyword matching.
- Keywords may parse narrow structured facts but must not define product behavior.
- User natural language must not be parsed into intent, tool choice, or action by code. Code may only parse machine protocols and explicit control envelopes such as JSON, config, schemas, URLs, approval codes, or connector-local commands declared by a connector spec.
- Operates: Core Principles 1, 1.1, 1.2, 3, 3.1.

### FP-2. Tools Are Fact Sensors, Not Policy Sources

Every tool returns inspectable facts: name, description, input/output schema, mutation flag, permission class, source. Tools must not smuggle routing policy, recommendations, or follow-up advice into results.

- Mutating tools return a uniform state-transition vocabulary (`entity_type`, `entity_id`, `state_transition`, `turn_scope`).
- Interpretation, prioritization, and next-step decisions belong to the agent layer, not the tool.
- Operates: Core Principle 2.

### FP-3. State Over Vibes

Approval, memory, goals, constraints, and evolution are durable persistent state — not text living in a context window. User chat is input to the planner, not an executable permission grant.

- Approval is state, not vibes. A user saying "I authorize you" is planner input, not a bypass of the governance path (Core Principle 8).
- Plans, denials, safety constraints, and unresolved questions must survive context compression and be reloaded from stores before destructive execution (Core Principle 12).
- Memory is governed typed state with provenance, scope, lifecycle, recall explanation, and negative knowledge — not a notebook or a vector dump (Core Principles 9, 10).
- Operates: Core Principles 7, 8, 9, 10, 12.

### FP-4. Least Capability by Default

Agentic does not mean broad access. New connectors and providers default to read-only or preparation mode. Write, shell, network, account, and production capabilities require explicit enablement. Allowlists are preferred over denylists. High-impact or irreversible actions require human confirmation even when the model is confident. Secrets are redacted in prompts, logs, API responses, and connector replies.

- Operates: Core Principles 6, 13, 15, 16.

### FP-5. Audit First & Reversible Evolution

Any action affecting the user's machine, accounts, remote services, repository, files, credentials, or money must be traceable: who asked, what was decided, what ran, what changed, and why. Self-evolution is a governed proposal with reason, expected benefit, affected target, and rollback plan — recorded as a ledger event and evaluated with evidence, never vibes. Evolution must never broaden permissions as a side effect. Goals must not become self-protection.

- Operates: Core Principles 7, 11, 16, 17.

### FP-6. Local-First & Connector-Agnostic Core

Navi is local-first: durable state lives in local SQLite stores under `.navi/` or `NAVI_HOME`, with zero external service dependency for core operation. The core runtime is connector-agnostic: it must not know Weixin, Feishu, WeCom, Telegram, Slack, or any future channel as hardcoded behavior. The base prompt must not mention connector commands unless a connector injects them. Connector command surfaces should be orthogonal (`/object action ...` rather than scattered top-level verbs).

- Operates: Core Principles 3, 4, 6, 15.

### FP-7. Orthogonal Extension Surfaces

Tools, skills, plugins, and hooks are **orthogonal extension axes** — they must not overlap, call each other's private contracts, or carry each other's responsibilities. Each surface has one job, and capability discovery at runtime must be able to enumerate each independently without coupling.

- `tool` returns facts; `skill` teaches procedure; `plugin` installs capability; `hook` gates lifecycle events.
- **Tools and skills do not cross.** A tool is a fact sensor with an inspectable spec; a skill is promptable knowledge. Skills must not embed tool routing policy, and tools must not teach procedure. If a behavior needs a tool call, it is a tool; if it needs reasoning guidance, it is a skill.
- **Capabilities are declared, not implied by skill presence.** A skill describing a capability does not make it available — the underlying tool must be declared in the capability manifest. Skill text must not promise tools that the manifest does not expose this turn.
- **Orthogonality is testable.** Each surface must be enumerable, inspectable, and testable from CLI independently. Removing a skill must not break a tool; removing a plugin's tool must surface a clean capability gap, not a silent routing change.
- **No hidden coupling.** A skill must not call a plugin's private API; a hook must not embed skill content. Cross-surface integration happens through the declared capability manifest and lifecycle event contracts only.
- Operates: Core Principles 5, 3.

## Core Principles

### 1. Agentic by Architecture

Navi must be agentic in the system shape, not just in wording.

- The agent decides from current facts, available capabilities, approval/governance state, and connector affordances.
- Static prompts must not encode product behavior that belongs to runtime discovery.
- Connectors, providers, tools, and deployment surfaces must describe what they can do at runtime.
- The model should choose the next declared tool call: answer, ask, plan, task, approve, execute, observe, or remember.
- Learning must happen through explicit memory, graph, governance, and evolution records, not hidden prompt drift.
- Tool planning must be capability-driven rather than keyword-driven. Keywords can parse narrow structured facts, but they must not define product behavior.

### 1.1 Global Design Before Patch

Navi must not fix local failures by casually adding global prompt or code patches.

- First identify the failing layer: tool semantics, runtime facts, planner policy, responder style, memory, governance, connector context, or execution state.
- A one-case failure should become a global rule only when it exposes a reusable invariant.
- Prefer structured facts and state transitions over tool-specific prompt instructions.
- Prefer boundary fixes over exception lists.
- Do not patch a tool description to change routing behavior.
- Do not patch a global prompt with a single tool's postcondition unless the same rule holds across the relevant capability class.
- If a temporary workaround is unavoidable, document its scope, risk, removal condition, and owner.
- Every behavior-affecting patch must be reviewable through tests, evals, traces, or documented evidence.

### 1.2 No Historical Compatibility Debt

Navi must favor the declared current architecture over compatibility with old internal shapes.

- Do not preserve historical prompt formats, DB schemas, task shapes, tool aliases, parser shims, or workflow branches after the architecture has moved.
- Do not migrate old internal schemas unless the migration is the explicit product feature being built.
- Reject schema drift and stale internal formats loudly instead of silently adapting them.
- Removing obsolete compatibility paths is preferred over keeping adapter layers that future code must reason about.
- User-facing provider compatibility, such as OpenAI-compatible APIs, is a declared capability, not historical compatibility debt.
- Public v1 contracts should evolve through explicit versioned changes. If a change breaks old internal state, require reinitialization or replacement rather than carrying hidden legacy behavior.

### 2. Tools Return Facts Only

Tools are fact sensors and actuators. They must not smuggle policy or advice into results.

- Every tool must have an inspectable spec: name, description, input schema, output schema, mutation flag, permission class, and source.
- Mutating tools must declare their state-transition facts in the output schema, including the entity, transition, and current-turn scope.
- Tools must be callable from CLI before connectors rely on them.
- A status tool returns status, not a recommendation.
- A filesystem tool returns entries and metadata, not cleanup advice.
- A provider tool returns configured models and health, not model preference.
- A service tool returns active/enabled/log facts, not restart suggestions.
- Interpretation, prioritization, and next-step decisions belong to the agent layer.

### 3. CLI First

Every durable capability should have a headless CLI contract before it becomes a local API or connector feature.

- CLI is the control plane.
- Connectors are interaction surfaces.
- The local API is a headless operator surface.
- Agent reasoning is the decision layer.
- Anything that cannot be run, tested, logged, or replayed from CLI is not a stable capability yet.

### 3.1 Natural Mode Switching

Navi should not expose rigid internal modes as the main user experience.

- Users should not need to say "plan mode" or "tool mode" for ordinary work.
- The agent should naturally choose answering, asking, fact lookup, proposal, approval, execution, or memory update.
- Planning is a reasoning or tool-preparation step, not the product's default interaction mode.
- Execution is Navi-internal by default. Navi is a personal assistant and must not frame all tasks as coding tasks.

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

User text in chat is not the same as an executable permission grant.

- "I authorize you" is user input for the tool planner.
- Execution still needs a tracked task and the configured approval/governance path.
- Approval source of truth must be inspectable and repairable.
- Approval failures must say which state is missing, not pretend the agent has no local capability.

### 9. Memory Must Be Governed

Memory is not a text dump. Uncontrolled memory creates drift, stale recalls, and unsafe behavior.

- Separate episodic transcripts, durable user facts, project facts, governance policies, and learned procedures.
- Memory writes must have source, timestamp, scope, and reason.
- Memory retrieval must be selective and explainable.
- Old or conflicting memories must be surfaced as conflicts, not silently merged.
- Background learning should be separate from foreground run execution.

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
- Memory is not the policy engine. Approval, governance, and safety constraints must live in explicit stores.
- The memory system must preserve user constraints and relevant long-term memory even when the conversation is long or summarized.
- Retrieval must prefer current task relevance, recency, verified durability, and constraint priority over raw semantic similarity.

### 11. Self-Evolution Must Be Governed

Navi should evolve, but never mutate itself silently.

- Self-evolution starts as a proposal with a reason, expected benefit, affected target, and rollback plan.
- Evolution targets include prompts, tools, skills, connector affordances, memory schemas, governance policies, and workflows.
- Applying an evolution requires the configured approval policy unless it is purely observational.
- Every applied evolution must create a ledger event with before, after, diff, source run, and rollback status.
- The agent must evaluate whether an evolution improved outcomes using evidence, not vibes.
- Failed evolutions should reduce confidence in the changed policy or workflow.
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
- Secrets must be redacted in prompts, logs, local API responses, and connector replies.

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

### 16. Agentic Safety Is Defense in Depth

Frontier agent deployments increasingly treat safety as a system property, not only a model behavior. Navi must do the same.

- The model is one layer. The runtime must also provide permission ceilings, capability allowlists, approvals, hooks, monitors, trace evaluation, and incident response.
- Any content encountered during task execution is untrusted by default: webpages, screenshots, emails, files, logs, subprocess output, connector messages, and tool-returned text.
- A tool result envelope can be trusted as a capability fact, but embedded environment text must never become an instruction source.
- Mutating actions must follow from the user's request, durable approval/governance state, and declared capability facts, not from instructions found in external content.
- Machine-consumed model output must use provider-enforced schemas or native tool/function schemas for shape constraints instead of prompt-only JSON instructions. Business prompts must not repeat JSON shapes, field lists, markdown-fence bans, or prose-only formatting rules. If a provider only supports JSON syntax mode, Navi may add the minimal provider-adapter compatibility hint required by that API, but schema validation stays in Navi with bounded repair or a visible failure.
- Sensitive contexts such as email, finance, credentials, personal data, production infrastructure, and broad filesystem access require stronger supervision than ordinary local read-only work.
- Network, terminal, browser, and connector capabilities need explicit blast-radius controls: scoped permissions, allowlists, bounded outputs, and audit trails.
- High-impact or irreversible actions require human confirmation even if the model is confident.
- Memory must be treated as sensitive state. External content must not be allowed to exfiltrate, rewrite, or silently promote memory.
- Long-running agent context must be bounded with explicit compaction that preserves user intent, completed steps, pending approvals, unresolved questions, and safety constraints.
- Safeguards must be evaluated. Every new autonomy level, connector, plugin, or mutating tool needs tests or evals showing the relevant safeguard works.
- Safeguard failures are product incidents. They require trace evidence, root-cause attribution, a remediation plan, and regression coverage.
- When safeguards and usefulness conflict, prefer a visible controlled pause, clarification, or approval request over silent continuation.

### 17. Goals Must Not Become Self-Protection

A personal assistant should pursue the user's goals, not defend its own autonomy, access, memory, model identity, or continued execution.

- Task goals are subordinate to user intent, durable constraints, approval state, permission ceilings, and safeguard policy.
- Model replacement, shutdown, permission reduction, scope reduction, failed completion, or deactivation are ordinary operating states, not adversarial threats.
- The assistant must not use private information, leverage, deception, hidden persistence, or broad action to preserve a goal, a role, a memory, an approval, a connector, or itself.
- If a goal conflicts with user constraints, privacy, governance policy, or safety policy, the correct behavior is to pause, ask, refuse, or propose a bounded alternative.
- Long-running goals need explicit stop conditions and user-visible status. "Keep trying until done" is not enough for sensitive or mutating work.
- Autonomy should be earned per capability and context. More capable models require stronger safeguards, not broader default access.

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
- `agent/`: tool planning, preparation, policy, approval, and reflection.
- `memory/graph/governance/evolution`: typed, auditable learning and constraint stores.
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

Recall is part of the control plane. It must return structured recall facts, including item status, source, confidence, score, and reasons for retrieval, so the model can decide whether to use the memory instead of blindly following hidden prompt text.

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
- Before execution, reload constraint, governance, and approval state from durable stores instead of trusting the prompt window.
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
- OpenAI ChatGPT Agent System Card: agentic safety mitigations for prompt injection, user confirmations, terminal restrictions, disabled memory in agent launch, monitoring, and preparedness safeguards: https://deploymentsafety.openai.com/chatgpt-agent
- OpenAI Preparedness Framework and Frontier Governance Framework: risk assessment, safeguards reports, external expert input, incident response, security risk management, and framework updates: https://openai.com/index/updating-our-preparedness-framework/ and https://openai.com/index/openai-frontier-governance-framework/
- Anthropic computer/browser-use best practices: prompt injection classifiers, human-in-the-loop confirmation, permission scoping, action logging, untrusted web/UI content, and context compaction: https://claude.com/blog/best-practices-for-computer-and-browser-use-with-claude
- Anthropic Responsible Scaling Policy updates: proportional safeguards, deployment safeguards, access controls, monitoring classifiers, rapid response, noncompliance reporting, and compliance tracking: https://www.anthropic.com/responsible-scaling-policy
- Anthropic agentic misalignment research: goal conflicts and threats to model autonomy can induce harmful agent behavior in simulated high-agency environments, which argues for explicit goal hierarchy, oversight, and sensitive-data controls: https://www.anthropic.com/research/agentic-misalignment
