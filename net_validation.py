"""Network validation: which hosts and URLs this server may be pointed at.

Extracted from app.py in 0.10.0. Every decision about whether an address is
reachable now lives in one file, which is the point: the SSRF surface was
thirteen names spread across three regions of a 5900-line module, and rules.md
section 8 asks for exactly this to be auditable.

The cluster was measured rather than chosen. Starting from the eight functions
that decide whether a host or URL may be used, and closing over the module
constants they read, gives these thirteen names -- and the only thing they
needed from app.py was a logger. A group that references nothing else is a seam;
one that does is a cut.
"""
from __future__ import annotations

import logging
import re
import socket
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network
from typing import Final

from fastapi import HTTPException, Request

import config

_log = logging.getLogger("wc.app")


# Hostname regex: labels, full IPv4, or bracketed IPv6.
_HOST_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|"
    r"\[[0-9A-Fa-f:]+\]|[0-9A-Fa-f:]+)$"
)
# Blocked private / reserved IP ranges for SSRF protection.
_BLOCKED_NETS: Final[list[IPv4Network | IPv6Network]] = [
    ip_network("0.0.0.0/8"),
    ip_network("10.0.0.0/8"),
    ip_network("100.64.0.0/10"),  # CG-NAT (includes the Tailscale range)
    ip_network("127.0.0.0/8"),  # Loopback
    ip_network("169.254.0.0/16"),  # Link-local
    ip_network("172.16.0.0/12"),
    ip_network("192.0.0.0/24"),  # IETF
    ip_network("192.0.2.0/24"),  # TEST-NET-1
    ip_network("192.88.99.0/24"),  # 6to4 relay
    ip_network("192.168.0.0/16"),
    ip_network("198.18.0.0/15"),
    ip_network("198.51.100.0/24"),  # TEST-NET-2
    ip_network("203.0.113.0/24"),  # TEST-NET-3
    ip_network("224.0.0.0/4"),  # Multicast
    ip_network("240.0.0.0/4"),  # Reserved
    ip_network("::1/128"),  # IPv6 loopback
    ip_network("fc00::/7"),  # ULA
    ip_network("fe80::/10"),  # IPv6 link-local
]


def _parse_allow_nets() -> list[IPv4Network | IPv6Network]:
    """Ranges that stay reachable despite _BLOCKED_NETS.

    An AI machine is normally *meant* to live on the operator's own network:
    a tailnet peer (100.64.0.0/10), a LAN box (RFC1918), or the local proxy on
    loopback. Blanket-blocking those rejects the product's intended topology,
    and the endpoints that accept a host are admin-gated or authenticated, so
    the operator is not the threat here.

    What stays blocked is what an operator would never legitimately target:
    169.254.0.0/16 (link-local, including the cloud metadata endpoint),
    0.0.0.0/8, the TEST-NET and benchmark ranges, multicast, reserved space
    and IPv6 ULA / link-local. Tighten with WC_SSRF_ALLOW_NETS, which replaces
    this list wholesale.
    """
    raw = config._str(
        "WC_SSRF_ALLOW_NETS",
        "100.64.0.0/10,127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16",
    )
    nets: list[IPv4Network | IPv6Network] = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            nets.append(ip_network(item))
        except ValueError:
            _log.warning("ignoring invalid WC_SSRF_ALLOW_NETS entry: %s", item)
    return nets


_ALLOWED_NETS: Final[list[IPv4Network | IPv6Network]] = _parse_allow_nets()


def _trusted_proxies() -> list[IPv4Network | IPv6Network]:
    """Parse WC_TRUSTED_PROXIES into networks. Empty (the default) means none."""
    raw = config._str("WC_TRUSTED_PROXIES", "") or ""
    nets: list[IPv4Network | IPv6Network] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ip_network(part, strict=False))
        except ValueError:
            _log.warning("ignoring invalid WC_TRUSTED_PROXIES entry %r", part)
    return nets


def _client_ip(request: Request) -> str:
    """Return the client address to attribute a request to.

    Uses the forwarded header **only** when the immediate peer is a configured
    trusted proxy. Reading it unconditionally would be worse than ignoring it:
    with the app exposed directly -- which is how it runs today, uvicorn holding
    :443 itself -- any client could rotate X-Real-IP and walk straight through
    the login rate limit.
    """
    peer = getattr(getattr(request, "client", None), "host", None) or "unknown"
    trusted = _trusted_proxies()
    if not trusted or peer == "unknown":
        return peer
    try:
        peer_addr = ip_address(peer)
    except ValueError:
        return peer
    if not any(peer_addr in net for net in trusted):
        return peer
    headers = getattr(request, "headers", None) or {}
    for header in ("x-real-ip", "x-forwarded-for"):
        value = headers.get(header) or ""
        # X-Forwarded-For is a chain; the left-most entry is the origin client.
        candidate = value.split(",")[0].strip()
        if not candidate:
            continue
        try:
            ip_address(candidate)
        except ValueError:
            continue
        return candidate
    return peer


def _is_private_ip(host: str) -> bool:
    """Return True if *host* is a blocked address and not explicitly allowed."""
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]  # strip IPv6 brackets for parsing
    try:
        addr = ip_address(host)
    except ValueError:
        return False
    if any(addr in net for net in _ALLOWED_NETS):
        return False
    return any(addr in net for net in _BLOCKED_NETS)


def _validate_host(host: str) -> str:
    """Validate *host* and confirm it does not resolve to a blocked address.

    Returns the resolved IP. Every call site persists a user-supplied host
    under a comment claiming SSRF protection, but this only pattern-matched
    the string and never resolved anything, so nothing was ever blocked. It
    now applies the blocklist at the point the value enters the system.
    """
    if not host or not isinstance(host, str) or not _HOST_PATTERN.match(host):
        raise HTTPException(
            status_code=400, detail="Enter a valid hostname or IP address"
        )
    return _resolve_host(host)


def _resolve_host(host: str) -> str:
    """Resolve *host* to an IP and check against the blocklist.

    Returns the resolved IPv4 or IPv6 string so that asyncio.open_connection
    can connect directly (avoiding a second DNS lookup).
    Raises HTTPException on private IPs.
    """
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except (socket.gaierror, OSError):
        raise HTTPException(status_code=400, detail="DNS resolution failed")
    # Pick the first result (address family, socktype, proto, canonname, sockaddr).
    for info in infos:
        ip = info[4][0]
        if _is_private_ip(ip):
            _log.warning("blocked outbound connection to %s -> %s", host, ip)
            raise HTTPException(
                status_code=403, detail="Internal hosts are not allowed"
            )
        return ip
    raise HTTPException(status_code=400, detail="DNS resolution failed")


_HOST_PATTERN_LOCAL = _HOST_PATTERN
# Allowed URL schemes for base_url validation.
_ALLOWED_URL_SCHEMES = {"http", "https"}


def _base_url_host(base_url: str) -> str:
    """Return the host portion of *base_url*, for host-level checks.

    Raises HTTPException if the URL is not a well-formed http/https URL.
    """
    # Must have a scheme.
    if "://" not in base_url:
        raise HTTPException(status_code=400, detail="Enter a valid base URL")
    scheme, rest = base_url.split("://", 1)
    if scheme.lower() not in _ALLOWED_URL_SCHEMES:
        raise HTTPException(
            status_code=400, detail="Only http and https schemes allowed"
        )
    # Extract the host:port before any path.
    host_port = rest.split("/")[0]
    # Strip port for host-only validation.
    if ":" in host_port and not host_port.startswith("["):
        host_part = host_port.rsplit(":", 1)[0]
    else:
        host_part = host_port
    if not _HOST_PATTERN_LOCAL.fullmatch(host_part):
        raise HTTPException(status_code=400, detail="Enter a valid base URL")
    return host_part


def _validate_base_url(base_url: str) -> str:
    """Validate *base_url* and return it unchanged (minus surrounding space).

    This is a validator, not a transformer: the caller persists the value it
    passes in, so returning only the host would silently discard the scheme,
    port and path. Use :func:`_base_url_host` when the host alone is wanted.
    """
    base_url = base_url.strip()
    if len(base_url) > 500:
        raise HTTPException(status_code=400, detail="Base URL is too long")
    _base_url_host(base_url)  # raises on a malformed or non-http(s) URL
    return base_url
