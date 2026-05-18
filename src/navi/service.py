from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ServiceUnit:
    name: str
    content: str
    path: Path


def build_systemd_user_unit(*, project_dir: Path, navi_home: Path | None = None) -> str:
    project_dir = project_dir.resolve()
    src_dir = project_dir / "src"
    python = Path(sys.executable)
    env_lines = []
    if src_dir.exists():
        env_lines.append(f"Environment=PYTHONPATH={src_dir}")
    if navi_home is not None:
        env_lines.append(f"Environment=NAVI_HOME={navi_home.resolve()}")
    env_block = "\n".join(env_lines)
    return (
        "[Unit]\n"
        "Description=Navi active assistant\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={project_dir}\n"
        f"{env_block}\n"
        f"ExecStart={python} -m navi.cli run\n"
        "Restart=always\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def systemd_user_unit_path(name: str = "navi.service") -> Path:
    return Path.home() / ".config" / "systemd" / "user" / name


def install_systemd_user_unit(*, project_dir: Path, navi_home: Path | None = None, name: str = "navi.service") -> ServiceUnit:
    path = systemd_user_unit_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = build_systemd_user_unit(project_dir=project_dir, navi_home=navi_home)
    path.write_text(content, encoding="utf-8")
    return ServiceUnit(name=name, content=content, path=path)
