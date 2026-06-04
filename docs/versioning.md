# Versioning

Navi uses semantic versioning for the public agent OS contract.

## Version Sources

- Package version: `pyproject.toml` `[project].version`
- Runtime version: `navi.__version__`
- Release tags: `v<version>`, for example `v0.1.0`

The package version and runtime version must always match. CI verifies this.

## Bump Rules

Before `1.0.0`, the version communicated maturity stages:

- Patch bump, for example `0.1.0` to `0.1.1`: tests, docs, CI, packaging, bug fixes, eval dataset additions, and internal hardening that do not change the user-visible operating model.
- Minor bump, for example `0.1.x` to `0.2.0`: a new stage of capability or architecture, such as a new syscall family, connector class, permission layer, memory subsystem, model provider integration, or agentic control-plane change.
- Major bump to `1.0.0`: the agent OS contract is stable enough that task/watch/memory/tool/skill permissions and public APIs are intentionally maintained.

From `1.0.0`, public contracts must move deliberately:

- Patch bump, for example `1.0.0` to `1.0.1`: compatible bug fixes, docs, tests, evals, packaging, and internal hardening.
- Minor bump, for example `1.0.x` to `1.1.0`: compatible new capabilities, public API additions, connector additions, or new inspectable control-plane surfaces.
- Major bump, for example `1.x` to `2.0.0`: intentional public contract changes that require user action.

Internal compatibility debt is still prohibited. Obsolete internal schemas, prompt shapes, aliases, and workflow branches should be removed rather than silently adapted unless migration is the explicit feature being shipped.

## Stage Gates

Every stage bump should include:

- Passing CI on the release commit.
- Passing task eval dataset validation.
- A package build and wheel smoke test.
- A GitHub tag `v<version>` after the version PR is merged.

## Release Flow

1. Open a PR that changes `pyproject.toml`, `src/navi/__init__.py`, and release notes.
2. Merge only after CI and review pass.
3. Tag the merged commit with `v<version>`.
4. Push the tag to trigger the GitHub Release workflow.

## Current Stage

`1.1.1` is the current v1 stage. It extends the stable Navi agent OS contract with governed dynamic workflows: declarative orchestration plans, subagent step records, dependency-aware execution, explicit approval, resumable workflow state, verifier-backed completion, and service startup hardening for current-contract trace store reinitialization.
