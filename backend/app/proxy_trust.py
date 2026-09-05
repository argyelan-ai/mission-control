"""Compute uvicorn's ``forwarded_allow_ips`` trust list at container start.

Why this exists (PR #404 review, HOCH-1)
----------------------------------------
Uvicorn only rewrites ``request.client.host`` from ``X-Forwarded-For`` when the
*connecting* peer is in ``forwarded_allow_ips``. Behind Caddy the peer is the
Caddy container, whose IP changes on every recreate — so the original fix used
``--forwarded-allow-ips=*`` and justified it with "backend publishes no host
port". That justification was wrong: ``docker-compose.yml`` publishes
``127.0.0.1:8000:8000`` (the host agents need it). With ``*`` every process on
the host can therefore forge ``X-Forwarded-For`` and land in someone else's
rate-limit bucket (login lockout, slowapi buckets, IP logs).

What we do instead
------------------
Trust *the container network, minus the host*. Measured inside the running
backend container:

* traffic from a host process through the published port arrives from the
  bridge **gateway** address (e.g. ``172.18.0.1``),
* traffic from a sibling container (Caddy) arrives from a normal container
  address (e.g. ``172.18.0.5``).

So the trust list is "every on-link subnet of this container, with the gateway
addresses cut out". That is derived from ``/proc/net/route`` at startup, which
means it survives a network recreate with a different subnet and a Caddy
recreate with a different IP — no hardcoded CIDR, nothing to keep in sync.

Docker assigns the first usable address of a bridge subnet to the gateway, so
the gateway is ``network_address + 1``; default-route gateways are cut as well,
belt and braces. Loopback is deliberately not trusted: nothing legitimately
proxies to us from inside our own container.

Uvicorn accepts a comma-separated list of IPs *and* CIDR networks here
(``uvicorn.middleware.proxy_headers._TrustedHosts``, uvicorn >= 0.30), which is
what makes "subnet minus one address" expressible at all.
"""
from __future__ import annotations

import ipaddress
from typing import Iterable

_PROC_NET_ROUTE = "/proc/net/route"


def _hex_le_to_ipv4(value: str) -> ipaddress.IPv4Address:
    """Decode a little-endian hex word from /proc/net/route into an address.

    ``000012AC`` -> ``172.18.0.0`` (bytes are reversed on little-endian hosts,
    which is every platform MC runs on).
    """
    raw = int(value, 16)
    return ipaddress.IPv4Address(raw.to_bytes(4, "little"))


def parse_proc_net_route(text: str) -> tuple[list[ipaddress.IPv4Network], list[ipaddress.IPv4Address]]:
    """Split a /proc/net/route dump into (on-link networks, default gateways).

    On-link routes are the ones with no gateway (``Gateway == 0``) and a
    non-empty mask — those are the subnets this container is directly attached
    to. Rows with a gateway and an all-zero destination are default routes; we
    keep their gateway addresses so they can be cut out of the trust list too.
    """
    networks: list[ipaddress.IPv4Network] = []
    gateways: list[ipaddress.IPv4Address] = []

    for line in text.splitlines()[1:]:  # first line is the header
        fields = line.split()
        if len(fields) < 8:
            continue
        try:
            destination = _hex_le_to_ipv4(fields[1])
            gateway = _hex_le_to_ipv4(fields[2])
            mask = _hex_le_to_ipv4(fields[7])
        except (ValueError, OverflowError):
            continue

        if int(gateway) != 0:
            gateways.append(gateway)
            continue

        if int(mask) == 0:
            # A default route without a gateway carries no subnet information.
            continue

        try:
            networks.append(ipaddress.ip_network(f"{destination}/{mask}", strict=False))
        except ValueError:
            continue

    return networks, gateways


def _excluded_addresses(
    networks: Iterable[ipaddress.IPv4Network],
    default_gateways: Iterable[ipaddress.IPv4Address],
) -> set[ipaddress.IPv4Address]:
    """Addresses that must NOT be trusted: every bridge gateway we can name."""
    excluded = set(default_gateways)
    for net in networks:
        if net.num_addresses >= 2:
            # Docker hands the first usable address to the bridge gateway —
            # which is where host traffic through a published port comes from.
            excluded.add(net.network_address + 1)
    return excluded


def compute_trusted_hosts(route_text: str) -> str | None:
    """Build the ``forwarded_allow_ips`` value from a /proc/net/route dump.

    Returns a comma-separated list of CIDR networks covering every attached
    subnet except the gateway addresses, or ``None`` when the routing table
    yields nothing usable (the caller then falls back loudly).
    """
    networks, default_gateways = parse_proc_net_route(route_text)
    if not networks:
        return None

    excluded = _excluded_addresses(networks, default_gateways)

    trusted: list[ipaddress.IPv4Network] = []
    for net in networks:
        remaining = [net]
        for address in sorted(excluded):
            if address not in net:
                continue
            expanded: list[ipaddress.IPv4Network] = []
            for candidate in remaining:
                if address in candidate:
                    expanded.extend(candidate.address_exclude(ipaddress.ip_network(f"{address}/32")))
                else:
                    expanded.append(candidate)
            remaining = expanded
        trusted.extend(remaining)

    if not trusted:
        return None

    return ",".join(str(net) for net in sorted(trusted, key=lambda n: (int(n.network_address), n.prefixlen)))


def main() -> int:
    """Print the trust list on stdout; print nothing and fail on any problem.

    Called by ``backend/docker-entrypoint.sh``; the entrypoint falls back to
    ``*`` (today's behaviour) with a warning when this prints nothing, so a
    surprise in the routing table can never silently *reduce* trust and bring
    back the "everyone shares one rate-limit bucket" bug.
    """
    try:
        with open(_PROC_NET_ROUTE, encoding="ascii") as handle:
            route_text = handle.read()
    except OSError:
        return 1

    value = compute_trusted_hosts(route_text)
    if not value:
        return 1

    print(value)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via the entrypoint
    raise SystemExit(main())
