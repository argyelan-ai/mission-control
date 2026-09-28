"""Guard for endpoints that must only be called from this machine.

"Local-only" used to be a docstring claim, not a check: Caddy forwards every
``/api/*`` path to the backend, on :80 and on the TLS site, so a route without
auth was reachable by anyone who could reach Caddy. This dependency makes the
claim true inside the backend; the Caddyfiles additionally answer such paths
with 403 (defence in depth, same pattern as ``/api/v1/internal/*``).

A caller counts as local when BOTH hold:

1. **No proxy headers.** Behind Caddy the TCP peer is the Caddy container — a
   Docker-network address — and uvicorn's ``--proxy-headers`` then rewrites
   ``request.client.host`` to whatever Caddy saw. Neither says anything about
   locality. Caddy always sets ``X-Forwarded-For``, so its mere *presence*
   rejects the request. The header's value is never trusted; a direct caller
   that adds one only locks itself out.
2. **The peer is loopback or on one of this container's own subnets.** A host
   process using the published ``127.0.0.1:8000`` port arrives from the bridge
   gateway, a sibling container from its container address — both on-link
   (measured live, see ``app/proxy_trust.py``). The subnets come from
   ``/proc/net/route``, so nothing is hardcoded; without a routing table
   (bare-metal dev run) only loopback is local.
"""
from __future__ import annotations

import ipaddress
from functools import lru_cache

from fastapi import HTTPException, Request

from app.proxy_trust import _PROC_NET_ROUTE, parse_proc_net_route

_PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "forwarded", "x-real-ip")

_LOOPBACK = (ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128"))


def _read_route_table() -> str | None:
    try:
        with open(_PROC_NET_ROUTE, encoding="ascii") as handle:
            return handle.read()
    except OSError:
        return None


@lru_cache(maxsize=1)
def local_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Loopback plus every subnet this container is directly attached to."""
    route_text = _read_route_table()
    if not route_text:
        return _LOOPBACK
    networks, _gateways = parse_proc_net_route(route_text)
    return _LOOPBACK + tuple(networks)


def is_local_request(request: Request) -> bool:
    if any(name in request.headers for name in _PROXY_HEADERS):
        return False
    if not request.client:
        return False
    try:
        peer = ipaddress.ip_address(request.client.host)
    except ValueError:
        return False
    if isinstance(peer, ipaddress.IPv6Address) and peer.ipv4_mapped:
        peer = peer.ipv4_mapped
    return any(peer in net for net in local_networks() if net.version == peer.version)


def require_local_caller(request: Request) -> None:
    """FastAPI dependency: 403 unless the request comes from this machine."""
    if not is_local_request(request):
        raise HTTPException(status_code=403, detail="Local callers only")
