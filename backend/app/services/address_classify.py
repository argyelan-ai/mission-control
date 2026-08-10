"""Address classification — Tailscale vs. LAN vs. public.

Root cause of the live incident this module fixes: a runtime's HTTP endpoint
was built from ``Host.ssh_host`` (services/runtime_manager._host_ip), and
whichever address an operator entered there — LAN IP or Tailscale IP — became
the endpoint every agent talks to. SSH from the backend container tolerates a
LAN IP just fine; an HTTP call from a *host* agent (launchd/tmux on the Mac)
does not, because a Tailscale /32 route on that machine hijacks the box's LAN
IP (see memory: spark-tailscale-route-hijack-host-agents). The fix is not
"always prefer Tailscale" in the abstract — it is "when both addresses are
known for a box, the endpoint MUST use the Tailscale one", which needs a way
to tell the two apart first.

Three checks, all pure and exception-free:
  * ``classify_address``  — what kind of address is this?
  * ``suggest_endpoint_fix`` — given a failing endpoint and a host's known
    Tailscale address, what should the endpoint be instead?
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

# Tailscale's CGNAT range for its stable IPv4 addresses.
# https://tailscale.com/kb/1015/100.x-addresses
_TAILSCALE_V4_NET = ipaddress.ip_network("100.64.0.0/10")
# Tailscale's IPv6 ULA range. https://tailscale.com/kb/1481/ipv6
_TAILSCALE_V6_NET = ipaddress.ip_network("fd7a:115c:a1e0::/48")

CLASS_TAILSCALE = "tailscale"
CLASS_LAN = "lan"
CLASS_PUBLIC = "public"
CLASS_UNKNOWN = "unknown"


def extract_host(value: str | None) -> str:
    """Pulls the bare host/IP out of a URL, a ``host:port`` pair, an IPv6
    literal (bracketed, with or without a port), or a bare address.

    Best-effort and exception-free: anything that doesn't parse cleanly is
    returned as-is (stripped) rather than raising, so callers never have to
    guard this with a try/except.
    """
    if not value:
        return ""
    v = value.strip()
    if "://" in v:
        # urlparse.hostname handles IPv6 brackets, strips port + path, and
        # lower-cases — exactly what every other caller here wants.
        return urlparse(v).hostname or ""
    if v.startswith("["):
        end = v.find("]")
        if end != -1:
            return v[1:end]
    # "host:port" — but a bare (unbracketed) IPv6 address has more than one
    # colon, so this must not fire for those; they fall through to `return v`
    # and classify_address() parses them as IPv6 directly.
    if v.count(":") == 1:
        host, _, _port = v.partition(":")
        return host
    return v


def classify_address(address: str | None) -> str:
    """``'tailscale' | 'lan' | 'public' | 'unknown'`` — never raises.

    A ``*.ts.net`` hostname (Tailscale's MagicDNS name) is classified without
    a DNS lookup — resolving it would turn a pure function into a network
    call and a flaky one at that. Any other non-IP hostname is ``'unknown'``:
    we genuinely cannot say whether it's LAN or public.
    """
    host = extract_host(address)
    if not host:
        return CLASS_UNKNOWN
    if host.lower().endswith(".ts.net"):
        return CLASS_TAILSCALE
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return CLASS_UNKNOWN
    if isinstance(ip, ipaddress.IPv4Address):
        if ip in _TAILSCALE_V4_NET:
            return CLASS_TAILSCALE
        return CLASS_LAN if ip.is_private else CLASS_PUBLIC
    if ip in _TAILSCALE_V6_NET:
        return CLASS_TAILSCALE
    return CLASS_LAN if ip.is_private else CLASS_PUBLIC


def is_tailscale(address: str | None) -> bool:
    return classify_address(address) == CLASS_TAILSCALE


def is_lan(address: str | None) -> bool:
    return classify_address(address) == CLASS_LAN


def preferred_endpoint_host(
    ssh_host: str | None, tailscale_host: str | None
) -> str | None:
    """Which address should a runtime endpoint on this box use?

    A validated Tailscale address always wins when one is on file — that is
    the whole point of recording it. Falls back to ``ssh_host`` (today's
    behaviour, unchanged) when no Tailscale address is known, or when the
    value entered as ``tailscale_host`` doesn't actually look like one (a
    typo'd LAN IP there must not silently become the endpoint).
    """
    if tailscale_host and classify_address(tailscale_host) == CLASS_TAILSCALE:
        return tailscale_host
    return ssh_host


def suggest_endpoint_fix(endpoint: str | None, tailscale_host: str | None) -> str | None:
    """Rewrites ``endpoint`` to use ``tailscale_host`` when the endpoint is a
    LAN address and ``tailscale_host`` is a known-good Tailscale address for
    the same box. Returns ``None`` when there's nothing to suggest:
      * the endpoint isn't a LAN address (nothing to fix), or
      * no Tailscale address is known for this host (not every box has one).
    """
    if not endpoint or not tailscale_host:
        return None
    lan_host = extract_host(endpoint)
    if not lan_host or classify_address(lan_host) != CLASS_LAN:
        return None
    if classify_address(tailscale_host) != CLASS_TAILSCALE:
        return None
    return endpoint.replace(lan_host, tailscale_host, 1)
