"""Local dev port proactive event detector."""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any

from ..daemon_types import (
    DEFAULT_DEV_PORTS,
    DEFAULT_PORT_PROBE_TIMEOUT_SECONDS,
    EventBatch,
    ProjectEventContext,
    ProactiveEvent,
)

logger = logging.getLogger("navi.daemon")


class PortEventDetector:
    """Detects dev server ports coming online or going offline."""

    async def __call__(self, context: ProjectEventContext) -> EventBatch:
        return await self.detect(context)

    async def detect(self, context: ProjectEventContext) -> EventBatch:
        events: list[ProactiveEvent] = []
        state_updates: dict[str, Any] = {}
        project_data = context.project_data
        dev_ports = project_data.get("dev_ports", [])
        if not dev_ports and context.use_default_ports:
            dev_ports = DEFAULT_DEV_PORTS
        if not dev_ports:
            return events, state_updates

        normalized_ports: list[int] = []
        for port in dev_ports:
            try:
                normalized_ports.append(int(port))
            except (TypeError, ValueError):
                logger.warning("Ignoring invalid dev port value: %r", port)

        async def probe_port_family(port: int, family: socket.AddressFamily) -> bool:
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection("localhost", port, family=family),
                    timeout=self._port_probe_timeout(project_data),
                )
                writer.close()
                await writer.wait_closed()
                return True
            except OSError:
                return False
            except asyncio.TimeoutError:
                return False

        async def probe_port(port: int) -> tuple[int, bool]:
            probe_results = await asyncio.gather(
                probe_port_family(port, socket.AF_INET),
                probe_port_family(port, socket.AF_INET6),
            )
            return port, any(probe_results)

        probe_results = await asyncio.gather(*(probe_port(port) for port in normalized_ports))
        for port, is_active in probe_results:
            port_key = f"port_active_{port}"
            was_active = project_data.get(port_key, False)
            if was_active and not is_active:
                events.append(
                    ProactiveEvent(
                        source="event_port",
                        message=f"Local service on port {port} went offline.",
                        facts={
                            "kind": "port_went_offline",
                            "port": port,
                            "previous_active": was_active,
                            "active": is_active,
                        },
                        state_updates={port_key: is_active},
                        suppressed_state_updates={port_key: is_active},
                    )
                )
                continue

            if is_active != was_active:
                state_updates[port_key] = is_active
        return events, state_updates

    @staticmethod
    def _port_probe_timeout(project_data: dict[str, Any]) -> float:
        raw = project_data.get(
            "port_probe_timeout_seconds", DEFAULT_PORT_PROBE_TIMEOUT_SECONDS
        )
        try:
            return max(0.5, min(float(raw), 10.0))
        except (TypeError, ValueError):
            return DEFAULT_PORT_PROBE_TIMEOUT_SECONDS
