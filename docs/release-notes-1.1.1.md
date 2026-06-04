# Navi 1.1.1 Release Notes

Date: 2026-06-04

Navi 1.1.1 is a patch release for the governed dynamic workflow line.

## Included

- Reinitializes `traces.db` trace tables when an older schema is found, using the current Navi contract instead of maintaining compatibility migrations.
- Keeps the dynamic workflow behavior from `1.1.0`: model-selected `workflow.propose`, explicit approval, bounded execution, resumable state, and verifier-backed completion.
- Adds a regression test for the real upgrade path where an older trace schema is present before service startup.

## Verification

- `navi.service` was restarted and verified active after the trace store reinitialization fix.
