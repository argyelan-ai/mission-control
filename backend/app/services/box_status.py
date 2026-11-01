"""Box facts for agents (ADR-100 §3 item 1) — read-only, nothing computed twice.

An agent asking "is box X free?" gets its answer from MC instead of from
another agent. Every fact comes from a source that already exists:

- the rows: enabled runtimes bound to a box, the same rows the runtimes page
  lists (``routers/runtimes.py::list_runtimes``), with two-box members from
  ``runtime_hosts`` (``recipe_switcher.runtime_host_ids``);
- engine state: the runtime watcher's live snapshot in Redis
  (``RedisKeys.runtime_live``), the same one ``GET /runtimes/live-status``
  reads — no second probe of the engine's model list;
- running requests and the switch lock: ``box_guard.switch_lock`` itself, so
  "locked" here means exactly "a switch would be refused now";
- a working head: ``box_guard.heads_on`` (empty while heads are off).

The verdict is the only thing added, and it is deliberately strict: a box is
``free`` only when every engine on it serves AND reports zero running
requests AND no head works there. An engine that does not serve is never
"free" — during a hand test the operator stops it by hand, and its zero
requests mean nothing (ADR-100 Kontext). Nothing here writes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.host import Host
from app.models.runtime import Runtime
from app.models.runtime_host import RuntimeHost
from app.redis_client import RedisKeys, get_redis
from app.services.heads import box_guard
from app.services.recipe_switcher import runtime_host_ids

logger = logging.getLogger(__name__)


async def _live_snapshot(redis, slug: str) -> dict | None:
    raw = await redis.get(RedisKeys.runtime_live(slug))
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _engine_state(snapshot: dict | None) -> str:
    """serving · switching · failed · not_serving · unknown (no fresh snapshot)."""
    if snapshot is None:
        return "unknown"
    status = snapshot.get("status")
    if status == "switching":
        return "switching"
    if status == "failed":
        return "failed"
    return "serving" if snapshot.get("reachable") else "not_serving"


async def _lock_for(rt: Runtime) -> tuple[str, int | None, str | None]:
    """``(switch_lock, running_requests, unknown_reason)`` from the real lock."""
    refusal, unknown = await box_guard.switch_lock([rt])
    if refusal is not None:
        return "locked", refusal.get("running_requests"), None
    if unknown:
        return "unknown", None, unknown[0].get("reason")
    return "open", 0, None


def _verdict(runtimes: list[dict], head: dict | None, auto_recovery: bool) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if head is not None:
        reasons.append("head_working")
    if any(r["switch_lock"] == "locked" for r in runtimes):
        reasons.append("requests_running")
    if any(r["engine"] == "switching" for r in runtimes):
        reasons.append("switch_in_progress")
    if reasons:
        return "busy", reasons
    if any(r["engine"] in ("not_serving", "failed") for r in runtimes):
        reasons.append("engine_not_serving")
        if not auto_recovery:
            # MC will not restart it on its own — the shape of a hand test.
            reasons.append("manual_test_possible")
        return "not_serving", reasons
    if any(r["engine"] == "unknown" for r in runtimes):
        return "unknown", ["engine_state_unknown"]
    if any(r["switch_lock"] == "unknown" for r in runtimes):
        return "unknown", ["load_unknown"]
    return "free", []


async def read_box_status(session: AsyncSession) -> dict:
    runtimes = [
        rt
        for rt in (await session.exec(select(Runtime).where(Runtime.enabled == True))).all()  # noqa: E712
        if rt.host_id is not None
    ]
    members: dict[uuid.UUID, list[RuntimeHost]] = {}
    if runtimes:
        rows = (
            await session.exec(
                select(RuntimeHost).where(RuntimeHost.runtime_id.in_([r.id for r in runtimes]))
            )
        ).all()
        for row in rows:
            members.setdefault(row.runtime_id, []).append(row)

    host_ids = {hid for rt in runtimes for hid in runtime_host_ids(rt, members)}
    hosts = {}
    if host_ids:
        hosts = {
            h.id: h
            for h in (await session.exec(select(Host).where(Host.id.in_(list(host_ids))))).all()
        }

    redis = await get_redis()
    snapshots = {rt.slug: await _live_snapshot(redis, rt.slug) for rt in runtimes}

    # One lock probe per engine endpoint (slot rows and recipe rows can share one).
    by_endpoint: dict[str, Runtime] = {}
    for rt in runtimes:
        by_endpoint.setdefault(rt.endpoint, rt)
    endpoints = list(by_endpoint)
    locks = dict(zip(endpoints, await asyncio.gather(*(_lock_for(by_endpoint[ep]) for ep in endpoints))))

    per_box: dict[uuid.UUID, list[dict]] = {}
    for rt in runtimes:
        snap = snapshots.get(rt.slug)
        lock, running, unknown_reason = locks[rt.endpoint]
        entry = {
            "slug": rt.slug,
            "display_name": rt.display_name,
            "model": rt.model_identifier,
            "served_model": (snap or {}).get("served_model"),
            "engine": _engine_state(snap),
            "checked_at": (snap or {}).get("last_probe_at"),
            "running_requests": running,
            "switch_lock": lock,
            "load_unknown_reason": unknown_reason,
            "switching": (
                {"phase": snap.get("phase"), "source": snap.get("switch_source")}
                if snap and snap.get("status") == "switching" else None
            ),
        }
        for hid in runtime_host_ids(rt, members):
            role = "head" if hid == rt.host_id else "worker"
            per_box.setdefault(hid, []).append({**entry, "role": role})

    boxes = []
    for hid, entries in per_box.items():
        host = hosts.get(hid)
        if host is None or not host.enabled:
            continue
        heads = box_guard.heads_on([hid])
        head = heads[0] if heads else None
        verdict, reasons = _verdict(entries, head, host.autostart_enabled)
        boxes.append({
            "host_id": str(host.id),
            "slug": host.slug,
            "display_name": host.display_name,
            # Off is the default and also what a hand test needs: MC will not
            # restart a stopped engine on this box by itself.
            "auto_recovery": bool(host.autostart_enabled),
            "head": head,
            "verdict": verdict,
            "reasons": reasons,
            "runtimes": sorted(entries, key=lambda e: e["slug"]),
        })
    boxes.sort(key=lambda b: (b["display_name"] or "", b["slug"]))
    return {
        "boxes": boxes,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
