# Navi Release Notes

This file is the compact release ledger for the current v1 line. Keep durable
release facts here instead of spreading short historical notes across many files.

## 1.1.2

Date: 2026-06-25

Patch release for principle-aligned runtime hardening and docs/eval reconciliation.

- Blocks remote connector ingress from local-environment fact tools and workflow execution/approval capabilities while preserving governed proposal and safe memory surfaces.
- Adds declared core fact tools for delegation validation: `directory.list`, `git.status`, `system.info`, `service.status`, and `test.run`.
- Fixes `codebase.search` registration by using the current RAG import path, storing its cache under Navi home, and returning the declared `results` schema.
- Fixes provider fallback retries so schema validation only passes optional call arguments accepted by the concrete provider implementation.
- Redacts secret-bearing action capability arguments and errors in audit logs.
- Removes routing policy from the `delegate.spawn` tool description and keeps remote/local access behavior declared through runtime policy and capability facts.
- Aligns dynamic workflow docs and evals with the current runtime contract: resume uses `workflow.run(resume=true)`, and verification is runtime-backed completion rather than a separate public tool.

Verification:

```bash
python -m ruff check src tests
pytest -q
PYTHONPATH=src python -m navi.cli eval delegations --validate-only --json-output
PYTHONPATH=src python -m compileall -q src/navi
git diff --check
```

## 1.1.1

Date: 2026-06-04

Patch release for the governed dynamic workflow line.

- Reinitializes `traces.db` trace tables when an older schema is found, using the current Navi contract instead of maintaining compatibility migrations.
- Keeps model-selected `workflow.propose`, explicit approval, bounded execution, resumable state, and verifier-backed completion.
- Adds a regression test for the real upgrade path where an older trace schema is present before service startup.

Verification:

- `navi.service` restarted and verified active after the trace store reinitialization fix.

## 1.1.0

Date: 2026-06-04

Minor release that adds governed dynamic workflows to the v1 agent OS contract.

- Adds `workflow.propose` for model-selected declarative orchestration plans with objective, subagent steps, dependencies, allowed tools, permission ceiling, cost/risk metadata, stop condition, and verification strategy.
- Adds approval, run/resume, status, durable store, CLI, and API surfaces for workflows. Current resume uses `workflow.run(resume=true)`.
- Keeps workflow plans as data, not executable scripts. Steps run as model-owned loops constrained by declared tool scopes; stored tool-call intents are facts, not replay instructions.
- Keeps remote connector ingress limited to workflow proposal/status by default; workflow approval/run remains blocked remotely unless explicit policy enables it.
- Adds engine-level tests for natural language request, model-selected `workflow.propose`, persisted proposal, and user-facing approval prompt.

Post-release work:

- richer workflow compaction for very long-running workflows;
- cost telemetry and model-token accounting for workflow proposals;
- parallel worker execution once shared-state race rules are mature;
- richer verifier policies for diffs, tests, and rollback plans.

## 1.0.0

Date: 2026-06-04

First stable agent OS contract for the local-first personal assistant.

- Capability-driven planner with declared tool specs and permission ceilings.
- Task, watch, goal, approval, execution, recovery, and trace lifecycles.
- Governed memory with typed items, scope, provenance, confidence, recall reasons, conflicts, and revocation.
- Reversible evolution ledger for prompts, skills, memory items, governance policy, workflow policy, and eval cases.
- Declarative capability safeguards.
- Remote connector tool policy with permission ceilings, blocked capability classes, and audit facts.
- Prompt boundary hardening: observed facts are untrusted input blocks; capability result envelopes remain structured runtime facts.
- Planner goal-integrity guidance: task goals are subordinate to user intent, durable constraints, approvals, permissions, and safeguards.
- CLI and local FastAPI surfaces for chat, memory, sessions, delegations, goals, approvals, tools, traces, diagnostics, subagents, connectors, and evolution.

Post-v1 work:

- durable context compaction contract for long-running goals;
- embedded-content provenance on tool outputs;
- user-visible memory influence records in responses and traces;
- install-time permission manifests for plugin and MCP providers;
- assistant status surface for active goals, stop conditions, pending approvals, last evidence, memory influence, and safeguard pauses;
- live Weixin/iLink payload calibration beyond the standard connector path.
