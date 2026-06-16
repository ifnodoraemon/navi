# E2E Test Infra: Navi Refactoring Project

## Test Philosophy
- Opaque-box, requirement-driven. No dependency on implementation design.
- Methodology: Category-Partition + BVA + Pairwise + Workload Testing.

## Feature Inventory
| # | Feature | Source (requirement) | Tier 1 | Tier 2 | Tier 3 |
|---|---------|---------------------|:------:|:------:|:------:|
| 1 | Circular Dependency Resolution | ORIGINAL_REQUEST §R1 | 6 | 4 | ✓ |
| 2 | Capabilities Decomposition | ORIGINAL_REQUEST §R2 | 6 | 4 | ✓ |
| 3 | API Boundary Standardization | ORIGINAL_REQUEST §R3 | 6 | 4 | ✓ |
| 4 | Event-Driven Background Task Orchestration | ORIGINAL_REQUEST §R4 | 6 | 4 | ✓ |
| 5 | Abstracted Prompt Layers | ORIGINAL_REQUEST §R5 | 6 | 4 | ✓ |

## Test Architecture
- **Test Runner**: pytest, running inside a virtual environment.
- **Location**: All E2E test files are organized under `tests_e2e/`.
- **Pass/Fail Semantics**: All test functions assert clean module imports, correct API status codes (e.g., 200 for allowed operations, 403/409 for blocked ones), expected database states, and asynchronous completion within predefined timeouts.
- **Directory Layout**:
  ```
  tests_e2e/
  ├── conftest.py                    # Shared fixtures: clients, mock engines, temp directories, hooks
  ├── pytest.ini                     # E2E-specific configuration (markers, asyncio settings)
  ├── tier1_feature_coverage/        # Tier 1: Core feature coverage (30 tests)
  ├── tier2_boundary_corner/         # Tier 2: Boundary and corner cases (20 tests)
  ├── tier3_cross_feature/           # Tier 3: Cross-feature interactions (5 tests)
  └── tier4_real_world/              # Tier 4: Real-world user journeys (5 tests)
  ```

## Real-World Application Scenarios (Tier 4)
| # | Scenario | Features Exercised | Complexity |
|---|----------|--------------------|------------|
| 1 | `test_t4_multi_agent_delegation_audit_journey` | F2, F3, F4 | High |
| 2 | `test_t4_weixin_connector_notification_cycle` | F3, F5 | Medium |
| 3 | `test_t4_admin_system_evolution_migration` | F2, F3, F4 | High |
| 4 | `test_t4_fault_recovery_and_engine_shutdown` | F1, F4 | High |
| 5 | `test_t4_low_risk_auto_execution_journey` | F2, F4, F5 | Medium |

## Coverage Thresholds
- **Tier 1 (Feature Coverage)**: >=5 tests per feature. Total 30 tests.
- **Tier 2 (Boundary/Corner cases)**: >=5 tests per feature. Total 20 tests.
- **Tier 3 (Cross-feature interactions)**: >=5 tests total.
- **Tier 4 (Real-world scenarios)**: >=5 tests total.
- **Total Tests**: 60 tests.
