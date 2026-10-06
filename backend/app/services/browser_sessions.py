"""Browser sessions (ADR-088) — open, address and end one browser area per
agent session or head run.

A session is a `browser_sessions` row plus a registration at cdp-gateway
(`PUT /mc/sessions/<token>`). The harness then reaches the shared agent
browser at `<gateway>/s/<token>/`; everything it creates there belongs to the
session. Registering creates nothing in Chromium — the browser part starts
when the harness first uses the address, and ending the session closes its
tabs and contexts (`DELETE /mc/sessions/<token>`).

The token is derived, never stored: HMAC-SHA256 over the session id with the
server secret, url-safe base64. MC can recompute it to re-register after a
gateway restart (the gateway's register lives in memory), and a database dump
contains no live address.

This module knows no harness: how an address reaches playwright-mcp, omp or a
head is the harness layer's job (ADR-088 harness-wiring step). For now there is no caller besides the
operator API — nothing opens a session on its own yet.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import uuid
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
# Head run ids are uuids (docs/specs/head-launcher.md); anything else never
# reaches a URL or the database.
_RUN_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# The gateway's slug rule (cdp_gateway.py `_SLUG_RE`); it refuses anything else.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# Tests swap in an httpx.MockTransport (same pattern as services/heads/engine.py).
_transport: Optional[httpx.AsyncBaseTransport] = None


def session_token(session_id: uuid.UUID) -> str:
    """The session's credential at the gateway: 43 url-safe characters,
    stable for the session, unguessable without the server secret."""
    digest = hmac.new(
        settings.jwt_secret_key.encode(), b"mc-browser-session:" + session_id.bytes, hashlib.sha256,
    ).digest()
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


async def end_session(session: AsyncSession, row: BrowserSession, *, reason: str) -> Optional[dict]:
    """End the session: the gateway closes its tabs, contexts and open
    connections, the row becomes "ended". Idempotent. Returns the gateway's
    cleanup report, or None if there was nothing to clean up there (already
    ended, gateway restarted, or unreachable — the row is ended anyway; tabs a
    reachable-but-failed gateway still holds are the ADR-088 lifecycle step's job)."""
    if row.status == "ended":
        return None
    result: Optional[dict] = None
    try:
        async with _client() as client:
            resp = await client.delete(f"/mc/sessions/{session_token(row.id)}")
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
