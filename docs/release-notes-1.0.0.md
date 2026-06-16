# Navi 1.0.0 Release Notes

Date: 2026-06-04

Navi 1.0.0 is the first stable agent OS contract for the local-first personal assistant.

## Release Position

Navi is now positioned as a governed local agent OS for a personal AI assistant, not a chat wrapper and not an enterprise RAG product. The v1 contract centers on inspectable capabilities, durable memory, trust and approval state, auditable execution, connector-safe ingress, and trace-driven improvement.

## Included

- Capability-driven planner with declared tool specs and permission ceilings.
- Task, watch, goal, approval, execution, recovery, and trace lifecycles.
- Governed memory with typed items, scope, provenance, confidence, recall reasons, conflicts, and revocation.
- Reversible evolution ledger for prompts, skills, memory items, trust policy, workflow policy, and eval cases.
- Declarative capability safeguards in `capability_safeguards.yaml`.
- Remote connector tool policy with explicit allowed tools, permission ceiling, blocked capability classes, and audit facts.
- Prompt boundary hardening: observed facts are untrusted input blocks; capability result envelopes remain structured runtime facts.
- Budget exhaustion is internal runtime state and no longer leaks as user-visible output.
- Planner goal-integrity guidance: task goals are subordinate to user intent, durable constraints, approvals, permissions, and safeguards.
- CLI and local FastAPI surfaces for chat, memory, sessions, delegations, goals, approvals, tools, traces, diagnostics, subagents, connectors, and evolution.

## Known Post-v1 Work

- Durable context compaction contract for long-running goals.
- Embedded-content provenance on all tool outputs.
- User-visible memory influence records in responses and traces.
- Install-time permission manifests for plugin and MCP providers.
- Assistant status surface for active goals, stop conditions, pending approvals, last evidence, memory influence, and safeguard pauses.
- Live Weixin/iLink payload calibration beyond the standard connector path.

## Verification Baseline

The v1 release candidate should pass:

```bash
pytest -q
python -m compileall src tests
git diff --check
```
