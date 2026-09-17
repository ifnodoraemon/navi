from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from navi.config import write_default_config


@pytest.fixture
def valid_runtime_config(tmp_path: Path) -> Path:
    path = write_default_config(tmp_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw.setdefault("model", {})["api_key"] = "test-model-key"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    path.chmod(0o600)
    return path


@pytest.fixture(scope="session")
def _ambient_parameters_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("ambient-parameters")


@pytest.fixture(autouse=True)
def _isolated_ambient_dynamic_parameters(
    _ambient_parameters_home: Path,
) -> Iterator[None]:
    """Pin the ambient dynamic-parameter plane to a session-scoped scratch home.

    ``dynamic_parameter()`` falls back to ``ensure_home()`` (the project
    ``.navi``) when no home is ambient. Without this fixture, runtime-tunable
    accessors in state_graph would read *production* parameter drift during
    tests (and the seed sweep would write into the production store).
    """
    from navi import dynamic_parameters as dp

    dp.reset_ambient_registry()
    dp._AMBIENT_HOME = _ambient_parameters_home
    yield
    dp.reset_ambient_registry()


def pytest_configure(config: Any) -> None:
    loop = asyncio.new_event_loop()
    asyncio.get_event_loop_policy().set_event_loop(loop)
    config._navi_default_event_loop = loop


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    loop = getattr(session.config, "_navi_default_event_loop", None)
    if loop is not None and not loop.is_running() and not loop.is_closed():
        loop.close()
    asyncio.get_event_loop_policy().set_event_loop(None)
