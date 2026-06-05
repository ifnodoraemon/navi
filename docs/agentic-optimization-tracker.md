# Agentic Optimization Tracker

This tracker converts the current architecture review into incremental work. Check one item only after code, tests, and docs are updated for that item.

| Status | ID | Priority | Area | Optimization | Target Outcome | Primary Files |
| --- | --- | --- | --- | --- | --- | --- |
| [x] | A01 | P0 | task workspace | Make task creation honor `CapabilityContext.workspace` instead of process cwd. | Daemon, connector, API, and CLI tasks execute in the workspace that produced the request. | `src/navi/capabilities.py`, `tests/test_capabilities_daemon.py` |
| [x] | A02 | P0 | evolution | Add a first-class evolution proposal model before apply. | Prompt, memory, skill, tool, connector, trust, and workflow changes start as reviewable proposals with rollback plans. | `src/navi/evolution.py`, `src/navi/api.py`, `src/navi/cli.py` |
| [x] | A03 | P0 | evolution targets | Add a content target registry for behavior-affecting content. | Evolution targets are inspectable and include prompt layers, skills, memory schema/items, tool specs, connector specs, trust policy, workflow policy, and eval cases. | `src/navi/evolution.py`, `src/navi/prompting.py`, `src/navi/skills.py` |
| [x] | A04 | P0 | prompt | Move system prompt layers out of inline Python strings into versioned prompt-layer content. | Prompt behavior can be proposed, diffed, applied, rolled back, and evaluated. | `src/navi/prompting.py`, `src/navi/specs/`, `tests/test_skills_watches_security.py` |
| [x] | A05 | P0 | daemon | Split proactive daemon detectors into fact observation plus policy decision. | Git/log/port detectors emit facts; model-visible policy decides whether to create tasks and how to phrase next action. | `src/navi/daemon.py`, `src/navi/capabilities.py`, `tests/test_agentic_guardrails.py` |
| [x] | A06 | P0 | execution | Replace fixed self-healing loop with model-selected execution follow-up capabilities. | Retry, repair, stop, ask, or rollback are explicit agent decisions, not a hidden fixed workflow. | `src/navi/execution.py`, `src/navi/specs/action_tools.yaml`, `tests/test_trust_evolution.py` |
| [x] | A07 | P1 | delegation lifecycle | Replace the old combined entry with separate spawn, prepare, approval request, and run capabilities. | The planner can choose each delegation lifecycle step and observe intermediate state. | `navi.capabilities`, `navi.runs`, delegation eval dataset |
| [x] | A08 | P1 | trust | Move trust-level progression rules into declared trust policy. | Success thresholds, demotion rules, and L0-L4 meanings are inspectable, evolvable, and rollbackable. | `src/navi/trust.py`, `src/navi/evolution.py`, `tests/test_trust_evolution.py` |
| [x] | A09 | P1 | memory | Make memory schema and recall policy declared/evolvable. | Memory types, priorities, expiry, contradiction handling, and recall scoring can evolve with evidence. | `src/navi/memory.py`, `src/navi/evolution.py`, `tests/test_active_memory.py` |
| [x] | A10 | P1 | memory safety | Treat run execution logs as untrusted input during memory extraction. | Logs cannot inject durable memories or override consolidation rules. | `src/navi/memory.py`, `tests/test_skills_watches_security.py` |
| [x] | A11 | P1 | skills | Add skill version, hash, provenance, trust, scope, and evaluation metadata. | Skills become governed behavior artifacts, not static markdown blobs. | `src/navi/skills.py`, `src/navi/evolution.py`, `tests/test_skills_watches_security.py` |
| [x] | A12 | P1 | connector surfaces | Move approval and connector reply phrasing into connector/surface affordance specs. | Core returns structured facts; surfaces decide locale, commands, and reply format. | `src/navi/engine.py`, `src/navi/*/specs/connector.yaml`, `src/navi/api.py` |
| [x] | A13 | P1 | tests | Replace narrow hardcoding guardrails with architecture-level anti-workflow tests. | Tests fail when behavior strategy moves back into Python instead of manifests/policies. | `tests/test_no_behavior_hardcoding.py`, `tests/test_agentic_guardrails.py` |
| [x] | A14 | P2 | evals | Bind evolution proposals to evidence and eval cases. | Applied changes include measurable expected outcomes and post-apply evaluation results. | `navi.evals`, `navi.evolution`, task eval dataset |
| [x] | A15 | P0 | trace evaluation | Add full-flow turn tracing and trace-level evaluation for optimization attribution. | Prompt, tool, memory, skill, provider, or policy optimizations can be chosen from recorded evidence instead of guesses. | `src/navi/trace.py`, `src/navi/engine.py`, `tests/test_engine.py` |
| [x] | A16 | P0 | coverage | Raise test and eval coverage for new agentic surfaces toward complete coverage. | Every exposed planner tool has an eval case; trace, evolution, API, and CLI paths have direct regression coverage. | eval loader, eval dataset, regression tests |
| [x] | A17 | P0 | compatibility removal | Remove historical compatibility paths and legacy combined task capability. | Current code starts from the declared agentic schema only: no old DB migration, no old Python support shim, no combined task workflow tool. | task store, memory store, action manifest, tests |

## Second-Round Agentic Gaps

These items came from real connector/watch usage after the first architecture pass. They focus on whether Navi behaves agentically in customer journeys, not only whether individual modules are shaped correctly.

| Status | ID | Priority | Area | Optimization | Target Outcome | Primary Files |
| --- | --- | --- | --- | --- | --- | --- |
| [x] | A18 | P0 | completion verifier | Guard turn completion against pending tasks and partial cleanup. | The agent cannot report completion when a recorded task is still pending/prepared or failed-task cleanup left remaining records. | `src/navi/engine.py`, `src/navi/capabilities.py`, `tests/test_engine.py` |
| [x] | A19 | P0 | watch execution | Run scheduled watches through the same actuator evidence path as tasks. | Watch output is backed by capability evidence and execution logs instead of model-proposed evidence. | `src/navi/execution.py`, `tests/test_execution_protocol.py` |
| [x] | A20 | P0 | customer journey coverage | Add connector-level customer journeys for task cleanup and scheduled watch delivery. | Tests exercise IM entry, session state, task/watch DB, daemon background work, message send, execution logs, and verifier evidence together. | `tests/test_customer_journeys.py` |
| [x] | A21 | P1 | trace attribution | Teach trace evaluation to classify completion-verifier failures and false-completion risk. | Trace evidence points to goal/completion policy gaps instead of only broad runtime/tool categories. | `src/navi/trace.py`, `tests/test_trace.py` |
| [x] | A22 | P0 | goal lifecycle | Add a first-class goal run that can continue across turns/background ticks until verified complete, blocked, or awaiting approval. | Navi can pursue user goals beyond a single message loop with durable state, progress evidence, and explicit stop conditions. | `src/navi/goals.py`, engine/daemon lifecycle updates, API/CLI observability, tests |
| [x] | A23 | P1 | recovery planner | Make failed verifier outcomes produce explicit recovery choices such as retry, alternate capability, ask user, or rollback proposal. | Failure handling becomes an observable agent decision instead of a hand-coded local branch or one-off answer. | `src/navi/recovery.py`, engine trace recovery events, planner prompt, tests |
| [x] | A24 | P2 | multi-agent readiness | Define when planner/critic/executor split is needed and how sub-agent results become auditable evidence. | Multi-agent evolution is introduced only where parallel critique or specialization improves verified outcomes. | `src/navi/specs/agent_roles.yaml`, role contracts, trace role results, architecture docs |

## Frontier-Agent Safety Gaps

These items come from OpenAI and Anthropic frontier-agent safety materials reviewed on 2026-06-04. They focus on whether Navi can remain a trustworthy personal assistant as models and tools become more capable.

| Status | ID | Priority | Area | Optimization | Target Outcome | Primary Files |
| --- | --- | --- | --- | --- | --- | --- |
| [x] | F01 | P0 | prompt boundary | Mark observed facts as untrusted execution-environment content. | Tool result envelopes remain usable facts, but embedded content cannot become instructions. | `src/navi/prompt_os.py`, `src/navi/specs/syscall_planner.yaml`, `tests/test_runtime.py` |
| [x] | F02 | P0 | connector policy | Replace the remote connector allowlist with an inspectable connector tool policy object. | Remote exposure has permission ceiling, allowed tools, blocked capability classes, and audit facts. | `src/navi/connector_runtime.py`, `tests/test_connector_runtime.py` |
| [x] | F03 | P0 | safeguards | Move sensitive-context classification into declarative capability metadata. | Runtime surfaces risk facts without keyword-inferred product behavior. | `src/navi/safeguards.py`, `src/navi/specs/capability_safeguards.yaml`, `tests/test_tools.py` |
| [x] | F04 | P0 | trace attribution | Classify hook/safeguard blocks as safeguard-policy failures. | Trace evaluation points to the correct safety layer for root-cause work. | `src/navi/trace.py`, `tests/test_trace.py` |
| [x] | F05 | P0 | goal integrity | Make task objectives subordinate to user intent, constraints, approvals, and safeguards. | Goal conflicts, shutdown, replacement, and scope reduction cannot justify unsafe self-protective behavior. | `src/navi/specs/syscall_planner.yaml`, `tests/test_runtime.py` |
| [x] | F06 | P1 | context compaction | Add a durable compaction contract for long-running goals. | Summaries preserve intent, completed steps, pending approvals, unresolved questions, and safety constraints. | `src/navi/goals.py`, `src/navi/memory.py`, trace records |
| [x] | F07 | P1 | provenance | Add embedded-content provenance to tool results. | The planner and trace viewer can distinguish file, webpage, log, connector, subprocess, and model-generated content. | capability result schemas, `src/navi/capabilities.py` |
| [x] | F08 | P1 | memory influence | Expose memory influence records in responses and traces. | Users can see which memories affected a decision or action. | `src/navi/memory.py`, `src/navi/trace.py`, API/CLI |
| [x] | F09 | P1 | plugin/MCP policy | Require install-time permission manifests for plugin and MCP providers. | New providers cannot reach connectors or mutating tools before policy audit. | tool gateway, plugin registry |
| [x] | F10 | P1 | assistant status | Add a user-visible goal/safety status surface. | A user can inspect active goals, stop conditions, pending approvals, last evidence, memory influence, and safeguard pauses. | CLI/API, `src/navi/goals.py`, `src/navi/trace.py` |

## Dynamic Workflow Stage

| Status | ID | Priority | Area | Optimization | Target Outcome | Primary Files |
| --- | --- | --- | --- | --- | --- | --- |
| [x] | W01 | P0 | workflow store | Add durable dynamic workflow, step, and event state. | Orchestration survives turns and can be resumed or audited. | `src/navi/workflows.py` |
| [x] | W02 | P0 | workflow capabilities | Add `workflow.propose/approve/run/resume/verify/status`. | The planner can propose and inspect workflows, while execution stays governed by capabilities. | `src/navi/specs/action_tools.yaml`, `src/navi/capabilities.py` |
| [x] | W03 | P0 | workflow safety | Enforce approval, permission ceiling, allowed tools, dependency ordering, and no recursive workflow calls. | Dynamic workflows remain scaffolding, not executable script escape hatches. | `src/navi/capabilities.py`, `src/navi/specs/capability_safeguards.yaml` |
| [x] | W04 | P1 | surfaces | Expose workflows through CLI/API and remote-safe connector policy. | Users can create, approve, run, resume, verify, and inspect workflows; connectors can only propose/status by default. | `src/navi/cli.py`, `src/navi/api.py`, `src/navi/connector_runtime.py` |
| [x] | W05 | P1 | cost telemetry | Add workflow cost and token accounting. | Users can approve workflows with concrete cost telemetry instead of vague estimates. | workflow store, provider usage records |
| [x] | W06 | P1 | parallel execution | Add true concurrent step workers once shared-state race rules are mature. | Parallel audits can run faster without racing state mutation. | workflow runner, subagent runtime |
