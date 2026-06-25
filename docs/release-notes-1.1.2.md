# Navi 1.1.2 Release Notes

Date: 2026-06-25

Navi 1.1.2 is a patch release for principle-aligned runtime hardening and docs/eval reconciliation.

## Included

- Blocks remote connector ingress from local-environment fact tools and workflow execution/approval capabilities while preserving governed proposal and safe memory surfaces.
- Adds declared core fact tools for delegation validation: `directory.list`, `git.status`, `system.info`, `service.status`, and `test.run`.
- Fixes `codebase.search` registration by using the current RAG import path, storing its cache under Navi home, and returning the declared `results` schema.
- Fixes provider fallback retries so schema validation only passes optional call arguments accepted by the concrete provider implementation.
- Redacts secret-bearing action capability arguments and errors in audit logs.
- Removes routing policy from the `delegate.spawn` tool description and keeps routing guidance in planner context.
- Aligns dynamic workflow docs and evals with the current runtime contract: resume uses `workflow.run(resume=true)`, and verification is a runtime-backed completion concern rather than a separate public tool.

## Verification

- `python -m ruff check src tests`
- `pytest -q`
- `PYTHONPATH=src python -m navi.cli eval delegations --validate-only --json-output`
- `PYTHONPATH=src python -m compileall -q src/navi`
- `git diff --check`
