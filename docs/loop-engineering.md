# Loop Engineering

Loop engineering in Navi means the observe-plan-act loop has explicit runtime
decisions, checkers, gates, and trace evaluation. It is not a prompt rule that
tells the model how to behave.

## Runtime Contract

Every meaningful loop edge records a `loop.decision` trace event before the
trace is evaluated.

Required decision shapes:

- `continue`: a capability observation was appended and the planner should take
  another step.
- `recover`: a completion checker blocked a terminal answer and recovery facts
  were added to observations.
- `pause_for_approval`: the current facts require approval or an existing
  approval is already pending.
- `converged`: the runtime detected repeated stable progress and is finalizing
  from stable observations.
- `finalize`: terminal facts or completion evidence are sufficient to answer.
- `blocked`: a checker or gate prevents the loop from completing.
- `failed`: planner, provider, parser, safeguard, capability, or workflow-step
  failure ended the loop.

Each decision may include:

- `checker_results`: fact-level completion checks such as completion evidence,
  workflow-step evidence, terminal result validity, or capability result status.
- `gate_results`: runtime gates such as approval wait and no-progress
  convergence.
- `failure_domain`: structured trace/eval domain such as `planner_or_parser`,
  `capability_failure`, `checker_blocked`, `approval_loop`, or `none`.
- `progress_signature`: the stable signature used for no-progress detection.
- `workflow_id`, `step_id`, and `goal_ids` when the loop is part of durable
  workflow or goal execution.

## Trace Evaluation

`trace.evaluate` must prefer loop decisions over raw first-failed-event
heuristics. The current loop-level failure domains are:

- `planner_or_parser`
- `provider_no_response`
- `capability_failure`
- `safeguard_policy`
- `checker_blocked`
- `missing_completion_check`
- `approval_loop`
- `loop_no_progress`
- `runtime`
- `trace_missing`

Raw events such as `planner.syscall`, `capability.result`,
`loop.check`, `loop.recovery`, `runtime.converged`, and `turn.final`
remain as evidence. They are not the primary diagnosis when a loop decision is
available.

Inspection surfaces:

- `navi trace show TRACE_ID` includes inline loop-decision summaries.
- `navi trace decisions TRACE_ID` lists only loop decisions.
- `navi trace runs TRACE_ID` lists a LangSmith-style run/span projection.
- `GET /v1/traces/{trace_id}` returns both raw `events` and `loop_decisions`.
- `GET /v1/traces/{trace_id}/decisions` returns only loop decisions.
- `GET /v1/traces/{trace_id}/runs` returns only run/span projections.

## LangSmith-Style Alignment

Navi traces should move toward a run/span model: a root trace run plus child
runs for planner calls, capability calls, loop checks, recovery, workflow
steps, and final responses. Each projected run exposes `name`, `run_type`,
`status`, `thread_id`, `inputs`, `outputs`, `tags`, `metadata`, and
`feedback`.

The current implementation derives this view from `trace_events`, which remain
the single trace source of truth. The root run uses the `trace_id`; child runs
point to it through `parent_run_id`. `session_id` is exposed as `thread_id`,
which gives trace consumers a LangSmith-style thread grouping without making
the runtime infer the agent's next step.

Remaining parity gaps are explicit product work: durable feedback capture,
dataset links, and export/import interoperability.

## Prohibited Surfaces

Navi must not control loop behavior through:

- Planner prompt rules for a specific approval state.
- Hardcoded `final.answer` fallback text.
- Visible step-budget or budget-exhausted semantics.
- Aliases for obsolete trace failure-domain names.
- JSON extraction from markdown fences, surrounding prose, or provider
  reasoning text for model-owned protocols.

Machine vocabulary for phases, decision kinds, checker names, and failure
domains must live in `src/navi/loop.py`, not as scattered string checks in
runtime code.

Trace evaluation must read structured JSON fields such as `failure_domain`,
`checker_results`, and `gate_results`. It must not infer domains from natural
language `reason` text or token matching.

If the model repeats a `delegate.spawn` while the approval is already pending,
the runtime records `pause_for_approval` with an `approval_gate` result and the
trace can evaluate to `approval_loop`. The fix belongs in state, trace, and gate
semantics, not in a prompt warning.
