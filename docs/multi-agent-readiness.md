# Multi-Agent Readiness

Navi should introduce true multi-agent execution only when a role split improves verified outcomes. The default architecture remains a single agent loop with declared role contracts and trace evidence.

## Role Contracts

Role definitions live in `src/navi/specs/agent_roles.yaml`.

- `planner` selects the next capability syscall.
- `responder` synthesizes user-facing answers from verified observations.
- `notification` formats verified background results for connector surfaces.
- `critic` reviews plans, execution results, and completion claims for missing evidence or unsafe assumptions. Current execution uses a deterministic critic gate before a completed run can update its goal to `verified_complete`.
- `executor` transforms approved plans into actuator instructions and evidence requirements.

## Split Criteria

Use a separate role when one of these is true:

- The task mutates local state and the planner's next action needs independent critique.
- A verifier or recovery plan reports missing evidence.
- A long-running goal is about to become `verified_complete`; completion must carry critic evidence with `passed=true`.
- A background watch result needs connector-specific notification separate from execution.
- Parallel review can reduce risk without racing state mutation.

Do not split roles when the work is a simple read, a direct clarification, or a single low-risk capability call.

## Evidence Contract

Every role handoff must leave trace evidence:

- `planner.syscall` records selected tool, permission, args, confidence, and reason.
- `agent.role_result` records model role, source observation count, target action, and response summary.
- `critic` execution logs record findings, recommendation, and pass/fail evidence for actuator-backed completion.
- `recovery.plan` records verifier-triggered recovery choices.
- Execution roles must preserve actuator evidence with a non-empty evidence list and verification status.

This makes future planner/critic/executor separation auditable before Navi introduces true concurrent sub-agent workers. A completed execution is only a completion candidate until the critic gate passes.
