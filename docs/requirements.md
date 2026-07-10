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
- source allowlists and blocked capability classes;
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

## Capability Contract

Capabilities are stable external contracts. Each capability declares:

- name, source, capability class, execution contexts, and permission;
- JSON input and output schemas;
- whether it mutates state;
- side-effect scope and stage/commit/compensate behavior when applicable.

Tools execute or observe and return facts. Skills provide procedures. Plugins
provide installed code and integrations. Hooks observe or gate lifecycle
events. These extension types must not silently assume each other's authority.

Remote connector policy is an explicit allowlist. Entering a goal, StateGraph,
subagent, resumed run, or background worker must not broaden that allowlist.

## State And Persistence

Approval is durable state. A chat message that expresses approval is not itself
an execution grant.

Run, Goal, and LoopRun creation and lifecycle changes must be atomic or use an
explicit, recoverable saga. Partial failure must not leave an apparently active
or approved orphan entity.

Memory must be typed, scoped, provenance-bearing, revocable, and conflict
visible. Preferences learned from prior approvals may inform explanations but
must not expand permissions.

Trace is audit evidence, not the authoritative runtime state. Secrets and
sensitive payloads must be redacted before persistence.

## Surfaces

The supported product surfaces are:

- `navi` CLI for chat, diagnostics, capabilities, goals, approvals, traces,
  memory, connectors, and service operation;
- authenticated local FastAPI endpoints under `/v1`;
- connector adapters discovered through the connector registry;
- the trace web UI as an inspection surface, not an execution authority.

Core capabilities must remain usable and testable without a browser UI or a
specific connector.

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

