# Active Engineering TODO

This file tracks the current repair program. Checked items remain through the
next release verification, then must be removed once their evidence is retained
in tests, traces, or release notes.

## P0 — 2026-07-15 live conversation regressions

- [x] Make `shell.run` apply call-dependent read/network/write policy so factual
  read commands do not require write approval and opaque/effectful commands fail
  closed. Evidence: traces `7482957309616430216` and `7482957819081702536`.
- [x] Preserve and expose the resumed operation result through
  `approval.resolve`, instead of returning only approval metadata. Evidence:
  traces `7482957409423991048` and `7482957901583633928`.
- [x] Keep follow-up references tied to recent task outcomes and scoped goal
  state, so “但是没给我内容啊” does not resume an unrelated older task. Evidence:
  trace `7482957649409519368`.
- [x] Add focused trace regression tests.
- [ ] Run an opt-in live connector-path smoke when a configured recipient and
  delivery authorization are available; local connector regressions remain a
  required gate and must not send unsolicited messages.

## P0 — Child-agent foundation

- [x] Ship depth-1 background child Goals with a maximum of three active
  children per parent.
- [x] Enforce parent-policy, caller-policy, system-policy, permission, workspace,
  identity, budget, and capability intersections at runtime.
- [x] Keep user interaction in the main flow; children may receive parent
  messages and return structured reports but may not respond, approve, connect,
  or recursively delegate.
- [x] Verify `agent.control` operations and child-only `agent.report` across
  daemon execution and transient resource-pause resume.
- [ ] Make the maximum-active-child admission reservation atomic across
  concurrent API and daemon processes; the current sequential gate still
  inherits the known cross-store saga race.

## P1 — Memory correctness

- [x] Scope durable memory writes and recall to global/actor/session/workspace
  envelopes; prevent cross-actor recall.
- [x] Let the model explicitly create or revoke typed memory through governed
  capabilities; do not add runtime-authored semantic extraction rules.
- [x] Mark assistant conversation history as non-authoritative candidate text and
  keep stale assistant replies out of older-history previews.
- [x] Separate recent task outcomes, active constraints, stale/orphan runtime
  records, and conversational text so they cannot masquerade as one memory type.
- [x] Add recall-scope, activation, stale-history, and current-reference tests.

## P1 — Capability-surface consolidation

- [x] Audit the current capability catalog for operation-specific or overlapping
  tools that can be expressed by parameters or input-schema extensions.
- [x] Remove the redundant `directory.list`, `git.status`, `service.status`,
  `system.info`, and `test.run` command aliases in favor of `shell.run` argv.
- [x] Consolidate parent child-agent lifecycle operations into
  `agent.control(operation=...)` and memory revocation into
  `memory.add(operation="revoke")`.
- [x] Define a review gate: every new capability must document the distinct
  authority boundary that prevents reuse of an existing capability.
- [x] Prefer generic call-dependent effect classification over query-specific
  fact tools; keep unknown effects fail-closed.
