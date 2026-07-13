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
SQLite stores + TraceStore + connector outboxes
```

The unified loop is a protocol boundary. `loop_kind` distinguishes a turn,
control action, durable goal, or scheduled run without creating an unrelated
execution engine.

## Layer Ownership

| Layer | Primary modules | Responsibility |
|---|---|---|
| Ingress | `cli.py`, `api.py`, `connector_runtime.py` | Normalize external input and identity. |
| Turn control | `control_plane.py`, `turn_lifecycle.py`, `control.py` | Build turn context, current-state facts, trace, and final result. |
| Loop control | `loop_control_service.py`, `goal_state_graph.py` | Create or resume durable loop entities and bridge into the graph. |
| Loop kernel | `state_graph.py`, `loop.py`, `loop_contracts.py` | Plan, execute, check, pause, recover, and converge. |
| Capabilities | `capabilities.py`, `capabilities_types.py`, `actions/`, `core_tools/` | Declare, filter, validate, invoke, and audit callable operations. |
| Isolation | `harness.py`, `workspaces.py`, `resource_gateway.py` | Bound commands, workspaces, locks, resources, and merge behavior. |
| State | `runs/`, `goals.py`, `loop_runs.py`, `memory/`, `trace.py`, `evolution.py` | Persist lifecycle, memory, checkpoints, and audit evidence. |
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
gates. Staged external effects are committed only after acceptance or
compensated on rejection.

The checker evaluates objective evidence independently from planner reasoning.
Trace proxies record model calls and capability spans without changing their
decisions.

## Persistence

SQLite is the local storage mechanism. Runs and approvals share `runs.db`;
goals, loop checkpoints, traces, memory, graph data, and connector state use
specialized stores.

Cross-store operations require a Unit of Work or an explicit saga. Creating a
Run, Goal, LoopSpec, and LoopRun is one logical operation even when the records
live in separate databases.

Trace events are append-oriented audit evidence. Materialized Run, Goal, and
LoopRun records own active lifecycle state.

## Connector Boundary

Connector adapters own authentication, polling, message normalization, media
transport, deduplication, and channel-local presentation. They publish the same
turn contract used by local surfaces.

Connector ingress uses the same capability catalog and default permission
ceiling as local CLI ingress. Sensitive operations do not execute merely because
they are visible to the planner: the shared risk assessment and durable approval
gate pause them before any effect. Connector authentication and sender policy
still determine who may enter the loop.

Configured MCP servers join the same registry through governed discovery and
call broker capabilities. Streamable HTTP and stdio are transport choices, not
separate permission models; configured permission, durable approval, audit, and
redaction apply to both.

## Known Current Deviations

These are present code/contract discrepancies, not a future feature roadmap:

- Run, Goal, and LoopRun remain separate SQLite stores. Open failures now use
  an explicit compensation path, but cross-store updates are not one database
  transaction.

Until these deviations are repaired, tests that exercise only individual
registries or graph nodes do not prove the end-to-end control boundary.
