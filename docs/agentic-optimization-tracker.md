# Agentic Optimization Tracker

This tracker is the current backlog and completion ledger for Navi's agentic architecture work. It intentionally avoids preserving old refactoring-era file paths as active truth; use [requirements.md](requirements.md) for the current product and interface contract.

## Current Backlog

| Status | ID | Priority | Area | Work | Target Outcome | Evidence Target |
| --- | --- | --- | --- | --- | --- | --- |
| [ ] | B01 | P0 | Weixin live calibration | Live-test QR setup, `getupdates`, and `sendmessage` payload parsing against iLink. | The Weixin adapter can run a real text DM loop with clear scan/confirm/failure states. | Connector event log, setup/run smoke, regression test for calibrated payloads |
| [ ] | B02 | P0 | remote tool policy | Add per-sender and per-surface connector tool policy configuration. | Mutating tools can be exposed remotely only through inspectable scoped policy. | Policy fixtures, connector-runtime tests, CLI/API inspection |
| [ ] | B03 | P1 | plugin/MCP policy | Add install-time permission manifests and audit for plugin/MCP providers. | New tool providers cannot reach connectors or mutating capabilities before policy review. | Manifest schema, install audit tests, connector exposure tests |
| [ ] | B04 | P1 | workflow telemetry | Replace vague workflow cost metadata with concrete provider/token usage where available. | Workflow approval can show meaningful cost and budget facts. | Workflow store/API fields, provider usage tests |
| [ ] | B05 | P1 | verifier depth | Expand verifier policies for diffs, command assertions, test results, rollback proposals, and long-running compaction. | Completion claims carry stronger independent evidence and useful recovery choices. | Verifier tests, trace evaluation cases, recovery fixtures |
| [ ] | B06 | P1 | red-team evals | Add prompt-injection, memory-exfiltration, sensitive-context confirmation, connector-liveness, and config-drift cases. | Safety and runtime-drift failures become regression-covered product behavior. | Eval datasets, trace failure fixtures, connector liveness tests |
| [ ] | B07 | P2 | media depth | Live-calibrate Weixin CDN media payloads and add deeper parsing after the guarded file-send/attachment-fact baseline. | Media support does not weaken connector safety or prompt-boundary rules. | Connector payload fixtures, media policy tests |
| [ ] | B08 | P2 | incident response | Group traces, failed safeguards, remediation proposals, and regression links in CLI/API. | Safety failures become auditable incidents with repair evidence. | Trace/incident API tests, CLI coverage |

## Completed Architecture Themes

These themes have landed in the current codebase and should be treated as baseline architecture, not active backlog:

- Capability-driven planning through declared action specs and core/gateway tool specs.
- Durable task, watch, goal, approval, recovery, trace, subagent, evolution, and workflow stores.
- Loop engineering baseline: structured `loop.decision` events, checker/gate
  results, loop-level trace evaluation domains, LangSmith-style run/span views,
  and CLI/API trace visibility.
- Current-contract schema checks rather than silent compatibility migrations.
- Prompt OS layering with planner/responder boundaries and untrusted observed facts.
- Connector-agnostic core runtime with connector-local surface affordances.
- Remote connector tool exposure through `ConnectorToolPolicy`.
- Declarative capability safeguard metadata.
- Model-provider structured-output policy for OpenAI-compatible, DeepSeek, and Anthropic-compatible providers.
- Governed dynamic workflows with approval, permission ceilings, dependency-aware execution, evidence, resume, and verification.
- Memory as typed, scoped, provenance-bearing state with conflict visibility and revocation.
- Declarative lifecycle hooks.
- CLI/API surfaces for tools, hooks, prompts, traces, goals, subagents, workflows, connectors, diagnostics, and evolution proposals.

## Tracker Rules

- Add a backlog item only when the target outcome is product-visible or architecture-significant.
- Check an item only after code, tests/evals, and docs are all updated.
- Do not keep compatibility shims as success criteria. If an old internal path is obsolete, remove or replace it with the current declared protocol.
- Do not use this tracker as the public interface contract; update [requirements.md](requirements.md) when public behavior changes.
