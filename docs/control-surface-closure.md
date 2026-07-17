# Control Surface Closure

This document is the live closure checklist for Navi control-plane entities.
It exists because every first-class entity must have a complete lifecycle
surface instead of isolated one-off fixes.

## Closure Rule

A first-class entity is complete only when the same governed capability surface
supports these operations inside the caller policy envelope:

| Operation | Requirement |
|---|---|
| List | Enumerate scoped records with an explicit authoritative scope. |
| Create | Create or start the entity through the governed lifecycle owner. |
| Read | Read one record or a named view without changing state. |
| Update | Change mutable fields without creating duplicate records. |
| Cancel/Delete | End, revoke, or remove the entity through an explicit lifecycle path. |
| Verify | Return read-back evidence after mutation so checker convergence does not depend on planner guesses. |
| History | Expose terminal records and recent outcomes separately from current work. |

Compatibility aliases for old internal contracts do not satisfy closure. If a
view or operation is ambiguous, the contract should be split rather than
silently folded into a generic field.

## Entity Matrix

| Entity | List | Create | Read | Update | Cancel/Delete | Verify | Current gap |
|---|---|---|---|---|---|---|---|
| Goal | `goal.state` views | `goal.open` | `goal.state(goal_id)` | `goal.update` | `goal.cancel` single or explicit ids | `verified_goal`, `verified_state`, `verified_after` | `goal.update` should grow the same verified read-back shape for all mutations. |
| Scheduled Goal | `goal.state view=scheduled` | `goal.open` with schedule | `goal.state(goal_id)` | `goal.update(goal_id)` | `goal.cancel(goal_id)` | single-goal verification | Add selector-based schedule cleanup only if it has an explicit scoped view and read-back. |
| Approval | existing approval reads through run state and `approval.resolve` | `approval.request` | pending approval facts | resolve only | `approval.resolve reject` | approval resolution facts | Needs an explicit scoped list/read surface independent of task goals. |
| Child Goal | `agent.control(operation=list)` depth-1 only | `agent.control(operation=start)` | `agent.control(operation=status)` | message only | `agent.control(operation=cancel)` | child result/report facts | Must stay separate from top-level goal cleanup. |
| Memory | memory recall/search surfaces | memory add | item facts from recall/search | not complete | not complete | provenance facts | Needs revoke/update/list-by-scope closure before memory is treated as fully governed. |
| Session | session elevation facts | session create/elevate | current session facts | not complete | not complete | approval/elevation facts | Needs explicit list/read/revoke lifecycle for durable session grants. |
| Trace | trace CLI/eval reads | runtime append | trace show/evaluate | append-only | retention/delete policy not complete | eval report | Needs governed retention/deletion path if traces become user-facing entities. |

## Regression Rules

- `goal.state` must expose distinct `current`, `scheduled`, `pending_approval`,
  and `history` views; it must not use `active_goals` as a generic task bucket.
- Bulk cleanup must pass explicit lifecycle entity ids, usually after a
  scoped `goal.state` read; selector shortcuts must not encode one incident's
  cleanup policy.
- `respond(options)` is a terminal suggestion response. Blocking user input
  uses `ask.user` and is not an approval gate.
- A checker must accept a successful mutation once the target record's
  read-back state proves the requested transition.
- Trace evaluation degrades runs that report success while showing conceptual
  contradictions, repeated mutations, or ordinary asks mislabeled as approvals.
