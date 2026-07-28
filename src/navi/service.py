from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import sys
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from .defaults import DEFAULT_SERVICE_NAME

_T = TypeVar("_T")


@dataclass(frozen=True)
class ServiceUnit:
    name: str
    content: str
    path: Path


@dataclass(frozen=True)
class SystemdNotifier:
    """Minimal sd_notify client with no optional systemd dependency."""

    notify_socket: str = ""
    watchdog_interval_seconds: float = 0.0

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> SystemdNotifier:
        env = os.environ if environment is None else environment
        notify_socket = str(env.get("NOTIFY_SOCKET") or "")
        watchdog_pid = str(env.get("WATCHDOG_PID") or "").strip()
        if watchdog_pid and watchdog_pid != str(os.getpid()):
            return cls(notify_socket=notify_socket)
        try:
            watchdog_usec = max(0, int(str(env.get("WATCHDOG_USEC") or "0")))
        except ValueError:
            watchdog_usec = 0
        interval = watchdog_usec / 3_000_000.0 if watchdog_usec > 0 else 0.0
        return cls(
            notify_socket=notify_socket,
            watchdog_interval_seconds=interval,
        )

    def notify(self, payload: str) -> bool:
        if not self.notify_socket:
            return False
        address: str | bytes = self.notify_socket
        if self.notify_socket.startswith("@"):
            address = b"\0" + self.notify_socket[1:].encode()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
                client.connect(address)
                client.sendall(payload.encode())
        except OSError:
            return False
        return True

    def ready(self, status: str = "") -> bool:
        payload = "READY=1"
        if status:
            payload += f"\nSTATUS={status.replace(chr(10), ' ')[:500]}"
        return self.notify(payload)

    def stopping(self) -> bool:
        return self.notify("STOPPING=1")

    async def watchdog_loop(self) -> None:
        if self.watchdog_interval_seconds <= 0:
            return
        while True:
            self.notify("WATCHDOG=1")
            await asyncio.sleep(self.watchdog_interval_seconds)


async def run_with_systemd_watchdog(
    awaitable: Awaitable[_T],
    *,
    status: str,
    notifier: SystemdNotifier | None = None,
) -> _T:
    active = notifier or SystemdNotifier.from_environment()
    active.ready(status)
    watchdog = (
        asyncio.create_task(active.watchdog_loop())
        if active.watchdog_interval_seconds > 0
        else None
    )
    try:
        return await awaitable
    finally:
        if watchdog is not None:
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog
        active.stopping()


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
        "NotifyAccess=main\n"
        f"WorkingDirectory={project_dir}\n"
        f"{env_block}\n"
        f"ExecStart={python} -m navi.cli run\n"
        "Restart=on-failure\n"
        "RestartSec=5s\n"
        "WatchdogSec=90s\n"
        "TimeoutStopSec=30s\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def systemd_user_unit_path(name: str = DEFAULT_SERVICE_NAME) -> Path:
    return Path.home() / ".config" / "systemd" / "user" / name


def install_systemd_user_unit(
    *, project_dir: Path, navi_home: Path | None = None, name: str = DEFAULT_SERVICE_NAME
) -> ServiceUnit:
    path = systemd_user_unit_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = build_systemd_user_unit(project_dir=project_dir, navi_home=navi_home)
    path.write_text(content, encoding="utf-8")
    return ServiceUnit(name=name, content=content, path=path)
