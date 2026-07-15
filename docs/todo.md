# Active Engineering TODO

Only unresolved engineering gaps belong here. Completed work is retained in
tests, traces, and release notes rather than duplicated as a permanent checklist.

## Open engineering gaps

- [ ] Run an opt-in live connector-path smoke when a configured recipient and
  delivery authorization are available; local connector regressions remain a
  required gate and must not send unsolicited messages.
- [ ] Make the maximum-active-child admission reservation atomic across
  concurrent API and daemon processes; the current sequential gate still
  inherits the known cross-store saga race.
