"""Which wire protocols a runtime REALLY serves — probed, not guessed.

``harness_compat.runtime_protocol`` classifies a runtime row by its type:
one protocol per runtime. That is stricter than reality: a vLLM engine also
serves the Anthropic route ``/v1/messages`` (live probe 2026-09-23: 400 on an
empty body, not 404 — also the EXL3 GLM engine), so Claude Code can talk to
it directly, without a proxy.

This module is the shared building block (docs/specs/head-launcher.md §4,
§6.6): ``runtime_protocols(runtime) -> set[str]`` = the classified protocol
plus ``anthropic`` when the engine answers the route probe. Heads use it now;
the paused fleet matrix (ADR-056) stays unchanged and can adopt it later.

The probe runs per runtime, never per engine family, and is cached.
"""
from __future__ import annotations

import logging
import time

import httpx

from app.models.runtime import Runtime
from app.services.harness_compat import runtime_protocol

logger = logging.getLogger(__name__)

PROBE_TTL_S = 600.0
_PROBE_TIMEOUT_S = 4.0

# (slug, endpoint) → (checked_at, serves_anthropic)
_cache: dict[tuple[str, str], tuple[float, bool]] = {}


def engine_root(endpoint: str) -> str:
    """``http://host:8000/v1`` → ``http://host:8000`` (the Anthropic base URL)."""
    base = (endpoint or "").rstrip("/")
    return base[: -len("/v1")] if base.endswith("/v1") else base


async def probe_anthropic_route(endpoint: str) -> bool:
    """POST an empty body to ``/v1/messages``: 400/405/422 = route exists,
    404 (or no answer) = it does not. Never sends a prompt."""
    url = f"{engine_root(endpoint)}/v1/messages"
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_S) as client:
            resp = await client.post(url, json={})
    except httpx.HTTPError as exc:
        logger.info("anthropic route probe %s failed: %s", url, exc.__class__.__name__)
        return False
    return resp.status_code in (400, 405, 422)


async def runtime_protocols(runtime: Runtime | None, *, now: float | None = None) -> set[str]:
    proto = runtime_protocol(runtime)
    if proto is None or runtime is None:
        return set()
    protocols = {proto}
    if proto != "openai" or not runtime.endpoint:
        return protocols
    key = (runtime.slug, runtime.endpoint)
    now = time.monotonic() if now is None else now
    cached = _cache.get(key)
    if cached is not None and now - cached[0] < PROBE_TTL_S:
        serves = cached[1]
    else:
        serves = await probe_anthropic_route(runtime.endpoint)
        _cache[key] = (now, serves)
    if serves:
        protocols.add("anthropic")
    return protocols


def clear_cache() -> None:
    _cache.clear()
