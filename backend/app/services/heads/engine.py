"""Small read-only probes of a local engine: what it serves, how busy it is."""
from __future__ import annotations

import re
import time

import httpx

from app.services.endpoint_probe import probe_endpoint_url
from app.services.runtime_protocols import engine_root

SERVED_TTL_S = 20.0
_served_cache: dict[str, tuple[float, frozenset[str] | None]] = {}

# Requests in flight, per engine family: vLLM (and the EXL3 fork, which runs
# on vLLM), SGLang, llama.cpp server (started with --metrics).
_RUNNING_RE = re.compile(
    r"^(?:vllm:num_requests_running|sglang:num_running_reqs|llamacpp:requests_processing)"
    r"(?:\{[^}]*\})?\s+([0-9.eE+-]+)\s*$",
    re.M,
)
# Tests put an ``httpx.MockTransport`` here (conftest keeps them off the network).
_transport: httpx.AsyncBaseTransport | None = None


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


async def probe_running_requests(endpoint: str) -> tuple[int | None, str | None]:
    """Requests the engine is working on right now, from its ``/metrics``.

    ``(count, None)`` when the engine reports it; ``(None, reason)`` when the
    load is unknown — engine unreachable, an error status, or no known metric
    (e.g. an engine without Prometheus metrics)."""
    try:
        async with httpx.AsyncClient(timeout=3.0, transport=_transport) as client:
            resp = await client.get(f"{engine_root(endpoint)}/metrics")
    except httpx.HTTPError:
        return None, "unreachable"
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}"
    values = [float(v) for v in _RUNNING_RE.findall(resp.text)]
    if not values:
        return None, "no running-requests metric"
    return int(sum(values)), None


async def running_requests(endpoint: str) -> int | None:
    """Only the count of :func:`probe_running_requests` — None when unknown."""
    return (await probe_running_requests(endpoint))[0]


def clear_cache() -> None:
    _served_cache.clear()
