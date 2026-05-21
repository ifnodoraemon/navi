from __future__ import annotations

import tomllib
from pathlib import Path

import navi


def test_package_version_matches_project_metadata():
    data = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())

    assert data["project"]["version"] == navi.__version__
