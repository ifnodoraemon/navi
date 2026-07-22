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

Each model role resolves to one declared provider. The runtime invokes that
provider once per model call and propagates provider, transport, empty-response,
and structured-output failures without retrying or switching providers. A later
planning turn may choose another attempt only from the surfaced failure facts.

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

When a later attempt converges, prior rejection and recovery facts remain in
attempt history and trace evidence but must not remain as the current LoopRun
reason code or active recovery state.

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
converge. Transient background resource pauses resume at their persisted node;
they must not be mislabeled or replayed as approval continuations.
`agent.control` exposes only depth-1 child records. `goal.state` exposes
actor-scoped top-level task, history, and recurring-schedule views. Read
results declare the scope for which an empty result is authoritative.
Recurring schedule changes use `goal.update` against an explicit `goal_id`;
`goal.open` creates a new schedule and refuses same-actor same-cron duplicates
unless the caller explicitly declares an independent duplicate schedule.

Planner and checker progress claims are governed by `task_context`, not by
hardcoded task types, keywords, or connector names. A loop may declare a
lineage, sequence number, progress authority, and authoritative prior items.
Ambient actor/workspace history is background only unless the task context
explicitly declares it authoritative for the current task.

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

Tools execute or observe and return facts. Skills provide procedures and may
package scripts, templates, or assets, but execution still passes through
governed capabilities. Plugins provide installed code and integrations. Hooks
observe or deterministically gate lifecycle events. These extension types must
not silently assume each other's authority or make product-semantic choices for
the model.

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

Run, Goal, and LoopRun creation and lifecycle changes must be atomic or use an
explicit, recoverable saga. Partial failure must not leave an apparently active
or approved orphan entity.

Only one execution driver may own an active LoopRun. Claims and transitions use
durable leases, versions, and compare-and-swap checks. Mutating capability calls
use a durable Effect Journal: completed calls replay their recorded result,
concurrent calls wait/fail closed, and uncertain outcomes require reconciliation
instead of blind retry. Model and capability budgets are accounted in a
process-safe ledger and reconciled with observed provider usage.
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
and dead-letters a recorded processing failure without retrying it. Missing jobs are
reconstructed before retention. Expired transient turns are compacted only after
consolidation, while terminal lifecycle summaries remain available for metrics. User-facing
actors cannot write global memory. Assistant conversation text and run result
summaries are non-authoritative candidates, not durable facts. Preferences
learned from prior approvals may inform explanations but must not expand
permissions.

Trace is audit evidence, not the authoritative runtime state. Secrets and
sensitive payloads must be redacted before persistence. One latest evaluation is
stored per trace so rerunning evaluation cannot inflate SLO samples.
Goal execution inherits the governed Run trace identity when an ingress caller
does not provide a separate trace id. Trace projections correlate that identity
back to durable Goal and LoopRun records, and a successful model call must not
make a blocked or failed durable task appear successful.

Calendar events, reminders, contacts, mail drafts, and attention policies share
a scoped personal-resource adapter contract with schema validation, optimistic
version checks, soft deletion, and mutation read-back. Mail drafts are local
records; no capability may claim delivery without an authoritative external
connector receipt.

Evolution proposals are allowed only for targets with a runtime Target Adapter.
Candidate evaluation cases, fingerprints, checks, approval evidence, applied
events, activation observations, and rollback facts are durable. Every proposal
declares evaluation cases and cannot apply unless its latest candidate experiment
passed. Human approval is bound to the exact apply arguments using keyed digests
for private values, so matching does not require their plaintext persistence.
Activation evidence must be explicitly attributed to its proposal; unrelated
system outcomes are not canary evidence. Regression beyond the approved threshold
triggers rollback, and uncertain application state is an SLO breach.

Metrics and SLOs are projections of durable facts, not model judgments. At
minimum they cover lifecycle orphans/sagas, execution leases, uncertain effects,
resource release, memory jobs, task outcomes, trace outcomes, and evolution
activation safety. Empty samples are reported as insufficient data, never as
healthy. User-requested cancellation is reported separately and is not classified
as task execution failure in the success-rate denominator.

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
Supported providers are `searxng` and `exa_mcp`. Each request uses exactly one
configured provider and one endpoint; the runtime must not retry a failed call
or switch providers. All Navi runtime configuration belongs in
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
