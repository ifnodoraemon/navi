# Navi Architecture

This document describes the current architecture and its required boundaries.
Code remains the runtime source of truth; discrepancies are listed explicitly
rather than describing an intended design as already implemented.

## System Shape

```text
CLI / API / Connectors
        |
        v
TurnController + CurrentStateBuilder
        |
        v
LoopControlService -> Run + Goal + LoopSpec + LoopRun
        |                  |
        |                  +-> depth-1 background child Goals
        |
        v
DurableStateGraphRunner
   | planner port
   | capability executor port
   | semantic checker / reflector ports
   v
CapabilityRegistry -> tools, actions, hooks, resource and workspace gates
        |
        v
SQLite stores + sagas/effect journal + metrics/SLO projection
        |
        v
personal resource adapters + connector delivery receipts
```

The unified loop is a protocol boundary. `loop_kind` distinguishes a turn,
control action, durable goal, or scheduled run without creating an unrelated
execution engine.

## Configuration Ownership

`.navi/config.yaml` is the single source of Navi runtime configuration. Its
top-level domains are `model`, `runtime`, `execution`, `api`, `search`,
`connectors`, and `mcp`. The loader applies declared defaults, validates typed
values and references, rejects unknown top-level domains, and rejects the old
`env`, `mcp.json`, and `api_key` files. `NAVI_HOME` is the sole bootstrap
environment variable because it selects the directory before the configuration
file can be loaded; it does not override values inside the file.

Secrets are stored in the same owner-readable file so there is one inspectable
authority. `navi config` renders the effective structure with secret-bearing
fields redacted. Model routes, search providers, connector credentials, API
authentication, and MCP servers do not read process-environment overrides.

Child agents reuse this exact loop. `agent.control(operation=...)` is the single
parent lifecycle surface; `agent.report` is separate because it is a child-only
terminal protocol with different authority. Child policy is the intersection of
system, parent, and caller envelopes, and only the parent remains user-facing.
The durable Goal store owns maximum-active-child admission so concurrent
drivers share one atomic reservation boundary.

Recurring schedules are mutable templates, not implicit natural-language
dedupe. A planner reads them through `goal.state`, updates an existing template
through `goal.update(goal_id=...)`, and uses `goal.open` only for new schedules
or explicitly independent duplicate schedules.

## Layer Ownership

| Layer | Primary modules | Responsibility |
|---|---|---|
| Ingress | `cli.py`, `api.py`, `connector_contract.py`, `connector_runtime.py` | Normalize external input and identity. |
| Turn control | `control_plane.py`, `turn_lifecycle.py`, `control.py` | Build turn context, current-state facts, trace, and final result. |
| Loop control | `loop_control_service.py`, `goal_state_graph.py` | Create or resume durable loop entities and bridge into the graph. |
| Loop kernel | `state_graph.py`, `loop.py`, `loop_contracts.py` | Plan, execute, check, pause, recover, and converge. |
| Capabilities | `capabilities.py`, `capabilities_types.py`, `actions/`, `core_tools/` | Declare, filter, validate, invoke, and audit callable operations. |
| Isolation | `process_sandbox.py`, `harness.py`, `workspaces.py`, `resource_gateway.py` | Bound commands, workspaces, locks, resources, and merge behavior. |
| State | `runs/`, `goals.py`, `loop_runs.py`, `memory/`, `trace.py`, `evolution.py`, `evolution_engine.py` | Persist lifecycle and audit evidence; apply evolution through a separate orchestration boundary. |
| Recovery | `lifecycle_saga.py`, `effect_journal.py`, `retention.py` | Recover cross-store projections, deduplicate effects, and compact expired transient detail. |
| Personal resources | `personal_resources.py`, `identity.py` | Provide scoped calendar, reminder, contact, draft-mail, attention, and explicit identity adapters. |
| Observability | `metrics.py`, `diagnostics.py` | Project durable events into SLOs, backlogs, and activation canary facts. |
| Adapters | `weixin/`, `telegram/`, `connector_registry.py` | Implement channel-specific transport outside the core loop. |

## Execution Policy Envelope

The architectural security boundary is the execution policy envelope, not an
individual registry instance. The envelope must be created at ingress and
preserved through every port and resume boundary. It contains identity,
current-state facts, capability allowlists, blocked classes, permission
ceiling, approval grants, workspace, trace, and governed run identifiers.

Inner layers may narrow this envelope through explicit caller restrictions.
They must never reconstruct a broader one from only a permission string. Source
identity is preserved for state, approval, audit, and delivery correlation, but
does not select a different capability catalog.

## Loop Responsibilities

The planner receives the objective, conversation context, current durable
facts, loop state, evidence, memory context, and the filtered tool manifest. It
chooses a capability but cannot bypass its schema or policy envelope.

The executor invokes the selected capability through the same policy envelope.
Mutating capabilities are subject to approval, workspace, hook, and side-effect
gates. Their audit row is reserved before the effect; an unavailable audit
boundary blocks execution and an unrecordable completion becomes an uncertain
outcome. Staged external effects are committed only after acceptance or
compensated on rejection. Capability inputs and outputs pass through one closed
JSON Schema validator, and failures expose typed reason and retryability facts.

The semantic checker evaluates objective evidence independently from planner
reasoning and returns only a verdict plus evidence summary. A LoopSpec may use
the deterministic objective-evidence tier when the capability contract itself
returns authoritative completion facts; durable semantic work uses the isolated
LLM checker. The capability's `deterministic_completion_authority` declaration
controls only that deterministic short circuit; it is removed from semantic
checker input so ordinary capability facts retain their actual provenance
instead of receiving a false pass/fail label. When no extra acceptance criteria
were declared, the checker judges the objective itself rather than a synthetic
criterion about its own verdict. The runtime enforces
attempt budgets, timeouts, safety stops, and no-progress bounds; while another
planning turn is allowed, the planner owns the semantic choice to gather facts,
change capability or arguments, clarify, or explain a blocker. A checker verdict
is evidence, not a runtime-selected next action. Trace proxies record model calls
and capability spans without changing their decisions. If a later attempt
converges, the LoopRun clears active recovery fields while the earlier rejection
remains available through attempt history and trace events.

A failed model, capability, connector, or external-provider call is recorded and
returned without an automatic repeat, provider switch, argument rewrite, or
degraded substitute. A later planner turn is a new model-owned decision, not a
runtime retry. Lease recovery after a crashed owner and database transaction
conflict handling remain deterministic control-plane coordination, not semantic
recovery choices. The systemd unit uses `Restart=no`; a failed assistant process
stays failed until an explicit operator or governed model action starts it again.

Prompt assembly is an inspectable interface, not scattered inline runtime text.
Stable prompt specifications live in `src/navi/specs_data.py`; assembly,
rendering, and manifest generation live in `src/navi/prompt_os.py`. Runtime
modules pass bounded facts into prompt OS assemblers, and tests or traces should
inspect prompt manifests and digests rather than parse rendered prose.
`navi prompts inspect planner --json-output` is the current inspection surface.
The semantic checker receives one evidence-authority contract rather than
task-type branches. Memory consolidation combines its evolvable task layer with
the same stable Prompt OS boundary and treats transcript data as untrusted input.

Connector delivery is a two-boundary operation: the capability records
`delivery_requested` and pauses the loop; only the connector's authoritative
transport receipt may converge the LoopRun and accept the Run and Goal.

## Persistence

SQLite is the local storage mechanism. Runs and approvals share `runs.db`;
goals, loop checkpoints, traces, memory, graph data, personal resources,
resource usage, evolution experiments, and connector state use specialized
stores.

Cross-store operations require a Unit of Work or an explicit saga. Creating a
Run, Goal, LoopSpec, and LoopRun is one logical operation even when the records
live in separate databases. Synchronous open failures compensate immediately;
startup recovery deterministically terminates stale partial opens after a grace
period. Terminal StateGraph projections use a persisted lifecycle saga and are
replayed until both Run and Goal match.

LoopRun execution uses a versioned lease and compare-and-swap transitions, so a
foreground driver and daemon cannot both advance one loop. Mutating capability
effects are reserved in `loop_effects` before invocation and replay only from a
completed result. Resource budgets live in `resource_ledger.db`; standard model
providers reconcile reserved cost/tokens with actual usage, while custom
providers use conservative declared phase estimates.

`process_sandbox.py` is the shared local-process isolation boundary used by
`shell.run`, browser artifacts, command verification, and proactive Git reads.
It uses fail-closed Bubblewrap, a sanitized environment, only the governed
workspace plus required runtime paths, and a separate network namespace unless
the declared operation has network authority. Durable Goals retain their logical
workspace for authorization and audit while effects and command verifiers are
translated into the active shadow workspace before merge. Shadow creation,
merge, and discard remain loop-kernel operations rather than planner-callable
tools addressed by arbitrary run identifiers.

Trace events are append-oriented audit evidence. Materialized Run, Goal, and
LoopRun records own active lifecycle state. Trace deletion is an API-only
capability with an explicit single/all scope and post-delete read-back facts;
the API does not mutate the trace store around the capability boundary.
The Goal-to-StateGraph boundary derives a missing trace id from the persisted
Goal or governed Run, so planner, capability, checker, notification, and
delivery spans share one root. Trace views also correlate older partial traces
through Run to Goal and LoopRun and project the durable terminal state over an
otherwise successful individual model-call span.

Scheduled Goal templates persist the real workspace rather than the ephemeral
shadow used by their registration turn. The registration capability resolves
that mapping from the workspace audit store. A materialization failure advances
the recurring template, records a Goal event and a failed capability trace, and
publishes structured notification facts, so one bad occurrence cannot spin the
background loop or vanish without evidence.

Durable memory items carry global, person, actor, session, or workspace scope. The
planner and responder receive only scopes derived from the current execution
identity. Person scope is created only through an explicit hashed identity link.
Conversation turns enqueue run-bound leased consolidation jobs; extracted items
remain proposed. Workers reclaim expired leases with bounded backoff and move
exhausted jobs to a visible dead-letter state. Retention reconstructs a missing
job from the run transcript before it can remove detail. Hybrid text/embedding
recall can discover candidates without FTS, and graph neighbors are ranked before
embedding-only candidates so graph recall cannot be starved. Retention removes
expired transient detail only after consolidation while preserving terminal summaries.

`EvolutionTargetAdapterRegistry` is the authority for evolvable target types.
Prompt layers, skills, memory items, eval cases, and graph nodes have real readers
and writers. Run lifecycle records and inert spec files are not evolution targets.
`EvolutionExperimentStore` persists candidate checks, eval-case fingerprints, and
activation windows. Only successful apply events are reversible; rollback restores
the exact pre-apply snapshot through the same adapter. `EvolutionEngine` is kept
in `evolution_engine.py` so the ledger and experiment stores do not call each
other through circular imports; regression rollback is supplied as an explicit
orchestration port.

`MetricsProjector` derives values from durable stores rather than maintaining a
second source of truth; construction may initialize or migrate those store
schemas. Its snapshots feed `system.metrics`, `/v1/metrics`, `navi metrics`, and
doctor SLO checks. Empty invariant samples remain insufficient data. Evolution
observations enter only through proposal-attributed evidence; the daemon performs
rollback recovery but does not reinterpret unrelated task outcomes as canaries.

## Connector Boundary

Connector adapters own authentication, polling, message normalization, media
transport, deduplication, and channel-local presentation. They publish the same
turn contract used by local surfaces. `ConnectorMessage` lives in the transport-
neutral `connector_contract.py`; adapters consume `ResponseReadyEvent` and never
reinterpret it as a string. An empty model response is recorded as a failed
delivery fact rather than replaced with connector-authored prose.
For a blocked or failed background task, the notification role receives a
bounded projection of persisted Goal and LoopRun diagnostics, including reason
codes, checker verdicts, and the last capability facts. The connector still
does not decide whether the event is noteworthy or author its own fallback text.

Outbound files use the connector-neutral `ConnectorDelivery` contract. The
kernel validates the original file and emits one structured synchronous
delivery request; the active adapter (Weixin today, email or Feishu adapters in
the future) sends it directly and records the real transport receipt. Textual
`MEDIA:` directives, connector outboxes, and deferred connector commits are not
delivery evidence and are not part of the real-time reply path.

Connector ingress uses the same capability catalog and default permission
ceiling as local CLI ingress. Sensitive operations do not execute merely because
they are visible to the planner: the shared risk assessment and durable approval
gate pause them before any effect. Connector authentication and sender policy
still determine who may enter the loop.

Configured MCP servers join the same registry through governed discovery and
call broker capabilities. Streamable HTTP and stdio are transport choices, not
separate permission models. The local `tool_permissions` map is both allowlist
and per-tool permission authority; server annotations cannot change it. HTTP
calls require network authority, while stdio calls require write authority and
receive a minimal environment plus explicit configuration instead of inheriting
the Navi process environment. Durable approval, audit, and redaction apply to
both transports.

Direct HTTP capabilities resolve and classify every address before the call,
bind that address set into approval arguments, and connect to a pinned address
while preserving the original Host and TLS identity. This keeps DNS rebinding
from changing the destination after policy evaluation.

## Known Current Deviations

No known discrepancy currently changes the contracts documented above. This is
not a claim of zero defects: end-to-end tests, live traces, SLO snapshots, and
runtime read-back remain required because isolated store tests do not prove the
complete control boundary.
