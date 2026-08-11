---
name: browser-screenshot-verification
description: Guidance for controlled page screenshots and visual evidence when the browser screenshot capability is available.
metadata:
  navi.permission: read
  navi.tags: "browser,automation,screenshots,playwright"
---

# Browser Screenshot Verification Skill

Use this skill when a task needs a rendered page screenshot or visual layout
evidence and `browser.screenshot` is present in the current capability catalog.
This skill does not provide navigation sessions, DOM inspection, clicks, text
input, or form submission.

1. Confirm `browser.screenshot` is callable before proposing a screenshot step. If
   it is unavailable, report the declared prerequisite facts; do not imitate an
   interactive browser through coordinates or unrelated shell commands.
2. Start from the authorized page URL, expected visible state, output artifact,
   and the visual question the screenshot should answer.
3. Capture only pages the user is authorized to inspect. A screenshot is an
   observation, never evidence that a click, form submission, or external effect
   occurred.
4. Verify the returned artifact facts (`exists`, `size`, and successful command
   result) before using the image as evidence.
5. Keep credentials, cookies, and personal data out of logs and summaries unless
   the user explicitly asks to inspect them.
6. If the task requires DOM state or interaction, state that an interactive
   browser capability is required and stop before claiming completion.
