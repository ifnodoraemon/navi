# Navi Product Requirements

This document is Navi's current product contract. It defines required behavior
and boundaries. Implementation plans, incident reports, and completed repair
notes do not belong here.

## Product Boundary

Navi is a local-first, governed personal agent OS. It gives a model current
facts and declared capabilities, lets the model make semantic decisions, and
uses deterministic runtime controls to bound execution.

Navi must provide:

- a CLI-first local experience, with API and connector surfaces using the same
  runtime contracts;
- model-owned planning from current facts and declared capabilities;
- model-owned clarification, semantic recovery, and user-facing synthesis;
- durable goals, runs, approvals, checkpoints, memory, and trace evidence when
  work needs persistence;
- explicit permission ceilings, source policies, approval grants, workspace
  isolation, and side-effect controls;
- connector-neutral core behavior;
- inspectable failures instead of invented success or hidden fallback text.

Navi is not:

- a product-specific workflow engine;
- an autonomous authority for granting itself permissions;
- a system that silently rewrites, merges, restarts, or deploys itself;
- a compatibility layer for obsolete internal contracts;
- a collection of channel-specific prompt rules.

## Decision Ownership

The model owns semantic decisions:

- interpreting the user's objective;
- choosing among capabilities visible in the current policy envelope;
- deciding whether more facts, clarification, or another attempt are needed;
- synthesizing user-facing responses from verified facts.

The runtime owns deterministic enforcement:

- capability schemas and availability;
- execution-context capability restrictions and blocked capability classes;
- permission ceilings and scoped approval grants;
- path, workspace, timeout, concurrency, and resource boundaries;
- checkpoint, merge, compensation, and audit requirements;
- durable lifecycle transitions.

A capability with unmet declared runtime prerequisites must be absent from the
planner manifest. The capability catalog keeps structured unavailable facts and
diagnostics identify the missing prerequisite, so absence is observable without
offering the model a call that is known to fail.

Each model role resolves to one declared provider. The runtime invokes that
provider once in an execution pass and must not recurse, switch providers,
rewrite arguments, or synthesize a substitute after failure. Structured-output
and semantic failures propagate to the loop. A malformed structured model
response is a typed Planner/Checker contract fact: it may enter the ordinary
model-owned replan budget, but it is never a provider-transport retry. A typed
transient provider transport failure may create one persisted retry gate with a
bounded delay and resume the same Planner or Checker node once. The gate records
model role, resume node, retry count, and maximum; exhaustion is terminal.
An HTTP success status is not a successful provider result until it satisfies
the configured response transport. `json` requires a JSON object body; `sse`
requires JSON object data events and a terminal `DONE` event. The transport is
declared per model route and must never be inferred from a malformed response or
retried under another mode. Malformed envelopes become bounded
`ProviderResponseError` facts at the adapter boundary; raw response bodies and
non-canonical response values must not enter exceptions, traces, or user-facing
diagnostics.
Provider-specific request extensions are explicit `request_options` on that
model route, not model-name branches in runtime code. They must be JSON data and
cannot override runtime-owned protocol fields such as model, messages, output
schema, tools, token limit, or response transport.
Retry accounting is role-scoped, and a recovered role's stale gate is archived
and cleared before a different role can fail.
Checker recovery must reuse the persisted capability result and candidate
response without reexecuting the capability. A foreground retry pause persists
the inbound user message, writes no empty assistant message, and suppresses the
otherwise automatic fact responder; a later accepted connector result enters
the ordinary outbox.

A persisted connector delivery item with a stable idempotency key is the other
transport-level recovery boundary: its connector-neutral outbox may make a
bounded retry of the unchanged item after an adapter-classified transient
failure. This does not repeat a model or capability call, alter content, switch
channels, or replace the required connector receipt. The adapter supplies typed
provider codes and a minimum retry interval; the outbox enforces the item attempt
bound and delivery deadline. A recurring occurrence that reaches its next
persisted schedule deadline without a receipt expires instead of joining a later
replay burst.
When authoritative inbound activity refreshes a connector session, only an
unchanged, receipt-free delivery that failed for session expiry and remains
inside its persisted deadline may be requeued under its original idempotency
key. Other failures require an explicit retry decision.

Active-path state and retrieval failures must also propagate. A corrupt durable
cursor, dedup ledger, FTS index, embedding result, or semantic graph must not be
silently replaced with empty state or a weaker retrieval strategy. Missing
optional state remains a valid observed absence.

An LLM may produce risk facts or explanations. It must never be the authority
that converts a sensitive operation from approval-required to allowed.

## Turn And Loop Contract

Every ingress surface must create one immutable execution policy envelope that
survives planning, execution, pause, resume, and background processing. It must
contain at least:

- source, peer, sender, session, workspace, and trace identity;
- current durable state facts;
- allowed capabilities and blocked capability classes;
- permission ceiling and any scoped approval grant;
- execution context and governed run identity.

All request shapes use the same loop protocol, but not necessarily the same
cost or persistence profile. A conversational answer, a state query, and a
long-running goal may share planning and policy contracts while using different
checkpoint, verification, and storage requirements.

Each LoopSpec declares an execution profile. `turn` uses transient audit with a
bounded retention window, `control` and `scheduled` use objective evidence when
the called capability returns authoritative completion facts, and durable goals
retain semantic checking. The runtime must not call an LLM checker when the
declared objective-evidence contract is already deterministically satisfied.
That capability declaration is only deterministic completion authority; it is
not a quality label for the capability's other returned facts and must not be
shown to the semantic checker as one. Extra acceptance criteria may be empty,
in which case the objective remains the semantic contract. The runtime must not
invent a self-referential criterion such as requiring the verifier to accept
the verification ladder.

The loop must:

- plan only from the capabilities in its policy envelope;
- execute through the same or stricter envelope;
- record capability results as structured facts;
- preserve approval and constraint state across context compaction;
- pause before unapproved sensitive effects;
- require objective evidence before declaring completion;
- expose failure, blocking, and no-progress facts without fabricating a result.
- return checker rejection and capability-failure facts to the model while a
  bounded replanning opportunity remains; the runtime must not choose the
  semantic recovery route.

For a connector or interactive turn, checker acceptance of raw capability facts
is not yet a user-visible result. When `respond` is inside the governed
capability envelope, the model must author a grounded response and that response
must pass the checker before the LoopRun converges. The runtime may expose that
the verified work is complete and a surface response remains; it must not repeat
the completed effect or let an unchecked finalizer turn a tool observation into
a completion claim.
The semantic checker operates before external transport under an explicit
evaluation contract. It judges objective coverage, grounding, and
contradictions; it never requires or infers a connector receipt. A separate
outbox/receipt protocol owns transport completion. Its input omits
pre-acceptance delivery state, because no receipt can exist before semantic
acceptance authorizes the outbox. Within that scope, communication obligations
are evaluated from grounded candidate copy; only the later receipt establishes
external delivery.

When a later attempt converges, prior rejection and recovery facts remain in
attempt history and trace evidence but must not remain as the current LoopRun
reason code or active recovery state.
Planner recovery receives a bounded typed projection of prior capability facts,
not only their field names, so a later response can use an earlier authoritative
observation after another capability has run. `respond` output remains labeled
candidate-only in that projection. The final Planner assembly must preserve
ordinary nested rows inside an already bounded projection rather than truncating
them again at a shallower generic depth. Detached background execution excludes
ambient session transcripts unless its task context explicitly declares them
authoritative; lineage facts and governed memory remain available. For foreground
turns, the isolated semantic checker receives the same bounded transcript as
semantic context so it can resolve referents and elliptical replies. That context
never establishes capability facts, effects, completion, or delivery, and
assistant messages remain explicitly non-authoritative candidates. The current
candidate exists only when the current capability result declares candidate copy;
an assistant message in transcript history cannot be substituted for it. Before
candidate copy exists, the checker judges current capability-evidence coverage;
a pass enters the governed response phase rather than failing for missing prose.

## Delegation Contract

Delegation is ordinary governed Goal execution, not a second user-facing agent
stack. A parent may use `agent.control` operations to spawn, inspect, message,
cancel, and collect a depth-1 background child. A child receives an immutable
intersection of system, parent-Goal, and caller policy with explicit objective,
acceptance criteria, context facts, permission ceiling, workspace, timeout, and
resource budgets. No more than three children may be active for one parent.
Admission to that active-child limit is reserved by the durable Goal store in a
single transaction so concurrent API and daemon processes cannot both pass a
stale sequential count.

Children cannot recursively delegate, contact the user, resolve approvals, use
connectors, or mutate the workspace. They return findings only through the
child-only terminal `agent.report` protocol. A report is a claim; completion
remains separate and requires the child LoopRun and checker evidence to
converge. Transient background resource pauses and typed provider transport
pauses resume at their persisted node; they must not be mislabeled or replayed
as approval continuations. Provider retry gates are eligible for foreground
recovery because connector ingress may own the original turn; accepted output
still requires the same outbox and receipt path.
`agent.control` exposes only depth-1 child records. `goal.state` exposes
actor-scoped top-level task, history, recurring-schedule, and scheduled-
occurrence views. A recurring template read includes a bounded set of recent
occurrences and their authoritative delivery status, attempts, typed failure
reason, persisted Run/Loop diagnostics, and trace/run identities without
embedding the delivered body. Read results declare the scope for which an empty
result is authoritative. `goal.state` is authoritative only for Navi's control
plane and must explicitly exclude external application, agent-process, and
external approval state.
Recurring schedule changes use `goal.update` against an explicit `goal_id`;
`goal.open` creates a new schedule and refuses same-actor same-cron duplicates
unless the caller explicitly declares an independent duplicate schedule.

Planner and checker progress claims are governed by `task_context`, not by
hardcoded task types, keywords, or connector names. A loop may declare a
lineage, sequence number, progress authority, and authoritative prior items.
Failed or blocked occurrences may remain in the control ledger but must not be
projected as authoritative prior semantic results.
Ambient actor/workspace history is background only unless the task context
explicitly declares it authoritative for the current task.
Durable task and recurrence records retain their complete authoritative data,
but the Planner receives one bounded task-context projection. Prior result text,
delivery state, and lineage must not be duplicated through Goal metadata,
trigger facts, and current-state records in the same model call. Ambient record
samples are bounded independently while their total counts remain visible.

## Capability Contract

Capabilities are stable external contracts. Each capability declares:

- name, source, capability class, execution contexts, and permission;
- JSON input and output schemas;
- whether it mutates state;
- call-dependent permission, risk, actor-context, runtime, and delegation policies;
- side-effect scope and stage/commit/compensate behavior when applicable.

Public input and output object schemas must explicitly declare their fields and
reject unknown root fields. One canonical validator owns conditional and
composite JSON Schema semantics; property declaration order must never create an
implicit required-field policy. Capability failures expose typed reason and
retryability facts rather than requiring callers to parse prose.
The Planner receives the capability manifest as a stable prefix before mutable
turn facts. Its model projection may remove duplicated schema prose, but it must
retain the input validation shape and cannot replace the executor's complete
authoritative schema.

Governance code executes those declared policies generically. It must not infer
permission, risk, context injection, runtime binding, or delegation eligibility
from capability names.

The capability surface must remain minimal. If an existing generic capability
can express a new operation through parameters or an input-schema extension,
Navi must evolve that contract instead of adding an operation-specific tool.
A new capability is justified only by a distinct authority boundary such as a
different permission, effect, approval, lifecycle, or execution environment;
unrelated authority boundaries must not be hidden behind one generic name.
Local process operations use `shell.run` unless another capability has a real
authority boundary. Directory listing, Git status, service inspection, system
facts, and test commands are argv choices, not separate tools. The runtime must
derive read, network, or write permission and approval requirements from the
concrete argv and fail unknown effects closed. If that derived permission is
higher than the model-declared permission, a sensitive call may proceed only
after an exact durable approval for the derived permission and arguments; it
must never execute directly or bypass the immutable permission ceiling.
Local commands execute in a fail-closed OS sandbox with a sanitized environment:
only the governed workspace and explicitly required runtime paths are mounted,
and host credentials are not inherited. Logical paths used by a durable Goal
are translated into its active shadow workspace for both effects and command
verification, without changing the Goal's durable authorization scope.
Shadow create, merge, and discard are loop-kernel operations, not planner-callable
capabilities selected by arbitrary run identifiers.
Declared read-only process inspection argv use a read-only host `/proc` view
inside the remaining sandbox boundaries. Their output declares
`observation_scope=host_process_table`; a matching row proves only process
presence and sampled state, never task progress or completion by itself.
Web search uses one configured provider explicitly selected by the model for
each call; query text cannot switch providers. Its evidence contract
establishes retrieved URLs, snippets, and
source-reported claims, but not claim truth, source authority,
representativeness, or real-world outcomes. Material numbers and outcome claims
must retain source attribution or be described as unverified reports.
Account-usage reads likewise require an explicit model-selected provider ID.
Adapters preserve provider field names and scalar values as structured facts;
the runtime must not choose a default provider, invent window labels, format
credit values as currency, or normalize plan names for presentation. Their
snapshot contract does not establish future request acceptance, future usage,
provider availability, or billing state after the observation.

Opt-in proactive project detectors emit bounded observations rather than
runtime-authored summaries. Git status, sampled TCP connectivity, and appended
log bytes each declare their own evidence scope. In particular, an unstructured
log watcher must redact secrets and leave error classification, root-cause
analysis, service health, and notification wording to the model.

Tools execute or observe and return facts. Skills provide procedures and may
package scripts, templates, or assets, but execution still passes through
governed capabilities. Plugins provide installed code and integrations. Hooks
observe or deterministically gate lifecycle events. These extension types must
not silently assume each other's authority or make product-semantic choices for
the model.
When a selected skill instruction file is disclosed, the file is returned
complete or the capability returns an explicit typed resource-limit failure;
silent truncation cannot satisfy the skill contract. The model cannot choose a
read limit that changes this completeness guarantee.

First-class lifecycle entities need a complete governed surface inside the
caller policy envelope: scoped list, create/start, read, update, cancel/delete
or revoke, mutation read-back evidence, and terminal history. Compatibility
aliases for old internal contracts do not satisfy this requirement. Ambiguous
views should be split instead of silently folded into a generic field.

CLI, API, and connector ingress use the same capability catalog. Source identity
scopes durable state, approvals, audit, and reply delivery; it does not implicitly
narrow or broaden capability visibility. Explicit caller restrictions and the
permission ceiling must survive every Goal, StateGraph, resume, and background
boundary, while sensitive effects always require a matching durable approval.
Surface commands and endpoints that mutate a governed first-class entity must
invoke its capability and return the capability's read-back facts; they must not
write the backing store directly or append surface-specific audit side effects.

## State And Persistence

Approval is durable state. A chat message that expresses approval is not itself
an execution grant.
Approval behavior is declared capability policy. An explicit typed CLI or API
control command may avoid a redundant prompt only when the capability declares
that control-plane policy; model-planned and connector calls remain subject to
the normal durable approval gate. Authorization preflight runs before approval
creation and is repeated before the effect to prevent approval of an operation
the caller cannot perform.
Approval of an observation is not evidence about the observed entity. When the
approved continuation reaches its own checker-accepted response, the approval
surface returns that original task result as the primary reply rather than
stopping at “approval succeeded.”
An approval wait belongs to exactly one LoopRun and must name a durable approval
whose Run matches that LoopRun's governed Run. An approval-control turn may
report that the original task reached another gate, but must not copy that gate
into its own lifecycle. Expired, rejected, missing, or mismatched gates are
reconciled through a recoverable lifecycle saga; an approved gate left behind by
a crash is reopened at its persisted checkpoint.

Run, Goal, and LoopRun creation and lifecycle changes must be atomic or use an
explicit, recoverable saga. Partial failure must not leave an apparently active
or approved orphan entity.
A Goal's statically declared capabilities must each require no more permission
than that Goal's immutable permission ceiling; `goal.open` and `goal.update`
reject an invalid envelope before persistence. Capabilities with call-dependent
permission remain eligible for model selection, but every concrete call is
derived and gated against the same ceiling.

Only one execution driver may own an active LoopRun. Claims and transitions use
durable leases, versions, and compare-and-swap checks. Mutating capability calls
use a durable Effect Journal: completed calls replay their recorded result,
concurrent calls wait/fail closed, and uncertain outcomes require reconciliation
instead of blind retry. Model and capability budgets are accounted in a
process-safe ledger and reconciled with observed provider usage.
Present provider usage must contain canonical non-negative integer counts;
malformed counts are protocol failures, never synthetic zero-cost usage.
An active execution driver renews its lease while model or capability work is in
flight; losing renewal authority fails the driver closed before its next state
transition.
Process-owned execution leases must carry an inspectable process identity.
Startup and queue reconciliation may release a lease only after its declared
owner is observably unavailable or the lease has expired. A stale, unowned
foreground LoopRun may then resume from its persisted checkpoint; if it belongs
to a connector, any accepted result enters the same durable result outbox and
still requires an authoritative transport receipt.
Before a mutating capability effect begins, the audit store must reserve its
tool-call record. Reservation failure blocks the effect; completion failure
surfaces an uncertain audit outcome instead of reporting clean success.

Memory must be typed, scoped, provenance-bearing, revocable, and conflict
visible. Recall, revocation, conflict reads, and activation records must stay
inside global, person, actor, session, and workspace visibility scopes. Cross-
surface person scope requires an explicit approved identity link; aliases are
stored as fingerprints and conflicting identities are not implicitly merged.
Conversation consolidation uses a durable leased job queue, produces proposed
memory rather than self-approved facts, and hybrid recall must not depend on an
FTS seed. Consolidation is bound to one run transcript, reclaims expired leases,
and dead-letters a recorded processing failure without retrying it. Every queue
transition is append-only evidence. A trusted local operator may explicitly
requeue exact dead-letter job IDs with a reason only after repairing the cause;
the previous error remains in the retry event, and no broad or automatic retry
path exists. Missing jobs are reconstructed before retention. Expired transient
turns are compacted only after consolidation and after any external wait has
been durably cancelled; retention must never delete a gate or LoopSpec while its
Run, Goal, or LoopRun still claims to be resumable. Terminal lifecycle summaries
remain available for metrics. User-facing actors cannot write global memory.
Assistant conversation text and run result summaries are non-authoritative
candidates, not durable facts. Preferences learned from prior approvals may
inform explanations but must not expand permissions.
Context search promotes an assistant message to
`trust=checker_accepted_result` only when its exact body matches a converged
LoopRun response. A connector receipt is projected separately and is the only
authority for transport completion; ordinary `trust=conversation_log` text may
resolve a referent but cannot prove task state or completion.

Trace is audit evidence, not the authoritative runtime state. Secrets and
sensitive payloads must be redacted before persistence. An evaluation is
materialized after each background LoopRun processing pass as well as foreground
turn completion, so scheduled failures and no-progress stops are not absent from
SLO evidence. Exactly one latest evaluation is stored per trace, so rerunning
evaluation cannot inflate SLO samples. Duplicate-effect diagnostics use the
executor's call-level `mutates` fact; repeated read observations are not effects
even when their domain payload contains a lifecycle `state_transition`.
Goal execution inherits the governed Run trace identity when an ingress caller
does not provide a separate trace id. Trace projections correlate that identity
back to durable Goal and LoopRun records, and a successful model call must not
make a blocked or failed durable task appear successful.
An earlier Planner call or parse error followed by a later accepted result is a
degraded recovered trace, not a runtime failure. An unrecovered Planner error
remains a failure.
Checker verdicts and summaries are model judgments, not observation facts.
Their summaries must preserve exact capability labels and values, and no later
Planner or responder may use a checker paraphrase instead of conflicting raw
capability fields.

Calendar events, reminders, contacts, mail drafts, and attention policies share
a scoped personal-resource adapter contract with schema validation, optimistic
version checks, soft deletion, and mutation read-back. Mail drafts are local
records; no capability may claim delivery without an authoritative external
connector receipt.

Evolution proposals are allowed only for targets with a runtime Target Adapter.
The adapter, not proposal input, reads the authoritative baseline and validates
that the target is actually consumed by the runtime. Inert prompt-layer names
are rejected. Proposal and state capability facts expose lifecycle and
fingerprints rather than copying target payloads back into model context.
Skill candidates must pass the same load-bearing contract before experiment or
apply: valid YAML frontmatter with a name and description, a valid permission,
and non-empty instructions. A generic non-empty-text check alone cannot qualify
an invalid skill for activation.
Candidate evaluation cases, fingerprints, checks, approval evidence, applied
events, activation observations, and rollback facts are durable. Every proposal
declares evaluation cases and cannot apply unless its latest candidate experiment
passed. Human approval is bound to the exact apply arguments using keyed digests
for private values, so matching does not require their plaintext persistence.
Immutable runtime evaluation contracts provide the minimum schema/non-empty
checks needed to propose and test the first managed evaluation case; they do not
authorize apply or replace proposal-specific evaluation and human approval.
Activation evidence must be explicitly attributed to its proposal; unrelated
system outcomes are not canary evidence. Regression beyond the approved threshold
triggers rollback, and uncertain application state is an SLO breach.

Metrics and SLOs are projections of durable facts, not model judgments. At
minimum they cover lifecycle orphans/sagas, execution leases, uncertain effects,
resource release, memory jobs, task outcomes, trace outcomes, and evolution
activation safety, proactive connector delivery success, and overdue delivery
backlog. Empty samples are reported as insufficient data, never as
healthy. User-requested cancellation is reported separately and is not classified
as task execution failure in the success-rate denominator.
Execution-lease health includes stale unowned active loops, not only leases whose
expiry timestamp has passed.

The active assistant service must expose an event-loop watchdog to its external
process supervisor. Connector status reads must classify an overdue ingress
heartbeat as stale, and overall health must not report healthy while egress is
partial, unknown, or degraded. Supervisor restart is process recovery only;
durable leases, outbox idempotency, and connector receipts remain the authority
for work recovery and completion. Before each watchdog heartbeat, the resident
service must verify that its configured Python executable, runtime prefix, and
loaded Navi package remain present. A missing runtime is a deployment-integrity
failure: the process must stop feeding the watchdog and exit instead of remaining
an active zombie. This guard must not classify connector or provider outcomes.

Recurring Goal templates must persist a durable real workspace, never a
turn-scoped shadow workspace. Registration resolves managed paths from workspace
audit state. Occurrence-creation failures must advance the template out of the
due queue, record a Goal event and failure trace, and expose structured facts to
the connector notification boundary instead of retrying in a tight loop or
disappearing silently.

## Surfaces

The supported product surfaces are:

- `navi` CLI for chat, diagnostics, metrics/SLOs, capabilities, goals,
  approvals, traces, memory, connectors, and service operation;
- authenticated local FastAPI endpoints under `/v1`;
- connector adapters discovered through the connector registry;
- the trace web UI as an inspection surface, not an execution authority.

Core capabilities must remain usable and testable without a browser UI or a
specific connector.

MCP tool calls must pass through the same capability registry, approval, audit,
and redaction boundaries as core and connector tools. Server annotations must
not grant or lower permissions. MCP servers are configured only under
`mcp.servers` in `.navi/config.yaml`; enabled servers expose governed discovery
and call capabilities only. MCP prompts, resources, sampling, elicitation, and
server-driven permission changes are not enabled.
Each server's `tool_permissions` map is both the local allowlist and the
permission authority for individual tools. HTTP tools require at least network
permission; stdio tools require write permission and start with a minimal
environment plus only explicitly configured variables, never the full Navi
process environment.

Web search must use supported structured providers, surface provider and
configuration facts, and label whether the same failed provider call is
retryable. The loop may still let the model choose a different capability,
arguments, clarification, or blocker response within its remaining budget.
Provider instances are configured under `search.providers`; each has a
model-visible ID and an adapter kind. Supported adapter kinds are `searxng`,
`exa_mcp`, and the official `x_api`. Every request must name one enabled
provider ID explicitly. The runtime must not infer a provider from query text,
retry a failed call, switch providers, or fuse provider results. SearXNG must
surface upstream engine failures rather than representing an empty blocked
response as a successful search. Search HTTP adapters resolve once, reject
redirects, and connect to a pinned address. Loopback and private SearXNG targets
require an explicit per-provider `allow_private_network` opt-in; link-local,
multicast, reserved, and unspecified targets are always rejected. The X adapter
must remain disabled until its
Bearer Token is present, use the official API, and return post identity,
attribution, timestamps, pagination, metrics, and provider errors as bounded
facts. All Navi runtime configuration belongs in
`.navi/config.yaml`; `NAVI_HOME` is the only bootstrap environment variable and
only selects the directory containing that file. Process environment variables
must not override configuration values. `navi config`, `navi doctor`, and
`navi doctor --connectivity` are the inspection and live probe surfaces.
Runtime, API, daemon, and connector startup must fail before accepting work if
the active configuration has validation errors. Diagnostic surfaces must remain
usable without constructing the model runtime and must expose those errors as
facts.
Direct HTTP capabilities resolve every target before approval and invocation,
classify all resolved addresses, bind the approved address set into the call,
and connect to a pinned address while preserving the original Host and TLS
identity. A later DNS answer cannot redirect an already approved call.
General shell execution is not a raw-network escape hatch: direct HTTP clients
remain effectful and approval-gated, and execute without sandbox network
access. Governed HTTP and search capabilities own outbound request policy.

## Verification Contract

Required repository gates are:

- Python compilation and package build;
- Ruff with no errors;
- unit and integration tests;
- focused cross-boundary tests for policy-envelope preservation, approvals,
  pause/resume, side effects, and connector ingress;
- eval dataset validation;
- CLI import and command smoke tests;
- trace web UI build when its code changes;
- opt-in live-provider and live-connector checks when credentials exist.

Coverage must measure the control plane, capabilities, connectors, and stores.
A zero threshold or broad omission of those modules is not an acceptance gate.
