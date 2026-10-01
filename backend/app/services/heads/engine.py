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
# on vLLM), SGLang, llama.cpp server (started with --metrics), TensorFold
# (gauge ``tensorfold:requests_running``; its ``requests_waiting`` and the
# ``tensorfold_health:*`` lifetime totals deliberately do NOT count). A new
# engine family is one more name here — never a check on a recipe slug.
RUNNING_REQUEST_METRICS: tuple[str, ...] = (
    "vllm:num_requests_running",
    "sglang:num_running_reqs",
    "llamacpp:requests_processing",
    "tensorfold:requests_running",
)
_RUNNING_RE = re.compile(
    r"^(?:" + "|".join(re.escape(name) for name in RUNNING_REQUEST_METRICS) + r")"
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
    root = engine_root(endpoint)
    try:
        async with httpx.AsyncClient(timeout=3.0, transport=_transport) as client:
            resp = await client.get(f"{root}/metrics")
            if resp.status_code == 200:
                values = [float(v) for v in _RUNNING_RE.findall(resp.text)]
                if values:
                    return int(sum(values)), None
                reason = "no running-requests metric"
            elif resp.status_code == 404:
                reason = "HTTP 404"
            else:
                return None, f"HTTP {resp.status_code}"
            # Fallback: an engine that says nothing usable on /metrics may
            # still report its load in a /health JSON (TensorFold:
            # ``requests_running`` and ``busy``).
            from_health = await _running_from_health(client, root)
    except httpx.HTTPError:
        return None, "unreachable"
    if from_health is not None:
        return from_health, None
    return None, reason


async def _running_from_health(client: httpx.AsyncClient, root: str) -> int | None:
    """``requests_running`` (int) or ``busy`` (bool → 1/0) from ``/health``;
    None when the engine has no such JSON. Never raises for a bad body."""
    try:
        resp = await client.get(f"{root}/health")
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        doc = resp.json()
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    running = doc.get("requests_running")
    if isinstance(running, (int, float)) and not isinstance(running, bool):
        return int(running)
    busy = doc.get("busy")
    if isinstance(busy, bool):
        return 1 if busy else 0
    return None


async def running_requests(endpoint: str) -> int | None:
    """Only the count of :func:`probe_running_requests` — None when unknown."""
    return (await probe_running_requests(endpoint))[0]


def clear_cache() -> None:
    _served_cache.clear()
