"""Browser sessions (ADR-088) — open, address and end one browser area per
agent session or head run.

A session is a `browser_sessions` row plus a registration at cdp-gateway
(`PUT /mc/sessions/<token>`). The harness then reaches the shared agent
browser at `<gateway>/s/<token>/`; everything it creates there belongs to the
session. Registering creates nothing in Chromium — the browser part starts
when the harness first uses the address, and ending the session closes its
tabs and contexts (`DELETE /mc/sessions/<token>`).

The token is derived, never stored: HMAC-SHA256 over the session id with a
stable server secret (`_token_key`), url-safe base64. MC can recompute it to re-register after a
gateway restart (the gateway's register lives in memory), and a database dump
contains no live address.

This module knows no harness: how an address reaches playwright-mcp, omp or a
head is the harness layer's job (ADR-088 harness-wiring step).

Lifecycle (`lifecycle_tick`, run every few seconds by `browser_session_lifecycle`
in the background-services process; operator decisions 2026-10-06):
- an agent's working phase opens lazily at its first `/a/<slug>/` tab and ends
  after `browser_session_idle_s` without browser activity — with its tabs;
- a head's session ends when its run has ended (MC's run status, never a
  dropped connection); every session ends after `browser_session_max_age_s`;
- open sessions the gateway forgot (restart) are registered again, gateway
  sessions MC has ended are ended there too, and tabs an ended session left
  behind are swept;
- the last image is taken while a session is active (at most every
  `browser_frame_interval_s`) and right before it ends, and deleted
  `browser_frame_retention_days` after the end;
- the shared Chromium itself always stays on; nothing here starts or stops it.
Nothing is ever ended while the gateway is unreachable.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import logging
import os
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.agent import Agent
from app.models.browser_session import BrowserSession
from app.utils import utcnow

logger = logging.getLogger(__name__)

# cdp-gateway inside the cdp-browser container (also used by routers/browser_live.py).
GATEWAY_BASE_URL = os.environ.get("CDP_GATEWAY_URL", "http://cdp-browser:9300")
_GATEWAY_TIMEOUT = 5.0
# Ending waits longer: the gateway answers DELETE within its cleanup deadline
# (cdp_gateway.py `_CLEANUP_DEADLINE`, 8 s), so "unreachable" never hides a
# cleanup that is still running.
_END_TIMEOUT = 15.0
# Head run ids are uuids (docs/specs/head-launcher.md); anything else never
# reaches a URL or the database.
_RUN_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# The gateway's slug rule (cdp_gateway.py `_SLUG_RE`); it refuses anything else.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# Tests swap in an httpx.MockTransport (same pattern as services/heads/engine.py).
_transport: Optional[httpx.AsyncBaseTransport] = None


def _token_key() -> bytes:
    """The encryption key — stable by necessity, since rotating it already
    breaks every stored secret — so no extra setting has to reach the
    containers. The JWT secret is only the last resort for installs without
    one: rotating it to log users out must not strand open sessions."""
    return (settings.secrets_encryption_key or settings.jwt_secret_key).encode()


def session_token(session_id: uuid.UUID) -> str:
    """The session's credential at the gateway: 43 url-safe characters,
    stable for the session, unguessable without the server secret."""
    digest = hmac.new(_token_key(), b"mc-browser-session:" + session_id.bytes, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def endpoint_path(session_id: uuid.UUID) -> str:
    """Path under the gateway's base URL a harness uses as its CDP endpoint."""
    return f"/s/{session_token(session_id)}/"


def _agent_slug(agent: Agent) -> Optional[str]:
    """The agent's name at the gateway — the same slug its `/a/<slug>/`
    connections carry (routers/browser_live.py `_agent_slug`), so a session's
    tabs still show up in that agent's panel."""
    slug = agent.slug or (agent.name or "").lower().replace(" ", "-")
    return slug if _SLUG_RE.match(slug) else None


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=GATEWAY_BASE_URL, timeout=_GATEWAY_TIMEOUT, transport=_transport)


async def register_with_gateway(row: BrowserSession, agent_slug: Optional[str] = None) -> bool:
    """Tell the gateway about the session (idempotent). False if the gateway
    could not be reached or refused — the row stays valid either way."""
    params = {"session": str(row.id)}
    if agent_slug:
        params["agent"] = agent_slug
    try:
        async with _client() as client:
            resp = await client.put(f"/mc/sessions/{session_token(row.id)}", params=params)
    except httpx.HTTPError as e:
        logger.info("browser_sessions: gateway unreachable, %s not registered yet: %s", row.id, e)
        return False
    if resp.status_code not in (200, 201):
        logger.warning("browser_sessions: gateway refused %s: %s %s", row.id, resp.status_code, resp.text[:200])
        return False
    return True


async def _find_open(
    session: AsyncSession, agent: Optional[Agent], head_run_id: Optional[str],
) -> Optional[BrowserSession]:
    query = select(BrowserSession).where(BrowserSession.status != "ended")
    if agent is not None:
        query = query.where(BrowserSession.agent_id == agent.id)
    else:
        query = query.where(BrowserSession.head_run_id == head_run_id)
    return (await session.exec(query)).first()


async def open_session(
    session: AsyncSession,
    *,
    agent: Optional[Agent] = None,
    head_run_id: Optional[str] = None,
) -> tuple[BrowserSession, bool]:
    """Open (or return the already open) browser session of exactly one
    owner and register it with the gateway. Returns (row, registered)."""
    if (agent is None) == (head_run_id is None):
        raise ValueError("a browser session needs exactly one owner: an agent or a head run")
    if head_run_id is not None and not _RUN_ID_RE.match(head_run_id):
        raise ValueError("head_run_id must be a run uuid")

    row = await _find_open(session, agent, head_run_id)
    if row is None:
        row = BrowserSession(
            owner_kind="agent" if agent is not None else "head",
            agent_id=agent.id if agent is not None else None,
            head_run_id=head_run_id,
        )
        session.add(row)
        try:
            await session.commit()
        except IntegrityError:
            # Lost a race against a second open for the same owner: the
            # partial unique index kept it to one row — use that one.
            await session.rollback()
            row = await _find_open(session, agent, head_run_id)
            if row is None:
                raise
        else:
            await session.refresh(row)
    registered = await register_with_gateway(row, _agent_slug(agent) if agent is not None else None)
    return row, registered


async def end_session(
    session: AsyncSession, row: BrowserSession, *, reason: str, agent_tabs: bool = False,
) -> Optional[dict]:
    """End the session: the gateway closes its tabs, contexts and open
    connections, the row becomes "ended". Idempotent. Returns the gateway's
    cleanup report, or None if there was nothing to clean up there (already
    ended, gateway restarted, or unreachable — the row is ended anyway).
    `agent_tabs`: an agent's working phase also closes the agent's own tabs."""
    if row.status == "ended":
        return None
    result: Optional[dict] = None
    try:
        async with _client() as client:
            resp = await client.delete(
                f"/mc/sessions/{session_token(row.id)}",
                params={"agent_tabs": "1"} if agent_tabs else None,
                timeout=_END_TIMEOUT,
            )
        if resp.status_code == 200:
            result = resp.json()
        elif resp.status_code != 404:
            logger.warning("browser_sessions: gateway end of %s answered %s", row.id, resp.status_code)
    except (httpx.HTTPError, ValueError) as e:
        logger.info("browser_sessions: gateway unreachable while ending %s: %s", row.id, e)
    row.status = "ended"
    row.ended_at = utcnow()
    row.end_reason = reason[:64]
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return result


# ── lifecycle (ADR-088 lifecycle step) ─────────────────────────────────────

_FRAME_NAME = "last.jpg"
# A last image larger than this is not a screenshot of one tab; refuse it.
_MAX_FRAME_BYTES = 8 * 1024 * 1024
_RETENTION_BATCH = 50


def aware(value: Optional[datetime]) -> Optional[datetime]:
    """SQLite hands back naive datetimes; everything here compares in UTC."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def frame_path(session_id: uuid.UUID) -> Path:
    return Path(settings.browser_sessions_root) / str(session_id) / _FRAME_NAME


async def _gateway_json(client: httpx.AsyncClient, path: str):
    resp = await client.get(path)
    resp.raise_for_status()
    return resp.json()


async def _take_frame(client: httpx.AsyncClient, row: BrowserSession, now: datetime) -> bool:
    """Ask the gateway for the session's last image and store it. False (and
    the previous image kept) when there is no tab or the capture failed."""
    try:
        resp = await client.get(f"/mc/sessions/{session_token(row.id)}/snapshot", timeout=_END_TIMEOUT)
        if resp.status_code != 200:
            return False
        snap = resp.json()
        data = base64.b64decode(snap.get("data") or "", validate=True)
    except (httpx.HTTPError, ValueError, binascii.Error) as e:
        logger.info("browser_sessions: no last image for %s: %s", row.id, e)
        return False
    if not data or len(data) > _MAX_FRAME_BYTES:
        return False
    target = frame_path(row.id)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(target)
    row.last_frame_at = now
    row.last_url = (snap.get("url") or "")[:2048] or None
    row.last_title = (snap.get("title") or "")[:512] or None
    return True


async def _ended_or_unknown(session: AsyncSession, session_id: str) -> bool:
    """True if MC has no open row for this session id (ended, or no row at
    all). Read fresh: a session opened by the API after this pass listed its
    rows must never be ended or swept."""
    try:
        sid = uuid.UUID(session_id)
    except (ValueError, TypeError):
        return False
    row = await session.get(BrowserSession, sid)
    if row is not None:
        await session.refresh(row)
    return row is None or row.status == "ended"


def _head_run_ended(run_id: str, now: datetime) -> Optional[str]:
    """End reason for a head's session, or None while its run is going."""
    from app.services.heads import files
    from app.services.heads.state import FINAL_STATES, derive_for_run

    run = files.load_run(run_id)
    if run is None:
        return "run_missing"
    if derive_for_run(run, now.timestamp())["state"] in FINAL_STATES:
        return "run_ended"
    return None


def _purge_old_frames(rows: list[BrowserSession], now: datetime) -> int:
    cutoff = now - timedelta(days=settings.browser_frame_retention_days)
    purged = 0
    for row in rows:
        ended = aware(row.ended_at)
        folder = frame_path(row.id).parent
        if ended is not None and ended < cutoff and folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)
            purged += 1
    return purged


async def lifecycle_tick(session: AsyncSession, *, now: Optional[datetime] = None) -> dict:
    """One pass of the browser-session lifecycle. Returns a small report."""
    now = aware(now) or utcnow()
    report = {
        "gateway": "ok", "registered": 0, "opened": 0, "ended": 0, "frames": 0, "purged": 0,
        "stale_ended": 0, "swept": 0,
    }
    async with _client() as client:
        try:
            known = {s.get("sessionId") for s in await _gateway_json(client, "/mc/sessions")}
            targets = await _gateway_json(client, "/mc/targets")
            gateway_ids = set(known)
        except (httpx.HTTPError, ValueError) as e:
            logger.info("browser_sessions: gateway unreachable, lifecycle pass skipped: %s", e)
            report["gateway"] = "unreachable"
            return report

        rows = list((await session.exec(select(BrowserSession).where(BrowserSession.status != "ended"))).all())
        agents: dict[uuid.UUID, Agent] = {}
        agent_ids = [r.agent_id for r in rows if r.agent_id]
        if agent_ids:
            for agent in (await session.exec(select(Agent).where(Agent.id.in_(agent_ids)))).all():
                agents[agent.id] = agent

        # 1. Agent working phases open lazily at the agent's first tab
        #    (decision a): tabs on `/a/<slug>/` that no session owns — and
        #    only recently active ones. A tab idle past the limit is not a new
        #    phase; without this, a leftover tab would open and end a phase on
        #    every pass.
        phase_slugs = {
            t.get("agent") for t in targets
            if t.get("agent") and not t.get("session")
            and float(t.get("idleSeconds") or 0.0) < settings.browser_session_idle_s
        }
        open_agent_slugs = {_agent_slug(agents[r.agent_id]) for r in rows if r.agent_id in agents}
        for slug in sorted(phase_slugs - open_agent_slugs):
            matches = (await session.exec(select(Agent).where(Agent.slug == slug))).all()
            # Ambiguous or unknown slug: never guess whose browser it is.
            if len(matches) != 1 or matches[0].archived_at is not None:
                continue
            agent = matches[0]
            row, registered = await open_session(session, agent=agent)
            agents[agent.id] = agent
            rows.append(row)
            if registered:
                known.add(str(row.id))
            report["opened"] += 1

        for row in rows:
            slug = _agent_slug(agents[row.agent_id]) if row.agent_id in agents else None
            # 2. The gateway forgot it (restart): register again.
            if str(row.id) not in known:
                if await register_with_gateway(row, slug):
                    report["registered"] += 1

            # 3. Its tabs: its own, plus the agent's for an agent's phase.
            tabs = {
                t["targetId"]: t for t in targets
                if t.get("session") == str(row.id) or (slug and row.owner_kind == "agent" and t.get("agent") == slug)
            }
            if tabs:
                if row.status == "open":
                    row.status = "live"
                row.started_at = aware(row.started_at) or now
                idle = min(float(t.get("idleSeconds") or 0.0) for t in tabs.values())
                seen = now - timedelta(seconds=idle)
                if aware(row.last_active_at) is None or seen > aware(row.last_active_at):
                    row.last_active_at = seen

            # 4. Should it end?
            reason = None
            started = aware(row.started_at)
            # Measured from the first tab, or from the opening for a session
            # that never got one (a head whose harness never connected).
            age_from = started or aware(row.created_at) or now
            if (now - age_from).total_seconds() >= settings.browser_session_max_age_s:
                reason = "max_age"
            elif row.owner_kind == "head" and row.head_run_id:
                reason = _head_run_ended(row.head_run_id, now)
            elif row.owner_kind == "agent":
                reference = aware(row.last_active_at) or started or aware(row.created_at) or now
                if (now - reference).total_seconds() >= settings.browser_session_idle_s:
                    reason = "idle"

            # 5. Last image: while active (new activity, not more often than
            #    the interval), and once more right before the end.
            last_frame = aware(row.last_frame_at)
            active_since_frame = last_frame is None or (
                aware(row.last_active_at) is not None and aware(row.last_active_at) > last_frame
            )
            due = last_frame is None or (now - last_frame).total_seconds() >= settings.browser_frame_interval_s
            if tabs and (reason is not None or (active_since_frame and due)):
                if await _take_frame(client, row, now):
                    report["frames"] += 1

            session.add(row)
            await session.commit()
            if reason is not None:
                await end_session(
                    session, row, reason=reason,
                    agent_tabs=row.owner_kind == "agent" and settings.browser_idle_close_agent_tabs,
                )
                report["ended"] += 1

        # 6. The other direction: the gateway still knows a session MC has
        #    ended (an open/end race, a gateway 5xx on DELETE) -> end it there.
        #    And tabs an ended session created and left behind (a /json/new
        #    still in flight while it ended) -> sweep them.
        live_ids = {str(r.id) for r in rows if r.status != "ended"}
        ended_now = {str(r.id) for r in rows if r.status == "ended"}
        for sid in sorted(gateway_ids - live_ids - ended_now - {None}):
            if await _ended_or_unknown(session, sid):
                try:
                    await client.delete(f"/mc/sessions/{session_token(uuid.UUID(sid))}", timeout=_END_TIMEOUT)
                    report["stale_ended"] += 1
                except httpx.HTTPError as e:
                    logger.info("browser_sessions: could not end stale gateway session %s: %s", sid, e)
        try:
            orphans = {o.get("sessionId") for o in await _gateway_json(client, "/mc/orphans")}
        except (httpx.HTTPError, ValueError):
            orphans = set()
        orphans |= {t.get("creatorSession") for t in targets if t.get("creatorSession")}
        for sid in sorted(orphans - live_ids - ended_now - {None}):
            if await _ended_or_unknown(session, sid):
                try:
                    await client.post("/mc/orphans/close", params={"session": sid}, timeout=_END_TIMEOUT)
                    report["swept"] += 1
                except httpx.HTTPError as e:
                    logger.info("browser_sessions: could not sweep tabs of ended session %s: %s", sid, e)

    ended_rows = (await session.exec(
        select(BrowserSession)
        .where(BrowserSession.status == "ended")
        .where(BrowserSession.ended_at < now - timedelta(days=settings.browser_frame_retention_days))
        .order_by(BrowserSession.ended_at.desc())
        .limit(_RETENTION_BATCH * 4)
    )).all()
    report["purged"] = _purge_old_frames(list(ended_rows), now)
    return report


class BrowserSessionLifecycle:
    """Runs `lifecycle_tick` every `browser_sessions_interval` seconds in the
    background-services process (same pattern as heads_sync)."""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        interval = settings.browser_sessions_interval
        if self._running or not interval or interval <= 0 or interval >= 99999:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(interval), name="browser_session_lifecycle")
        logger.info("browser session lifecycle started (interval=%ss)", interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self, interval: int) -> None:
        from app.database import engine

        while self._running:
            await asyncio.sleep(interval)
            try:
                async with AsyncSession(engine, expire_on_commit=False) as session:
                    await lifecycle_tick(session)
            except Exception:  # noqa: BLE001 — never kill the loop
                logger.exception("browser session lifecycle pass failed")


browser_session_lifecycle = BrowserSessionLifecycle()
