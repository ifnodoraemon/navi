# Project: Navi Architecture Refactoring

## Architecture
Navi is an AI agent runtime consisting of:
- **Engine Layer (`engine.py`)**: Manages the main execution loop (`HernessEngine`), coordinating the observe-plan-act cycles.
- **Capabilities Layer (`capabilities.py`, `actions/` package)**: Defines action capability execution, parameter schemas, and dynamic tool invocation.
- **Execution Layer (`execution.py`)**: Orchestrates execution state, prompt compilation, and calling model endpoints.
- **API Layer (`api.py`)**: Exposes REST endpoints to query and mutate session, memory, run, and evaluation states.
- **Event Bus (`event_bus.py`)**: Asynchronous Pub/Sub event dispatcher for cross-module notifications.

## Code Layout
- `src/navi/engine.py` - Core engine execution loop.
- `src/navi/capabilities.py` - Registry primitives (`CapabilityRegistry`, `CapabilityContext`).
- `src/navi/actions/` - Decomposed capability handlers (new package).
- `src/navi/actions/helpers.py` - Common capability helper utilities.
- `src/navi/execution.py` - Model preparation and task execution.
- `src/navi/api.py` - FastAPI application exposing API endpoints.
- `src/navi/event_bus.py` - Pub/Sub event broker.
- `src/navi/prompting.py` - System prompt layer loader.
- `src/navi/specs/prompt_layers.yaml` - Default prompt layers.
- `tests/` - Existing pytest test suite.

## Milestones
| # | Name | Scope | Dependencies | Status | Conv ID |
|---|------|-------|-------------|--------|---------|
| 1 | Prompt Abstraction & Capabilities God Module Decomposition | Extract prompts to prompt layer; Decompose `capabilities.py` into `src/navi/actions/` package; extract helpers. | None | IN_PROGRESS | |
| 2 | Resolve Circular Dependencies & Log Protocols | Remove load-time circular dependencies and runtime inline imports; generate ExecutionProtocol in `execute_task`. | M1 | IN_PROGRESS | |
| 3 | Standardize API Boundaries | Route all database mutations in `api.py` (memory, session, evolution) through the Capability layer. | M2 | IN_PROGRESS | |
| 4 | Decouple Background Tasks & Event-Driven Handlers | Implement Pub/Sub background memory/evaluation tasks in `HernessEngine`; introduce domain events/Unit of Work. | M3 | PLANNED | |
| 5 | E2E Testing Track | Design and implement comprehensive tests covering Tiers 1-4. | None | IN_PROGRESS | |
| 6 | Final Verification & Audit | Run complete test suite, verify judge requirements, perform forensic audit. | M4, M5 | PLANNED | |

## Interface Contracts
### `capabilities` ↔ `actions`
- `CapabilityRegistry` discovers and loads capabilities dynamically from `src/navi/actions/`.
- Concrete capabilities implement the `Capability` protocol defined in `src/navi/capabilities.py`.

### `engine` ↔ `execution`
- `HernessEngine` calls `ExecutionService.execute_task(...)` to execute task steps.
- Clean separation: `ExecutionService` does not directly instantiate `HernessEngine`. Communication is routed through callbacks, return values, or the event bus.

### `api` ↔ `capabilities`
- Mutating endpoints invoke `CapabilityRegistry.invoke(capability_name, context, params)`.
- No direct database writes (e.g., via `runtime.memory` or `EvolutionEngine`) are allowed in `api.py`.
