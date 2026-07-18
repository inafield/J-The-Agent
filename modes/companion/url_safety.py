"""SSRF guards for Companion ``fetch_url`` / local-dev allowlist."""

from __future__ import annotations

import ipaddress
import socket
import urllib.parse
from dataclasses import dataclass


@dataclass(frozen=True)
class UrlCheck:
    ok: bool
    url: str
    host: str
    port: int
    is_local: bool = False
    whitelisted: bool = False
    error: str | None = None
    suggestion: str | None = None


_LOCAL_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.",
        "127.0.0.1",
        "::1",
        "0.0.0.0",
        "[::1]",
    }
)


def normalize_allowed_ports(ports: list[int] | None) -> list[int]:
    cleaned: list[int] = []
    for port in ports or []:
        value = int(port)
        if 1 <= value <= 65535 and value not in cleaned:
            cleaned.append(value)
    return cleaned


def check_fetch_url(url: str, *, allowed_local_ports: list[int] | None = None) -> UrlCheck:
    """Validate a URL before network access.

    Public internet hosts are allowed (caller still asks the user).
    Loopback / private / link-local / reserved targets are blocked unless the
    host is an explicit loopback name and the port is on the allowlist.
    """

    raw = (url or "").strip()
    if not raw:
        return UrlCheck(ok=False, url=raw, host="", port=0, error="URL is empty")
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        return UrlCheck(
            ok=False,
            url=raw,
            host="",
            port=0,
            error="URL must start with http:// or https://",
        )
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return UrlCheck(ok=False, url=raw, host="", port=0, error="URL is missing a host")
    if parsed.username or parsed.password:
        return UrlCheck(
            ok=False,
            url=raw,
            host=host,
            port=0,
            error="URLs with embedded credentials are not allowed",
        )

    default_port = 443 if parsed.scheme == "https" else 80
    port = parsed.port or default_port
    allow = set(normalize_allowed_ports(allowed_local_ports))
    loopback_name = host in _LOCAL_HOSTNAMES or host.endswith(".localhost")

    try:
        addresses = _resolve_hosts(host)
    except OSError as exc:
        return UrlCheck(
            ok=False,
            url=raw,
            host=host,
            port=port,
            error=f"Could not resolve host: {exc}",
        )
    if not addresses:
        return UrlCheck(ok=False, url=raw, host=host, port=port, error="Host did not resolve")

    local_ips = [ip for ip in addresses if _is_blocked_target(ip)]
    if not local_ips:
        return UrlCheck(ok=True, url=raw, host=host, port=port, is_local=False)

    # Any resolution to a non-public address makes the target "local/internal".
    if loopback_name and port in allow and all(ip.is_loopback for ip in local_ips):
        return UrlCheck(
            ok=True,
            url=raw,
            host=host,
            port=port,
            is_local=True,
            whitelisted=True,
        )

    suggestion = None
    if loopback_name:
        suggestion = f"Allow this port with: ja allow-local {port}"
    return UrlCheck(
        ok=False,
        url=raw,
        host=host,
        port=port,
        is_local=True,
        error=(
            f"Blocked local/internal address {host}:{port} "
            f"({', '.join(str(ip) for ip in local_ips)}). "
            "Private, loopback, and link-local targets are denied by default."
        ),
        suggestion=suggestion,
    )


def _resolve_hosts(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    found: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    seen: set[str] = set()
    for info in infos:
        address = info[4][0]
        if address in seen:
            continue
        seen.add(address)
        found.append(ipaddress.ip_address(address))
    return found


def _is_blocked_target(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or getattr(ip, "is_site_local", False)
    )
