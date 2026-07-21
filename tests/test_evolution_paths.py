from __future__ import annotations

import pytest

from navi.evolution import _safe_evolution_target_path


def test_evolution_target_path_is_confined_to_managed_directory(tmp_path):
    target = _safe_evolution_target_path(
        tmp_path,
        subdir="specs",
        target_id="planner-policy-v2",
        suffix=".yaml",
    )
    assert target == (tmp_path / "specs" / "planner-policy-v2.yaml").resolve()

    for unsafe in ("../escape", "nested/name", "nested\\name", ".."):
        with pytest.raises(ValueError, match="safe name"):
            _safe_evolution_target_path(
                tmp_path,
                subdir="specs",
                target_id=unsafe,
                suffix=".yaml",
            )
