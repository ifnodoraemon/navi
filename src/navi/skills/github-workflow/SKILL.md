---
name: github-workflow
description: Practical workflow guidance for GitHub issues, pull requests, reviews, CI failures, and repository maintenance.
metadata:
  navi.permission: read
  navi.tags: "github,pull-requests,ci"
---

# GitHub Workflow Skill

Use this skill when the user asks about GitHub issues, pull requests, commits, pushes, CI failures, review comments, or release coordination.

1. Inspect local git state before changing files, committing, rebasing, or pushing.
2. Keep user changes intact. Do not revert unrelated dirty files unless the user explicitly asks.
3. For CI failures, identify the failing job, failing command, relevant log lines, and whether the failure reproduces locally.
4. For review work, answer with findings first: severity, file, line, impact, and suggested fix. Keep summaries secondary.
5. For commits, stage only files relevant to the requested change and use a concise message that describes the behavior change.
6. For PR or issue updates, distinguish implemented changes, verification performed, and remaining risks.
