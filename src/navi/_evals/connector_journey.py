"""Connector journey eval dataset."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import yaml

from ..connector_registry import get_connector_adapter


def load_connector_journey_eval_dataset(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    data = {} if loaded is None else loaded
    if not isinstance(data, dict):
        raise ValueError("connector journey eval dataset must be a mapping")
    connector = str(data.get("connector") or "").strip()
    if not connector:
        raise ValueError("connector journey eval dataset must declare connector")
    adapter = get_connector_adapter(connector)
    if adapter is None or adapter.load_journey_eval_dataset is None:
        raise ValueError(f"connector journey eval is not available for {connector!r}")
    return adapter.load_journey_eval_dataset(path)


async def run_connector_journey_eval_dataset(
    *,
    home: Path,
    project_dir: Path,
    dataset: Path,
    timeout_seconds: float = 30.0,
) -> list[Any]:
    loaded = load_connector_journey_eval_dataset(dataset)
    connector = str(loaded.get("connector") or "").strip()
    adapter = get_connector_adapter(connector)
    if adapter is None or adapter.run_journey_eval_dataset is None:
        raise ValueError(f"connector journey eval is not available for {connector!r}")
    return await asyncio.wait_for(
        adapter.run_journey_eval_dataset(home, project_dir, dataset, timeout_seconds),
        timeout=timeout_seconds,
    )
