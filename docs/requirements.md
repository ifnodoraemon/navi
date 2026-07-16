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

## Delegation Contract

Delegation is ordinary governed Goal execution, not a second user-facing agent
stack. A parent may use `agent.control` operations to spawn, inspect, message,
cancel, and collect a depth-1 background child. A child receives an immutable
intersection of system, parent-Goal, and caller policy with explicit objective,
acceptance criteria, context facts, permission ceiling, workspace, timeout, and
resource budgets. No more than three children may be active for one parent.

Children cannot recursively delegate, contact the user, resolve approvals, use
connectors, or mutate the workspace. They return findings only through the
child-only terminal `agent.report` protocol. A report is a claim; completion
remains separate and requires the child LoopRun and checker evidence to
converge. Transient background resource pauses resume at their persisted node;
they must not be mislabeled or replayed as approval continuations.
`agent.control` exposes only depth-1 child records. `goal.state` exposes
actor-scoped top-level task, history, and recurring-schedule views. Read
results declare the scope for which an empty result is authoritative.

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

Tools execute or observe and return facts. Skills provide procedures and may
package scripts, templates, or assets, but execution still passes through
governed capabilities. Plugins provide installed code and integrations. Hooks
observe or deterministically gate lifecycle events. These extension types must
not silently assume each other's authority or make product-semantic choices for
the model.

CLI, API, and connector ingress use the same capability catalog. Source identity
scopes durable state, approvals, audit, and reply delivery; it does not implicitly
narrow or broaden capability visibility. Explicit caller restrictions and the
permission ceiling must survive every Goal, StateGraph, resume, and background
boundary, while sensitive effects always require a matching durable approval.

## State And Persistence

Approval is durable state. A chat message that expresses approval is not itself
an execution grant.

Run, Goal, and LoopRun creation and lifecycle changes must be atomic or use an
explicit, recoverable saga. Partial failure must not leave an apparently active
or approved orphan entity.

Memory must be typed, scoped, provenance-bearing, revocable, and conflict
visible. Recall, revocation, conflict reads, and activation records must stay
inside global, actor, session, and workspace visibility scopes. User-facing
actors cannot write global memory. Assistant conversation text and run result
summaries are non-authoritative candidates, not durable facts. Preferences
learned from prior approvals may inform explanations but must not expand
permissions.

Trace is audit evidence, not the authoritative runtime state. Secrets and
sensitive payloads must be redacted before persistence.

Recurring Goal templates must persist a durable real workspace, never a
turn-scoped shadow workspace. Registration resolves managed paths from workspace
audit state. Occurrence-creation failures must advance the template out of the
due queue, record a Goal event and failure trace, and expose structured facts to
the connector notification boundary instead of retrying in a tight loop or
disappearing silently.

## Surfaces

The supported product surfaces are:

- `navi` CLI for chat, diagnostics, capabilities, goals, approvals, traces,
  memory, connectors, and service operation;
- authenticated local FastAPI endpoints under `/v1`;
- connector adapters discovered through the connector registry;
- the trace web UI as an inspection surface, not an execution authority.

Core capabilities must remain usable and testable without a browser UI or a
specific connector.

MCP tool calls must pass through the same capability registry, approval, audit,
and redaction boundaries as core and connector tools. Server annotations must
not grant or lower permissions.

Web search must use supported structured providers, surface provider and
configuration facts, and label whether the same failed provider call is
retryable. The loop may still let the model choose a different capability,
arguments, clarification, or blocker response within its remaining budget.

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
