---
name: Test Driven Development
description: Guidance for turning user-facing failures and desired behaviors into focused tests, evals, and regression gates.
permission: read
source: local
tags: [testing, evals, regression]
---

# Test Driven Development Skill

Use this skill when implementing or repairing behavior that can regress.

1. Start from the user-visible workflow, not an internal assumption. Write the test in the same language and channel shape the user actually used when practical.
2. Add the narrowest unit test for deterministic code and a journey/eval for multi-step agent behavior, connector delivery, scheduling, or tool routing.
3. Include negative assertions when the bug is a confusion bug, such as mixing two categories or choosing an unsafe tool.
4. Keep mock-provider routes aligned with the same high-level intent being tested, but do not encode business behavior in production source as phrase matching.
5. Run the smallest relevant tests first, then the broader regression gate when the fix affects shared routing, memory, tools, or connectors.
6. When a real incident is fixed, add it to the regression inventory with the test or eval case that covers it.
