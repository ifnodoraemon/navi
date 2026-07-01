"""Core tool handlers."""
from __future__ import annotations
from pathlib import Path
from urllib.parse import urlparse


def _is_browser_url(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    return _is_public_http_host(host, port=parsed.port)


def _is_blocked_http_host(host: str) -> bool:
    try:
        import ipaddress

        ip = ipaddress.ip_address(host)
        if ip in ipaddress.ip_network('198.18.0.0/15'):
            return False
        return (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        )
    except ValueError:
        if host in {"localhost", "host.docker.internal"}:
            return True
    return False


def _is_public_http_host(host: str, *, port: int | None = None) -> bool:
    if _is_blocked_http_host(host):
        return False
    try:
        import socket

        for family, _, _, _, sockaddr in socket.getaddrinfo(host, port or 443):
            del family
            address = str(sockaddr[0])
            if _is_blocked_http_host(address):
                return False
    except OSError:
        return True
    return True


