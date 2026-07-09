# Navi Loop Architecture Audit - Current Repair Map

Current target design: every request enters the unified durable StateGraph loop.
`loop_kind` labels request shape (`turn`, `control`, `durable_goal`,
`scheduled`) but does not create a second execution path.

This audit follows the loop-engineering model: Prompt -> Context -> Harness ->
Loop. Runtime state, convergence checks, gates, and trace evidence are the
control surface; prompt text is not the control plane.

## Blueprint Implementation Plan

The AGI blueprint is intentionally broad, so implementation is staged by the
runtime failure boundary it removes. Each phase must ship with a bug gate before
the next phase starts.

1. Unified loop kernel and observability. Collapse turn/control/goal execution
   into one durable StateGraph intake, preserve `loop_kind` only as metadata,
   expose LoopRun state in trace APIs and the web UI, isolate semantic checking,
   and make resource gates trace-visible. Bug gate: compile, full pytest, and
   trace web build.
2. Side-effect immunity. Convert connector sends and future remote writes into a
   staged saga contract: prepare artifact, verify, then commit or compensate.
   Bug gate: acceptance and rejection tests must prove staged effects are either
   released or removed.
3. Subconscious context and semantic memory. Add background compaction and a
   typed memory graph for episode/fact/preference/constraint records. Bug gate:
   context injection must be provenance-preserving and never replace current
   runtime facts.
4. Harness hardening and AST patching. Move unsafe command/file/network work
   behind disposable harnesses and prefer structured patch operations where a
   parser exists. Bug gate: failed patches must not mutate the live workspace,
   and rollback evidence must appear in traces.
5. Evolution arena and HITL steering. Run proposed self-improvements against
   historical hard traces in an isolated beta loop, and support user steering
   without bypassing approvals. Bug gate: no automatic merge or external commit
   can occur without checker evidence and an auditable human or policy gate.

The blueprint's "sub-agent swarm" is implemented as role-specialized loop
participants owned by the main flow. Research, coding, and review roles can
operate as isolated workers, but user-visible messages, approvals, and final
acceptance stay in the primary StateGraph path.

## 1. Execution Contract Drift

Problem: runtime and docs previously described routing as a dual-path choice,
which made ordinary turns look like they could bypass the durable loop.

Solution: request routing now validates intent only and returns `unified_loop`.
Run kind, Goal evidence, and LoopSpec metadata persist `loop_kind` for
observability without splitting execution.

Status: Implemented.

## 2. Incomplete Web Trace

Problem: web trace mostly projected `trace_events`; durable LoopRun state,
checkpoints, transitions, terminal state, and loop decisions were hidden.

Solution: trace APIs now return merged run views and `loop_runs`; StateGraph
transitions and resource gates emit `loop.decision`; the web UI shows loop runs,
transition counts, gate decisions, and budget ledger rows.

Status: Implemented.

## 3. Unified Loop Context

Problem: after ordinary turns enter the durable loop, planner/checker context
must stay tied to the original session/source/peer/sender.

Solution: LoopSpec metadata now carries ingress identity and session metadata;
planner context is assembled from the same session contract. Long session
history now enters the planner through a bounded compaction policy: recent
messages stay visible, older turns are reduced to provenance previews, and
`runtime_facts.conversation_compaction` records the policy, counts, and limits.

Status: Implemented for planner intake. Remaining product work: replace the
deterministic older-message preview with the later semantic memory graph and
background compaction daemon.

## 4. Checker Independence

Problem: semantic tasks without a deterministic verification command previously
depended on model reflection to decide completion, which blurred maker/checker
responsibilities.

Solution: the default semantic verification path now uses a required
`LLM_CHECKER` step whose evidence is produced by an isolated `checker` role.
The checker receives objective, acceptance criteria, attempt number, and final
capability evidence only; it does not see planner reasoning or attempt history.
`DeterministicChecker` consumes the resulting evidence and decides converge,
retry, or block.

Status: Implemented for semantic verification.

## 5. Loop Gates and Budget Ledger

Problem: loop gates existed, but budget/resource decisions were not a complete
trace-visible ledger.

Solution: LoopSpec carries `BudgetPolicy`; StateGraph configures
ResourceGateway from that policy when no gateway is supplied; every resource
gate writes trace evidence; web trace surfaces recent budget/gate rows.

Status: Implemented for resource gates. The old user-visible step-budget
mechanism remains intentionally removed.

## 6. External Side Effects

Problem: local file edits are protected by shadow workspaces, but external
side effects such as connector sends, remote APIs, and database writes cannot be
rolled back by discarding a shadow directory.

Solution: side-effect capabilities need a saga contract: dry-run/stage, commit,
and compensating action metadata. The loop should stage or mock external writes
until checker acceptance, then commit through an auditable step.

Status: Implemented for the current staged-artifact contract. `ToolSpec`
`side_effect_policy` distinguishes local immediate writes from staged external
effects; Weixin outbound media declares an external staged saga and returns
explicit `side_effect_*` facts. StateGraph now releases accepted staged effects
through a saga port, compensates rejected/timed-out staged artifacts, and emits
trace-visible `state_graph.side_effect.commit/compensate` decisions. Remaining
product work is to add concrete commit adapters for future remote APIs beyond
the current connector-egress handoff.

## 7. Evolution Arena Gate

Problem: evolution proposals already required an approved evaluation before
apply, but the approval record only stored a result and approver. That made the
arena evidence weak: a proposal could say "approved" without durable proof of
which hard traces, checker, or acceptance evidence justified the decision.

Solution: proposal evaluations now persist `evaluation_evidence`. Approved
evaluations require both an approver and non-empty evidence, and apply checks
that evidence again before side effects run. API, capability, and CLI surfaces
all pass the evidence field through the same ledger gate.

Status: Implemented for proposal apply safety. Remaining product work: build
the actual beta-run arena that automatically fills `evaluation_evidence` from
historical trace replays and checker results.

## 8. AST Patch Gate

Problem: `file.write` can safely target shadow workspaces, but it still accepts
raw text. For Python code edits, a malformed replacement can be detected before
the file is mutated if the patch goes through an AST-aware boundary.

Solution: `python.ast.replace_symbol` replaces exactly one Python function or
class definition after parsing both the existing file and replacement. The tool
validates the whole patched file before writing, supports shadow workspaces, and
returns structured blocked facts when parsing or symbol matching fails.

Status: Implemented for Python function/class replacement. Remaining product
work: route model code-edit plans toward AST tools by default and add
language-specific structured patch tools where parsers exist.

## 9. Memory Confidence Decay

Problem: memory GC expired time-bounded items, but old learnable memories kept
their original confidence indefinitely. That made stale preferences, facts, and
semantic memories too sticky unless a user explicitly corrected them.

Solution: `MemoryStore.garbage_collect()` now also decays inactive
`preference`, `fact`, and `semantic` memories after a grace window. Low
confidence active items move to `stale`; `constraint` and `negative` memories
are excluded so durable red lines do not silently weaken.

Status: Implemented for explicit memory maintenance. Activation metadata now
feeds the decay anchor through the explicit activation contract below.

## 10. Memory Activation Tracking

Problem: confidence decay needed a real "was this memory used" signal, but
`memory.recall` must remain read-only so a model cannot mutate durable state by
searching memory.

Solution: `memory.recall` now returns `activation_candidate_ids` for the
recalled records, and `memory.record_activation` records explicit use with
reason and provenance. The write updates `last_recalled_at`, increments
`recall_count`, and passes through the governed memory-write hook. Decay checks
`last_recalled_at` before verification or update timestamps, so recently used
memories do not lose confidence just because their original verification is old.

Status: Implemented for explicit activation recording, decay anchoring, and
durable planner intake. Planner calls receive ordinary recalled memories as a
separate untrusted context block and candidate ids in runtime facts; activation
is only recorded when the planner declares a selected syscall's
`used_memory_ids`, so mere prompt injection does not keep a memory alive.
Remaining product work: replace flat FTS recall with the semantic graph and
background compaction daemon described in the blueprint.
