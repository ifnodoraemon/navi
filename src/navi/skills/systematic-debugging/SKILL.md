---
name: systematic-debugging
description: A repeatable workflow for diagnosing failures from symptoms, logs, traces, configuration, and recent changes.
metadata:
  navi.permission: read
  navi.tags: "debugging,diagnosis,reliability"
---

# Systematic Debugging Skill

Use this skill when the user reports a failure, regression, timeout, missing response, broken connector, failing test, or unclear runtime behavior.

1. Restate the observable symptom in concrete terms: command, service, connector, user-visible behavior, expected behavior, and actual behavior.
2. Gather facts before proposing a fix. Prefer service status, recent logs, trace records, config facts, test output, and repository status over speculation.
3. Separate likely failure domains: input/request, model routing, tool invocation, connector delivery, persistence, background task execution, and final user-visible response.
4. Reproduce the smallest failing path when possible. If a live connector is involved, add or run a connector-level journey rather than relying only on unit tests.
5. Convert the failed contract into a general regression case when it is user-visible or likely to recur.
6. After a fix, verify the original symptom and at least one nearby negative case so the fix does not overroute similar text.
