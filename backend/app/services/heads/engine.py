"""Small read-only probes of a local engine: what it serves, how busy it is."""
from __future__ import annotations

import re
import time

import httpx

from app.services.endpoint_probe import probe_endpoint_url
from app.services.runtime_protocols import engine_root

SERVED_TTL_S = 20.0
_served_cache: dict[str, tuple[float, frozenset[str] | None]] = {}

_RUNNING_RE = re.compile(r"^vllm:num_requests_running(?:\{[^}]*\})?\s+([0-9.eE+-]+)\s*$", re.M)


async def served_models(endpoint: str, *, now: float | None = None) -> frozenset[str] | None:
    """Model ids the engine lists on /models, or None when it does not answer."""
    now = time.monotonic() if now is None else now
    cached = _served_cache.get(endpoint)
    if cached is not None and now - cached[0] < SERVED_TTL_S:
        return cached[1]
    result = await probe_endpoint_url(endpoint)
    models = frozenset(result.get("models") or []) if result.get("reachable") else None
    _served_cache[endpoint] = (now, models)
    return models


async def running_requests(endpoint: str) -> int | None:
    """``vllm:num_requests_running`` from /metrics — vLLM-family engines only.
    None when the metric is not there (other engines): then only the lock rule
    applies (spec §6.7 rule (a))."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{engine_root(endpoint)}/metrics")
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    values = [float(v) for v in _RUNNING_RE.findall(resp.text)]
    if not values:
        return None
    return int(sum(values))


def clear_cache() -> None:
    _served_cache.clear()
