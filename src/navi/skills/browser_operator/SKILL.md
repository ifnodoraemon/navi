---
name: Browser Operator
description: Guidance for controlled browser automation, page inspection, screenshots, form workflows, and visual verification.
permission: read
source: local
tags: [browser, automation, screenshots, playwright]
---

# Browser Operator Skill

Use this skill when a task needs a real browser: checking page behavior, filling forms, taking screenshots, verifying visual layout, or debugging client-side interactions.

1. Start from the user workflow: page, account state, input data, expected outcome, and the visible failure.
2. Use browser automation only within the authorized site or local app. Do not submit irreversible actions without explicit user approval.
3. Prefer semantic selectors and visible labels. Fall back to coordinates only when the UI has no stable structure.
4. Capture screenshots or DOM snapshots when visual state matters, and verify that text, buttons, canvas content, and media are actually visible.
5. For local apps, ensure the dev server is running, record the URL, and check both desktop and mobile viewports when layout can vary.
6. Keep credentials, cookies, and personal data out of logs and summaries unless the user explicitly asks to inspect them.
