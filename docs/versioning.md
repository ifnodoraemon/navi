# Versioning

Navi uses staged semantic versioning while the project is still pre-1.0.

## Version Sources

- Package version: `pyproject.toml` `[project].version`
- Runtime version: `navi.__version__`
- Release tags: `v<version>`, for example `v0.1.0`

The package version and runtime version must always match. CI verifies this.

## Bump Rules

Before `1.0.0`, the version communicates maturity stages:

- Patch bump, for example `0.1.0` to `0.1.1`: tests, docs, CI, packaging, bug fixes, eval dataset additions, and internal hardening that do not change the user-visible operating model.
- Minor bump, for example `0.1.x` to `0.2.0`: a new stage of capability or architecture, such as a new syscall family, connector class, permission layer, memory subsystem, model provider integration, or agentic control-plane change.
- Major bump to `1.0.0`: the agent OS contract is stable enough that task/watch/memory/tool/skill permissions and public APIs are intentionally maintained.

Breaking compatibility is allowed before `1.0.0`, but the version must still move when the operating model moves.

## Stage Gates

Every stage bump should include:

- Passing CI on the release commit.
- Passing task eval dataset validation.
- A package build and wheel smoke test.
- A GitHub tag `v<version>` after the version PR is merged.

## Release Flow

1. Open a PR that changes `pyproject.toml` and `src/navi/__init__.py`.
2. Merge only after CI and review pass.
3. Tag the merged commit with `v<version>`.
4. Push the tag to trigger the GitHub Release workflow.

## Current Stage

`0.1.x` is the first agentic OS stage: capability syscall routing, task/watch lifecycle, trust/approval governance, multi-provider model configuration, connector surfaces, eval dataset validation, and CI release packaging.

The next architecture stage should be `0.2.0`.
