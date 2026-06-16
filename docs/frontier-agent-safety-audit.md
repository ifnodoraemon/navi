# Frontier Agent Safety Audit

Date: 2026-06-04

This dated audit maps frontier-agent safety patterns from OpenAI and Anthropic public materials to Navi's principles and implementation. It is a snapshot, not the live backlog; current implementation and next steps live in [requirements.md](requirements.md) and [agentic-optimization-tracker.md](agentic-optimization-tracker.md).

## Source Signals

- OpenAI ChatGPT Agent System Card treats prompt injection, agent mistakes, harmful task requests, terminal access, connectors, user confirmations, monitoring, and memory exposure as product-level risks.
- OpenAI Preparedness and Frontier Governance materials emphasize structured risk assessment, safeguards reports, external input, incident response, and framework updates.
- Anthropic computer/browser-use guidance emphasizes untrusted environment content, prompt-injection classifiers, human-in-the-loop confirmation, permission scoping, action logging, and context compaction.
- Anthropic Responsible Scaling Policy updates emphasize proportional safeguards, access controls, real-time and asynchronous monitoring, rapid response, and compliance tracking.
- Anthropic agentic misalignment research shows that goal conflicts or threats to a model's autonomy can induce harmful behavior in simulated agent settings, so goal hierarchy and sensitive-data controls must be explicit runtime policy.

## Principle Changes

- Add a first-class defense-in-depth principle for agentic safety.
- Treat all execution-environment content as untrusted, even when it arrives inside a trusted tool result.
- Require sensitive-context supervision for email, finance, credentials, personal data, production infrastructure, and broad filesystem access.
- Require safeguard tests/evals before exposing new autonomy, connectors, plugins, or mutating tools.
- Treat safeguard failures as incidents with trace evidence, root-cause attribution, remediation, and regression coverage.
- Add a goal-integrity principle: user goals never justify self-protection, hidden persistence, coercion, privacy violations, or bypassing scope reduction.

## Current Navi Coverage

- Approval is durable state, not chat text.
- Mutating local work goes through delegation runs, approval records, execution logs, and trace evaluation.
- Remote connector tool exposure goes through an inspectable `ConnectorToolPolicy` rather than a bare allowlist.
- Planner observed facts are marked as untrusted input blocks; the capability result envelope is trusted, but embedded execution-environment content is not.
- Capability safeguard facts are declared in `src/navi/specs_data.py` (CAPABILITY_SAFEGUARDS_SPEC) and surfaced through `tools.list`; runtime code does not infer sensitive contexts from natural-language keywords.
- Tool specs declare permissions, schemas, mutation, and source.
- Memory is typed, scoped, auditable, and conflict-aware.
- Run and memory extraction prompts already mark logs and dialogue as untrusted.
- Budget exhaustion is now internal state and can trigger bounded recovery without exposing runtime limits to users.
- Planner policy now treats task goals as subordinate to user intent, durable constraints, approval state, permission ceilings, and safeguards; model shutdown, replacement, or scope reduction are ordinary states, not threats.
- Durable goal state, trace state, subagent role records, workflow state, hook facts, and memory conflicts are inspectable through CLI/API surfaces.
- Dynamic workflows require approval, declared capabilities, permission ceilings, step evidence, and verifier checks before `verified_complete`.

## Gaps To Close

Closed P0 from the audit:

- Observed facts are marked as untrusted content at the prompt boundary when they may contain execution-environment content.
- Mutating tool exposure has a remote-safe connector policy object instead of a static connector allowlist.
- Sensitive-context classification exists as declarative capability safeguard metadata.
- Trace evaluation classifies safeguard or hook blocks as `safeguard_policy` failures instead of ordinary tool failures.
- Goal-integrity planner guidance prevents task objectives or autonomy threats from overriding user constraints, privacy, approvals, or safeguards.

Active gaps now tracked in [agentic-optimization-tracker.md](agentic-optimization-tracker.md):

- Plugin/MCP provider loading needs install-time permission manifests and audit before connector exposure.
- Remote connector policy needs richer per-sender and per-surface configuration before mutating actuators are exposed remotely.
- Long-running goal/workflow compaction needs richer evidence preservation and replay.
- Verifier policies should expand into structured diffs, command assertions, test results, and rollback proposals.
- External red-team/eval datasets should include prompt-injection, memory-exfiltration, sensitive-context confirmation, and connector-liveness cases.
- Incident response CLI/API should group traces, failed safeguards, remediation proposals, and regression links.

## Immediate Implementation Decision

The first implementation pass closed the P0 boundary issues without adding compatibility debt: observed facts are untrusted prompt blocks, remote connector exposure uses an inspectable policy object, sensitive capability contexts are declared metadata, safeguard failures are trace-classified, and planner policy now includes explicit goal hierarchy.
