from __future__ import annotations

from pathlib import Path

import navi
from navi.api import create_app

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility path.
    import tomli as tomllib


def test_package_version_matches_project_metadata():
    data = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())

    assert data["project"]["version"] == navi.__version__


def test_api_version_uses_package_version(tmp_path):
    assert create_app(tmp_path).version == navi.__version__
