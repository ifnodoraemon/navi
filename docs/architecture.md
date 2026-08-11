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

Runtime construction is the validation boundary. Chat, API, daemon, and
connector startup refuse to accept work when the active configuration has any
validation error. `navi config` and `navi doctor` load diagnostics without
constructing a model runtime, so operators can still inspect and repair an
invalid configuration.

Secrets are stored in the same owner-readable file so there is one inspectable
authority. `navi config` renders the effective structure with secret-bearing
fields redacted. Model routes, search providers, connector credentials, API
authentication, and MCP servers do not read process-environment overrides.

`search.providers` is an instance registry rather than a global provider
switch. Each instance declares an adapter kind, enabled state, endpoint or MCP
server reference, and adapter-specific credentials or defaults. The
`web.search` capability exposes enabled instance IDs in its schema and requires
the planner to select one on every call. Registry dispatch performs exactly one
adapter call and returns its facts; it does not inspect query keywords, choose
an instance, retry, switch providers, or merge results. New search backends
extend the adapter registry without adding product-specific tools or routing
branches.

Child agents reuse this exact loop. `agent.control(operation=...)` is the single
parent lifecycle surface; `agent.report` is separate because it is a child-only
terminal protocol with different authority. Child policy is the intersection of
system, parent, and caller envelopes, and only the parent remains user-facing.
The durable Goal store owns maximum-active-child admission so concurrent
drivers share one atomic reservation boundary.

Recurring schedules are mutable templates, not implicit natural-language
dedupe. A planner reads them through `goal.state`, updates an existing template
through `goal.update(goal_id=...)`, and uses `goal.open` only for new schedules
or explicitly independent duplicate schedules. The scheduled view projects a
bounded recent occurrence ledger, including the connector receipt or typed
delivery rejection, so diagnosis does not depend on guessing a child Goal id.
Failed occurrences remain control-plane history. Without a checker-accepted
result body, they are excluded from semantic prior-result authority.

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

Capability registration carries an explicit runtime-availability observation.
An unmet prerequisite keeps the tool out of the planner and callable registry;
`tools.list` retains its name, requirement, and typed reason under unavailable
facts, while diagnostics reports the concrete missing dependency. Availability
is therefore runtime-owned without inventing a semantic fallback for the model.

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
chooses exactly one capability per planning pass but cannot bypass its schema or
policy envelope. The Planner response schema and parser both enforce a one-item
syscall array. Extra candidates become one typed replanning fact; the executor
never selects among them.

The executor invokes the selected capability through the same policy envelope.
Mutating capabilities are subject to approval, workspace, hook, and side-effect
gates. Their audit row is reserved before the effect; an unavailable audit
boundary blocks execution and an unrecordable completion becomes an uncertain
outcome. A direct approved effect links its completed receipt to the approval Run
so startup can settle a crash-interrupted control lifecycle without matching
redacted values against secret digests. Staged external effects are committed only after acceptance or
compensated on rejection. Capability inputs and outputs pass through one closed
JSON Schema validator, and failures expose typed reason and retryability facts.

The semantic checker evaluates objective evidence independently from planner
reasoning and returns only a verdict plus a non-authoritative judgment summary.
That summary must preserve exact capability field labels and values and cannot
become a fact source for later planning; the Planner reads names, numbers, units,
times, statuses, and sources from the original capability facts. A LoopSpec may use
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
The public StateGraph driver advances planning passes iteratively. It never
recursively re-enters the graph, so an allowed replan does not consume the Python
call stack. `paused` and `waiting_approval` are persisted suspension states, not
terminal outcomes; final states are `converged`, `blocked`, `failed`, `cancelled`,
`superseded`, `conflicted`, or `timed_out`. Loop kinds share this lifecycle contract while declaring separate
bounded retry budgets for turn, control, scheduled, and durable-goal work.
Evidence authority is domain-scoped. Navi Goal/approval reads cannot establish
an external application's task or approval state, process-table observations
cannot establish task activity, and web search establishes retrieved,
source-attributed reports rather than universal truth. A later disclaimer does
not repair an earlier claim that exceeded those boundaries.
For connector and interactive work, accepted tool facts enter a response phase
when `respond` is allowed: the planner sees that verified work is complete,
authors the surface result, and the checker evaluates that candidate before it
becomes `responded_message`. A `respond` call is candidate copy rather than
independent evidence for its own claims. Approval continuation lifts that
checker-accepted original response back to the approval surface, preventing an
approval acknowledgement from replacing the requested result.
The checker input carries a typed pre-transport evaluation contract. Semantic
acceptance covers objective fit, grounding, and contradiction checks only;
connector transport and receipts stay outside that authority and are evaluated
by the durable outbox boundary. The checker projection omits the necessarily
receipt-free pre-acceptance delivery state: user-facing communication
obligations are satisfied at this stage by grounded candidate copy, while a
later outbox receipt independently establishes external delivery. This prevents
semantic acceptance and transport from depending circularly on one another.

A failed capability, connector, structured model response, or non-transient
external-provider call is recorded without an automatic repeat, provider switch,
argument rewrite, or degraded substitute. A malformed structured model response
is represented separately from provider transport and may enter the ordinary
model-owned replan budget. A typed transient Planner/Checker transport failure
crosses at most one persisted retry gate: the daemon resumes the original graph
node after its delay, records the role-scoped retry count, clears the gate after
that role recovers, and terminates on same-role exhaustion. An
Evaluate retry restores the persisted executor result and candidate response, so
no capability or effect is replayed. A foreground retry pause does not invoke
the fact responder as a hidden second model route; the inbound user message
persists without an empty assistant message and an eventual accepted connector
result uses the ordinary durable outbox. Lease recovery after a crashed owner
and database transaction
conflict handling remain deterministic control-plane coordination, not semantic
recovery choices. The generated connector and API systemd units use
`Restart=on-failure` and a least-privilege process boundary: no new privileges,
private temporary and device namespaces, a read-only system and home, an empty
capability set, and explicit writable project/Navi-home paths. The assistant
process is supervised with a 90-second event-loop watchdog and restarts after
process or watchdog failure. Restart recovery only resumes persisted
control-plane state; it
does not repeat model work or invent a semantic recovery choice. Connector
heartbeat freshness remains a separate health fact so an active PID cannot make
a stale polling loop appear healthy.

Provider adapters own protocol decoding. Each model route declares
`response_transport: json|sse`; the adapter performs exactly that protocol and
does not negotiate a fallback from response contents. JSON mode admits only an
object body. SSE mode admits only JSON object data events, assembles their
content and usage facts, and requires a terminal `DONE` event. Protocol
corruption is raised as `ProviderResponseError`, using only status, media type,
byte counts, and canonical structural facts; response bodies and non-canonical
provider values never cross this boundary. This keeps provider failures typed
and prevents a decoder detail from surfacing as a StateGraph implementation
failure.

Optional provider request extensions enter through the model route's declared
`request_options`. The adapter merges only non-conflicting JSON fields and fails
closed if an option attempts to replace a runtime-owned protocol field. This
supports provider features such as template controls without model-name checks,
hidden defaults, or a second request.
Daemon and StateGraph lease owners include the owning PID. Reconciliation
releases a future lease early only when that declared process is observably
absent; unknown or old owner formats fail closed until expiry. The queue also
claims stale, unowned foreground checkpoints after a bounded grace period.
Recovered connector turns persist accepted output through the ordinary durable
outbox, so restart recovery cannot bypass receipt-based completion.

Prompt assembly is an inspectable interface, not scattered inline runtime text.
Stable prompt specifications live in `src/navi/specs_data.py`; assembly,
rendering, and manifest generation live in `src/navi/prompt_os.py`. Runtime
modules pass bounded facts into prompt OS assemblers, and tests or traces should
inspect prompt manifests and digests rather than parse rendered prose.
The planner capability manifest is a stable system-prefix block placed before
mutable conversation and runtime facts so provider prefix caching can reuse it.
Its input schemas retain validation structure but omit nested field descriptions
already covered by the capability description; the registry's full schemas
remain authoritative for executor validation.
`navi prompts inspect planner --json-output` is the current inspection surface.
The semantic checker receives one evidence-authority contract rather than
task-type branches. Memory consolidation combines its evolvable task layer with
the same stable Prompt OS boundary and treats transcript data as untrusted input.
Foreground turns retain bounded transcript continuity. Detached background
loops omit ambient session transcripts unless their task context explicitly
grants that history authority; this prevents old assistant candidates from
competing with current capability facts. Planner attempt state retains a bounded
typed fact projection, with `respond` marked candidate-only, so recovery can use
an earlier observation after a later capability overwrites `last_capability`.
Task lineage, authoritative prior results, and delivery state are projected once
through the dedicated Planner task-context input. Their durable Goal metadata
remains intact, while duplicate recurrence copies are removed from the model
projection and ambient current-state record arrays are sampled to a fixed bound
with full counts preserved.
The final Prompt OS projection uses enough structural depth to preserve nested
rows inside that already bounded state, avoiding a second projection that erases
leaf facts.
The semantic checker receives the bounded foreground transcript through a
separate `semantic_context_only` contract. It may resolve a referent or elliptical
turn such as a confirmation, but cannot use conversation text to prove execution,
completion, effects, or transport. Its evaluation contract explicitly distinguishes
current candidate copy from capability-evidence-only evaluation, so an assistant
message from a previous turn cannot be mistaken for the current result. A passing
fact-only check enters the model-authored response phase.
`context.search` keeps ordinary conversation at `conversation_log` trust. It
promotes only an exact checker-accepted LoopRun response and projects transport
receipt state separately, so follow-up questions can retrieve a verified result
without laundering an earlier assistant claim into runtime truth.

Connector delivery is a two-boundary operation: the capability records
`delivery_requested` and pauses the loop; the connector-neutral durable
outbox persists independently receipted text and attachment units before an
adapter submits them. Only the adapter's authoritative transport receipt may
converge the LoopRun and accept the Run and Goal. A bounded re-submit is allowed
only for the same durable item and idempotency key; it never replays model work,
changes payload, or selects another channel. Connector adapters own their API
mapping and failure classification, while the outbox owns queue state, recovery,
receipt persistence, and retry scheduling. Adapter-classified retry intervals
are bounded by the outbox, interactive responses may carry a higher transport
priority than proactive notifications, and recurring occurrences expire at the
next persisted occurrence deadline instead of accumulating a stale replay
backlog.

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
Approval-gate reconciliation uses the same rule. The gate id is extracted only
from the approval request directly owned by that LoopRun, never from nested
continuation facts about another task. Missing, expired, rejected, or mismatched
gates are cancelled through a saga that first settles the LoopRun and then
projects Run and Goal; an approved gate stranded by a crash is reopened.

LoopRun execution uses a versioned lease and compare-and-swap transitions, so a
foreground driver and daemon cannot both advance one loop. The active driver
heartbeats that lease throughout long model and capability calls and fails
closed if ownership cannot be renewed. Mutating capability
effects are reserved in `loop_effects` before invocation and replay only from a
completed result. Resource budgets live in `resource_ledger.db`; standard model
providers reconcile reserved cost/tokens with actual usage, while custom
providers use conservative declared phase estimates.
A Goal stores one permission envelope: every statically permitted capability
must fit inside its ceiling at both create and update time. Dynamic-permission
capabilities remain selectable, but their concrete calls are classified and
gated against that same ceiling before execution.
Lease-health projection counts both expired leases and stale unowned active
loops, preventing a detached checkpoint from appearing healthy merely because
its owner field is empty.

`process_sandbox.py` is the shared local-process isolation boundary used by
`shell.run`, browser artifacts, command verification, and proactive Git reads.
It uses fail-closed Bubblewrap, a sanitized environment, only the governed
workspace plus required runtime paths, and a separate network namespace unless
the declared operation has network authority. Durable Goals retain their logical
workspace for authorization and audit while effects and command verifiers are
translated into the active shadow workspace before merge. Shadow creation,
merge, and discard remain loop-kernel operations rather than planner-callable
tools addressed by arbitrary run identifiers.
For argv classified as read-only process inspection, Bubblewrap keeps the
filesystem, environment, network, and session restrictions but binds host
`/proc` read-only. The result records `host_process_table` scope and explicitly
limits its semantics to process presence and sampled state, avoiding both a
private-PID false positive and an unsupported claim that a task is making
progress.
Proactive project detectors use the same fact boundary. They publish structured
Git status, TCP-connect samples, or bounded redacted log appends with explicit
`establishes` and `does_not_establish` domains. They do not author notification
copy or classify free-form log text as an error; the notification model decides
whether the observed event warrants surfacing.

Trace events are append-oriented audit evidence. Materialized Run, Goal, and
LoopRun records own active lifecycle state. Foreground completion and every
background LoopRun processing pass
materialize the latest rule evaluation for that trace, including scheduled
failure and no-progress outcomes. Duplicate-effect rules consume the executor's
call-level mutability fact rather than inferring effects from domain transition
labels, so repeated observations cannot corrupt reliability metrics. Trace deletion is an API-only
capability with an explicit single/all scope and post-delete read-back facts;
the API does not mutate the trace store around the capability boundary.
The Goal-to-StateGraph boundary derives a missing trace id from the persisted
Goal or governed Run, so planner, capability, checker, notification, and
delivery spans share one root. Trace views also correlate older partial traces
through Run to Goal and LoopRun and project the durable terminal state over an
otherwise successful individual model-call span.
When a later Planner attempt recovers an earlier call or parse error, trace
evaluation classifies the result as degraded recovery rather than runtime
failure; an unrecovered Planner error remains a failure.

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
exhausted jobs to a visible dead-letter state. Queue transitions and schema-
migration snapshots are append-only job events. `memory.jobs` exposes those facts
only on the trusted local control surface; `memory.retry_jobs` accepts exact dead-
letter IDs and an operator reason, carries the prior error into the retry event,
and never performs automatic retries. Retention reconstructs a missing job from
the run transcript before it can remove detail. Hybrid text/embedding recall can
discover candidates without FTS, and graph neighbors are ranked before embedding-
only candidates so graph recall cannot be starved. Retention removes expired
transient detail only after consolidation and durable external-wait settlement
while preserving terminal summaries. It cannot delete approval or LoopSpec state
from a resumable lifecycle.

`EvolutionTargetAdapterRegistry` is the authority for evolvable target types.
Prompt layers, skills, memory items, eval cases, and graph nodes have real readers
and writers. Run lifecycle records and inert spec files are not evolution targets.
Prompt adapters accept only declared layers loaded by `PromptLayerStore`.
The skill adapter validates the same Agent Skills-compatible contract used by
runtime discovery: a lowercase hyphenated name of at most 64 characters that
exactly matches its directory, a bounded description, non-empty instructions,
and Navi extension keys nested under the standard `metadata` map. Source, trust,
and scope are assigned from the actual installation boundary rather than package
claims. Legacy Navi top-level extension fields are rejected rather than silently translated.
`skills.list` exposes invalid excluded packages as typed facts. The `skills.view`
capability similarly returns a
selected instruction file in full or reports a typed size-limit failure, so the
model never acts on an undisclosed suffix.
Proposal creation reads the current target through its adapter, preserves the
candidate byte-for-byte, and returns hashes and lengths rather than target
payloads to model-facing capabilities. Immutable built-in evaluation contracts
bootstrap creation of the first managed eval case; managed cases remain
fingerprinted durable inputs to experiments, and every effectful proposal/apply
edge retains its approval boundary.
`EvolutionExperimentStore` persists candidate checks, eval-case fingerprints, and
activation windows. Only successful apply events are reversible; rollback restores
the exact pre-apply snapshot through the same adapter. `EvolutionEngine` is kept
in `evolution_engine.py` so the ledger and experiment stores do not call each
other through circular imports; regression rollback is supplied as an explicit
orchestration port.
`evolution.candidates` is the read-only Trace-to-Eval bridge. It clusters repeated
persisted failure evaluations by failure domain and evaluation rule, preserving
sample trace identities and time bounds. It deliberately does not select an
evolution target, write a candidate, create a proposal, approve, or apply. Those
semantic steps remain model-owned and enter the existing persisted experiment,
approval, activation-observation, and rollback chain.
Authenticated API and CLI surfaces record proposal-attributed activation
observations through `evolution.observe`; every observation must contain at least
one success or error outcome. Reaching the configured minimum observation count
with an error rate above the approved threshold invokes the persisted rollback
path rather than treating an empty or unrelated system event as canary evidence.
If the process stops after persisting `regressed` but before rollback completes,
startup maintenance resumes only that deterministic rollback; it does not write
a synthetic observation.

`MetricsProjector` derives values from durable stores rather than maintaining a
second source of truth; construction may initialize or migrate those store
schemas. Its snapshots feed `system.metrics`, `/v1/metrics`, `navi metrics`, and
doctor SLO checks. Empty invariant samples remain insufficient data. Evolution
observations enter only through proposal-attributed evidence; the daemon performs
rollback recovery but does not reinterpret unrelated task outcomes as canaries.

## Connector Boundary

Connector adapters own authentication, polling, message normalization, media
transport, and channel-local presentation. Ingress idempotency is a shared
boundary: `ConnectorIngressDeduplicator` treats a native transport message id
as the authoritative idempotency key, and falls back to a content key only
when the id is absent or marked with the `synthetic:` prefix by the adapter,
so a deliberate identical resend under a new native id is never dropped.
They publish the same
turn contract used by local surfaces. `ConnectorMessage` lives in the transport-
neutral `connector_contract.py`; adapters consume `ResponseReadyEvent` and never
reinterpret it as a string. An empty model response is recorded as a failed
delivery fact rather than replaced with connector-authored prose. An empty
response whose finalization facts show a pending durable provider-transport
retry is recorded as a deferred fact instead: the durable graph owns that
recovery, the eventual result uses the ordinary outbox, and no second model
role is invoked in the meantime. A single failing inbound update is recorded
and isolated; it cannot silently drop the remaining updates of the same poll
batch.
For a blocked or failed background task, the notification role receives a
bounded projection of persisted Goal and LoopRun diagnostics, including reason
codes, checker verdicts, and the last capability facts. The connector still
does not decide whether the event is noteworthy or author its own fallback text.
An adapter persists channel session material only at its native account-and-peer
scope, invalidates it from authoritative provider errors, and never emits it to
events or status output. Connector health separates ingress, reactive egress,
and proactive egress; a healthy poll loop cannot mask rejected outbound
notifications.
Ingress heartbeat age is evaluated when health is read. A stale ingress loop
degrades overall connector health, while partial or unknown egress prevents a
top-level healthy projection. After a fresh native peer session is observed, the
adapter may requeue only receipt-free items that failed specifically because the
old session expired and whose persisted delivery deadline is still in the
future. The original payload and idempotency key are preserved; expired and
unrelated failures remain terminal.
Provider error `-2` is not treated as a rate limit by code alone. Only explicit
frequency/rate-limit evidence receives that class; session expiry and other
transient connector rejection remain separate typed reasons with separate retry
intervals. Connector snapshots project instantaneous egress separately from
1-hour, 24-hour, and 7-day proactive-delivery reliability. A fresh successful
send cannot close an open rolling SLO incident until the measured window itself
recovers. The window query is channel-scoped; absent samples and read failures
remain explicit `insufficient_data` or `unknown` facts rather than healthy state.

Outbound files use the connector-neutral `ConnectorDelivery` contract. The
kernel validates the original file and emits one structured durable delivery
request; the adapter that implements durable delivery (Weixin today; email or
Feishu adapters in the future) maps each persistent item to its transport API
and records the real
receipt. Textual `MEDIA:` directives are not delivery evidence. For an accepted
background result, the Run and Goal remain unverified and non-terminal while
its outbox item is pending; only complete connector receipts or a terminal
connector rejection finalizes them. An interrupted `sending` claim is returned
to `pending` with the same idempotency key, not treated as a new semantic action.

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

## Verification Status

This document does not carry a hand-maintained claim that implementation and
runtime have zero discrepancies. Release closure requires current static checks,
full tests and eval validation, fault-path regression tests, live service and
database read-back, and connector receipt evidence where transport is in scope.
Historical SLO breaches remain historical facts; a repair is demonstrated by
new evidence and rolling-window recovery, never by deleting old samples.
