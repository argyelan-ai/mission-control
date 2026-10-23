"""Effort probe for the direct-API path (bench W0.6).

Which request key switches a served model's thinking is not knowable from
its name (see ``services.reasoning_params``). This probe asks the engine
itself with a handful of tiny requests and caches the answer per
(runtime, served model, serving_since) in ``bench_probes``:

1. baseline — no key: does the model think by default?
2. each key in ``THINKING_KEYS`` with the value that should flip that — the
   first key that flips it wins (a key the engine ignores or rejects is
   "no effect");
3. with ``reasoning_effort``: one more request with ``max`` — offered only
   when the engine accepts it.

Every request: one short arithmetic question, ``max_tokens`` 64, temperature
0. Typical cost: 3 requests, well under a second each on the Spark.

The effort chip and the create validation only offer what a probe found;
the run itself sends the probed key (``orchestrator._spark_generate``).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.bench import BenchProbe
from app.services import reasoning_params as rp

logger = logging.getLogger("mc.bench_studio")

PROBE_PROMPT = "What is 17 plus 25? Answer with the number only."
PROBE_MAX_TOKENS = 64
PROBE_REQUEST_TIMEOUT_S = 30.0
#: A probe without a known serving_since (engine state unknown) or a failed
#: one is trusted only this long.
UNPINNED_PROBE_TTL = timedelta(minutes=30)

_probe_lock = asyncio.Lock()


async def _ask(cli: httpx.AsyncClient, url: str, model: str, fields: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": PROBE_PROMPT}],
        "max_tokens": PROBE_MAX_TOKENS,
        "temperature": 0,
        **fields,
    }
    try:
        resp = await cli.post(url, json=body)
    except httpx.HTTPError as exc:
        return {"http": None, "error": f"{type(exc).__name__}: {exc}"[:200]}
    if resp.status_code != 200:
        return {"http": resp.status_code, "error": resp.text[:200]}
    try:
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        seen = rp.reasoning_of(choice.get("message"), data.get("usage"))
    except (ValueError, AttributeError, IndexError, TypeError) as exc:
        return {"http": 200, "error": f"unreadable response: {exc}"[:200]}
    return {"http": 200, **seen, "finish_reason": choice.get("finish_reason")}


async def run_probe(llm_url: str, model: str) -> dict[str, Any]:
    """Probe ``model`` at ``llm_url`` (OpenAI base URL ending in ``/v1``).
    Pure: no DB. Never raises — a dead engine is ``status: "failed"``."""
    url = f"{llm_url.rstrip('/')}/chat/completions"
    attempts: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=PROBE_REQUEST_TIMEOUT_S) as cli:
        base = await _ask(cli, url, model, {})
        attempts.append({"key": None, "value": None, **base})
        if base.get("http") != 200 or "thinking" not in base:
            return {
                "status": "failed",
                "thinking_key": None,
                "effort_param": None,
                "levels": [],
                "default_observed": "unknown",
                "raw": {"attempts": attempts},
            }
        default_on = base["thinking"] == "on"
        want = "off" if default_on else "on"
        thinking_key: str | None = None
        for key in rp.THINKING_KEYS:
            fields = rp.request_fields(key, want)
            seen = await _ask(cli, url, model, fields)
            attempts.append({**(rp.wire_fields(fields) or {}), **seen})
            if seen.get("http") == 200 and seen.get("thinking") == ("off" if default_on else "on"):
                thinking_key = key
                break

        levels: list[str] = []
        effort_param: str | None = None
        if thinking_key == rp.REASONING_EFFORT:
            effort_param = rp.REASONING_EFFORT
            # low/medium/high are the OpenAI-standard values of the same key
            # the engine just honoured; max is not standard — ask once.
            levels = (["off"] if default_on else []) + ["low", "medium", "high"]
            fields = rp.request_fields(rp.REASONING_EFFORT, "max")
            top = await _ask(cli, url, model, fields)
            attempts.append({**(rp.wire_fields(fields) or {}), **top})
            if top.get("http") == 200 and top.get("thinking") == "on":
                levels.append("max")
        elif thinking_key is not None:
            levels = [want]
    return {
        "status": "ok" if thinking_key else "no_effect",
        "thinking_key": thinking_key,
        "effort_param": effort_param,
        "levels": levels,
        "default_observed": "on" if default_on else "off",
        "raw": {"attempts": attempts},
    }


async def spark_runtime(session: AsyncSession):
    """The Spark runtime row the direct-API path talks to, or None."""
    from app.services.runtime_model_resolver import get_spark_vllm_runtime

    try:
        return await get_spark_vllm_runtime(session)
    except Exception:  # noqa: BLE001 — a missing runtime row only loses the cache key
        logger.warning("bench effort probe: spark runtime lookup failed", exc_info=True)
        return None


def _fresh(row: BenchProbe, pinned: bool) -> bool:
    """A failed probe is never reused (the box may just have been busy). A
    probe pinned to a serving_since stays valid for that engine run; one
    without (engine state unknown) only for ``UNPINNED_PROBE_TTL``."""
    if row.status == "failed":
        return False
    if pinned:
        return True
    probed = row.probed_at
    if probed is None:
        return False
    if probed.tzinfo is None:
        probed = probed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - probed < UNPINNED_PROBE_TTL


async def _cached(session: AsyncSession, runtime_id, model: str, serving_since) -> BenchProbe | None:
    stmt = select(BenchProbe).where(BenchProbe.served_model_id == model)
    stmt = stmt.where(
        BenchProbe.runtime_id == runtime_id if runtime_id is not None else BenchProbe.runtime_id.is_(None)  # type: ignore[union-attr]
    )
    stmt = stmt.where(
        BenchProbe.serving_since == serving_since
        if serving_since is not None
        else BenchProbe.serving_since.is_(None)  # type: ignore[union-attr]
    )
    return (await session.exec(stmt)).first()


async def cached_probe(session: AsyncSession, model: str) -> BenchProbe | None:
    """The stored probe for ``model`` on the Spark as it runs now, WITHOUT
    probing: ``None`` when there is none, or it failed, or it is stale. Used
    where an extra request is not wanted (a "default" run only needs to know
    whether the server thinks by default)."""
    runtime = await spark_runtime(session)
    runtime_id = runtime.id if runtime is not None else None
    serving_since = runtime.serving_since if runtime is not None else None
    row = await _cached(session, runtime_id, model, serving_since)
    if row is None or not _fresh(row, serving_since is not None):
        return None
    return row


async def stored_probe(session: AsyncSession, model: str) -> BenchProbe | None:
    """What the dialog may show WITHOUT touching the box: the stored probe
    for ``model`` on the Spark as it runs now — a fresh one, or a failed one
    (so the dialog can offer "check again"). ``None`` = never checked for
    this engine run (or the result went stale). No request, no write."""
    runtime = await spark_runtime(session)
    runtime_id = runtime.id if runtime is not None else None
    serving_since = runtime.serving_since if runtime is not None else None
    row = await _cached(session, runtime_id, model, serving_since)
    if row is None:
        return None
    if row.status == "failed" or _fresh(row, serving_since is not None):
        return row
    return None


async def spark_busy_reason(session: AsyncSession) -> str | None:
    """Why a probe must not run now, or None when the Spark is idle.

    A probe is 2-4 real requests against the box. Not while a bench run
    generates there, and not while an agent bound to a Spark runtime row
    (recipe or slot) works on a task — the operator rule "check running
    tasks before loading the Spark"."""
    from app.models.agent import Agent
    from app.models.bench import BenchChallenge, BenchEntry
    from app.services.runtime_model_resolver import spark_runtime_candidates

    # Only entries of a challenge that is still generating: a left-over row
    # under a stopped/failed challenge is no run. A run cut off by a restart
    # is healed by the bench sweep within a minute (review SHOULD-2, PR #955).
    running = (
        await session.exec(
            select(BenchChallenge.title)
            .join(BenchEntry, BenchEntry.challenge_id == BenchChallenge.id)  # type: ignore[arg-type]
            .where(
                BenchEntry.source_kind == "spark",
                BenchEntry.status == "generating",
                BenchChallenge.status == "generating",
            )
        )
    ).first()
    if running is not None:
        return (
            f"A bench run is generating on the Spark right now (challenge “{running}”) "
            "— check again when it is done."
        )
    runtime_ids = [r.id for r in await spark_runtime_candidates(session)]
    if not runtime_ids:
        return None
    busy = (
        await session.exec(
            select(Agent).where(
                Agent.runtime_id.in_(runtime_ids),  # type: ignore[union-attr]
                Agent.current_task_id.is_not(None),  # type: ignore[union-attr]
            )
        )
    ).all()
    if busy:
        names = ", ".join(sorted(a.name for a in busy))
        return f"The Spark is busy ({names} working on a task) — check again when it is done."
    return None


async def get_or_probe(session: AsyncSession, model: str, *, refresh: bool = False) -> BenchProbe:
    """The probe for ``model`` on the Spark as it runs now — cached per
    (runtime, model, serving_since), probed when missing, failed, stale
    (no serving_since and older than ``UNPINNED_PROBE_TTL``) or ``refresh``."""
    from app.services.spark_client import SparkClient

    runtime = await spark_runtime(session)
    runtime_id = runtime.id if runtime is not None else None
    serving_since = runtime.serving_since if runtime is not None else None
    async with _probe_lock:
        row = await _cached(session, runtime_id, model, serving_since)
        if row is not None and not refresh and _fresh(row, serving_since is not None):
            return row
        result = await run_probe(SparkClient().llm_url, model)
        if row is None:
            row = BenchProbe(runtime_id=runtime_id, served_model_id=model, serving_since=serving_since)
        row.thinking_key = result["thinking_key"]
        row.effort_param = result["effort_param"]
        row.levels = result["levels"]
        row.default_observed = result["default_observed"]
        row.status = result["status"]
        row.raw = result["raw"]
        row.probed_at = datetime.now(timezone.utc)
        try:
            async with session.begin_nested():
                session.add(row)
            await session.commit()
        except IntegrityError:
            # A probe of the same engine landed in between (another worker).
            await session.rollback()
            other = await _cached(session, runtime_id, model, serving_since)
            if other is not None:
                return other
            raise
        await session.refresh(row)
        logger.info(
            "bench effort probe %s: status=%s key=%s levels=%s default=%s",
            model, row.status, row.thinking_key, row.levels, row.default_observed,
        )
        return row


def serialize(
    row: BenchProbe | None, model: str | None, *, reachable: bool = True, status: str = "failed"
) -> dict[str, Any]:
    """The dialog's view of a probe (no raw attempts). Without a row:
    ``status`` says why — "failed" (box offline) or "unchecked" (never
    probed for this engine run; the dialog offers an explicit check)."""
    if row is None:
        return {
            "served_model_id": model,
            "reachable": reachable,
            "status": status,
            "thinking_key": None,
            "effort_param": None,
            "levels": [],
            "default_observed": "unknown",
            "probed_at": None,
            "serving_since": None,
        }
    return {
        "served_model_id": row.served_model_id,
        "reachable": reachable,
        "status": row.status,
        "thinking_key": row.thinking_key,
        "effort_param": row.effort_param,
        "levels": list(row.levels or []),
        "default_observed": row.default_observed,
        "probed_at": row.probed_at.isoformat() if row.probed_at else None,
        "serving_since": row.serving_since.isoformat() if row.serving_since else None,
    }


def reached(row: BenchProbe) -> bool:
    """False when the probe's first request never got an HTTP answer (box
    offline / connection refused) — the dialog then says "Box offline"
    instead of "Check failed". Any answer, even an error status, counts as
    reached."""
    if row.status != "failed":
        return True
    attempts = (row.raw or {}).get("attempts") or []
    return not attempts or attempts[0].get("http") is not None


class EffortProbeFailedError(rp.UnsupportedEffortError):
    """The probe itself failed (box gone, error answer): it knows nothing
    about the levels, so "the model does not offer it" would be a wrong
    claim (round-7 re-check r1 N3). A subclass, so every caller that only
    asks "can this level run?" keeps working unchanged."""


def level_fields(row: BenchProbe, level: str) -> dict[str, Any]:
    """Request fields for ``level`` under this probe — raises
    ``UnsupportedEffortError`` when the probe did not offer it, and its
    subclass ``EffortProbeFailedError`` when the probe failed."""
    if level in (None, "", "default"):
        return {}
    if row.status != "ok":
        raise EffortProbeFailedError(
            f"effort {level!r} cannot be checked for {row.served_model_id}: "
            f"the effort probe failed (probe: {row.status})"
        )
    if level not in (row.levels or []):
        raise rp.UnsupportedEffortError(
            f"effort {level!r} is not available for {row.served_model_id} "
            f"(probe: {row.status}, levels: {', '.join(row.levels or []) or 'none'})"
        )
    return rp.request_fields(row.effort_param or row.thinking_key, level)
