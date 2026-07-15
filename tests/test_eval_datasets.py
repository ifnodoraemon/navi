from pathlib import Path

import pytest

from navi.evals import (
    load_claw_eval_dataset,
    load_connector_journey_eval_dataset,
    load_daily_journey_eval_dataset,
)


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "name",
    [
        "daily_journeys.yaml",
        "journeys_ultra_long.yaml",
        "public_agent_journeys.yaml",
        "regression_journeys.yaml",
        "user_journeys.yaml",
    ],
)
def test_daily_journey_dataset_uses_current_contract(name: str) -> None:
    load_daily_journey_eval_dataset(ROOT / "evals" / name)


@pytest.mark.parametrize("name", ["claw_fast.yaml", "claw_navi.yaml"])
def test_claw_dataset_uses_current_contract(name: str) -> None:
    load_claw_eval_dataset(ROOT / "evals" / name)


def test_connector_dataset_uses_current_contract() -> None:
    load_connector_journey_eval_dataset(ROOT / "evals" / "connector_journeys.yaml")
