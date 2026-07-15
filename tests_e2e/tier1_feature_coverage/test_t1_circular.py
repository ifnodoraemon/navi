"""Package-import and current composition-root smoke tests."""

from __future__ import annotations

import importlib
import os
import pkgutil
import subprocess
import sys
from pathlib import Path


def _run_import_order(order: list[str]) -> None:
    src_dir = str(Path(__file__).resolve().parents[2] / "src")
    code = "\n".join(f"import {module}" for module in order)
    env = dict(os.environ, PYTHONPATH=src_dir)
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_t1_current_composition_roots_are_import_order_independent() -> None:
    modules = ["navi.control_plane", "navi.capabilities", "navi.state_graph"]
    _run_import_order(modules)
    _run_import_order(list(reversed(modules)))


def test_t1_package_import_all() -> None:
    src_dir = Path(__file__).resolve().parents[2] / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))

    import navi

    failures: list[str] = []
    for module_info in pkgutil.walk_packages(navi.__path__, navi.__name__ + "."):
        try:
            importlib.import_module(module_info.name)
        except Exception as exc:
            failures.append(f"{module_info.name}: {exc}")

    assert not failures, "Failed package imports:\n" + "\n".join(failures)
