from __future__ import annotations

from navi.service import build_systemd_user_unit


def test_build_systemd_user_unit_uses_project_and_home(tmp_path):
    (tmp_path / "src").mkdir()
    navi_home = tmp_path / ".navi"

    unit = build_systemd_user_unit(project_dir=tmp_path, navi_home=navi_home)

    assert "Description=Navi active assistant" in unit
    assert f"WorkingDirectory={tmp_path}" in unit
    assert f"Environment=PYTHONPATH={tmp_path / 'src'}" in unit
    assert f"Environment=NAVI_HOME={navi_home}" in unit
    assert "ExecStart=" in unit
    assert "-m navi.cli run" in unit
    assert "Restart=always" in unit
