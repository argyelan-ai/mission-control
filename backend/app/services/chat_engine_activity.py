"""Is the agent's model engine working right now? — for the chat's live row.

A running thought or reply can go quiet for minutes although the model is
at work: TensorFold holds a tool call's arguments (DSML) until the call is
complete, so a large file write streams nothing to MC (live 31.10.2026: a
51 KB write showed "silent for 2 min"). The chat asks this read only once its
live row has gone quiet, and then says "the model is computing" instead of
"silent" — or keeps saying "silent" when the engine is idle or unknown.

Honest by construction:
  - The signal is per ENGINE, not per agent: ``requests_running > 0`` says the
    engine the agent is bound to answers some request — usually the agent's
    own, but another agent on the same engine counts too. The UI words it so.
  - Requests in flight, not the decode rate: TensorFold's decode rate can read
    0 while it holds a tool call, a running request does not.
  - Reuses the one engine prober (``heads.engine.probe_running_requests``) —
    no third prober. Only local engines (a box-bound OpenAI runtime): a cloud
    API has no ``/metrics``.
  - Unknown is never busy: no runtime, a cloud runtime, an unreachable engine
    or no known metric → ``None`` and the field is absent.
  - One probe per engine per ``TTL_S`` (several open chats, one engine), a
    failure is cached too, and concurrent asks share one probe.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.runtime import Runtime
from app.services.heads import engine

#: Load changes fast; the chat polls every ~10 s while quiet.
TTL_S = 5.0
#: Above the prober's own 3 s HTTP timeout (it may ask /metrics, then /health).
PROBE_TIMEOUT_S = 7.0

_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
_locks: dict[str, asyncio.Lock] = {}


def clear_cache() -> None:
    _cache.clear()
    _locks.clear()


async def _probe(endpoint: str) -> dict[str, Any] | None:
    try:
        running, _reason = await asyncio.wait_for(engine.probe_running_requests(endpoint), PROBE_TIMEOUT_S)
    except Exception:  # noqa: BLE001 — an unreachable engine is "unknown", never an error
        return None
    if running is None:
        return None
    return {
        "busy": running > 0,
        "running": running,
        "checkedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


async def engine_state(endpoint: str) -> dict[str, Any] | None:
    """``{busy, running, checkedAt}`` for one engine, cached ``TTL_S``."""
    hit = _cache.get(endpoint)
    if hit is not None and time.monotonic() - hit[0] < TTL_S:
        return hit[1]
    lock = _locks.setdefault(endpoint, asyncio.Lock())
    async with lock:
        hit = _cache.get(endpoint)
        if hit is not None and time.monotonic() - hit[0] < TTL_S:
            return hit[1]
        value = await _probe(endpoint)
        _cache[endpoint] = (time.monotonic(), value)
        return value


async def agent_engine_activity(session: AsyncSession, agent: Agent) -> dict[str, Any]:
    """``{"engine": {busy, running, checkedAt}}`` for the agent's bound local
    engine, or ``{"engine": None}`` when that cannot be known."""
    from app.services.heads.pairs import is_local

    if agent.runtime_id is None:
        return {"engine": None}
    runtime = await session.get(Runtime, agent.runtime_id)
    if runtime is None or not runtime.endpoint or not is_local(runtime):
        return {"engine": None}
    return {"engine": await engine_state(runtime.endpoint)}
