"""Browser-session lifecycle (ADR-088 lifecycle step + operator decisions 2026-10-06).

One `lifecycle_tick` pass against a fake cdp-gateway:
- re-registers open sessions the gateway forgot (restart),
- opens an agent's working phase lazily at its first tab (decision a),
- open -> live, activity time, last image every 60 s while active,
- ends an agent phase after 30 min without activity (with its tabs),
- ends a head's session when the run has ended (MC run status, not a dropped
  connection), and any session after the hard age limit,
- never ends anything while the gateway is unreachable,
- deletes last images 30 days after the end.
"""
from __future__ import annotations

import base64
import uuid
from datetime import timedelta

import httpx
import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.browser_session import BrowserSession
from app.services import browser_sessions as svc
from app.utils import utcnow
from tests.conftest import test_engine
from tests.heads_backend_helpers import heads_root, make_run  # noqa: F401

JPEG = b"\xff\xd8\xff-fake-jpeg"


class FakeGateway:
    def __init__(self):
        self.reachable = True
        self.registered: dict[str, dict] = {}   # token -> {"session":..., "agent":...}
        self.targets: list[dict] = []
        self.snapshot_status = 200
        self.jpeg = JPEG
        self.requests: list[httpx.Request] = []

    def tab(self, tid, *, agent=None, session=None, idle=5.0, url="https://x.example", title="X"):
        self.targets.append({
            "targetId": tid, "title": title, "url": url, "agent": agent, "session": session,
            "browserContextId": None, "idleSeconds": idle,
        })

    def handler(self, request: httpx.Request) -> httpx.Response:
        if not self.reachable:
            raise httpx.ConnectError("down", request=request)
        self.requests.append(request)
        path = request.url.path
        if path == "/mc/targets":
            return httpx.Response(200, json=self.targets)
        if path == "/mc/sessions":
            return httpx.Response(200, json=[
                {"sessionId": v["session"], "agent": v["agent"], "tabs": 0, "contexts": 0, "connections": 0}
                for v in self.registered.values()
            ])
        token = path.split("/")[3]
        if path.endswith("/snapshot"):
            if self.snapshot_status != 200:
                return httpx.Response(self.snapshot_status)
            return httpx.Response(200, json={
                "targetId": "T", "url": "https://shot.example/page", "title": "Shot",
                "mime": "image/jpeg", "data": base64.b64encode(self.jpeg).decode(),
            })
        if request.method == "PUT":
            self.registered[token] = {"session": request.url.params["session"], "agent": request.url.params.get("agent")}
            return httpx.Response(201, json={})
        if request.method == "DELETE":
            if self.registered.pop(token, None) is None:
                return httpx.Response(404)
            return httpx.Response(200, json={"closedTargets": 1, "disposedContexts": 0, "closedConnections": 0, "errors": []})
        return httpx.Response(405)

    def calls(self, method, suffix=""):
        return [r for r in self.requests if r.method == method and r.url.path.endswith(suffix)]


@pytest.fixture
def gw(monkeypatch, tmp_path):
    fake = FakeGateway()
    monkeypatch.setattr(svc, "_transport", httpx.MockTransport(fake.handler))
    monkeypatch.setattr(settings, "browser_sessions_root", tmp_path / "browser-sessions")
    return fake


async def _rows():
    from sqlmodel import select
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return (await s.exec(select(BrowserSession))).all()


async def _add(**kw) -> BrowserSession:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        row = BrowserSession(**kw)
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row


async def _get(row_id) -> BrowserSession:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return await s.get(BrowserSession, row_id)


async def _tick(session, now=None):
    return await svc.lifecycle_tick(session, now=now or utcnow())


# ── gateway outage, re-register ────────────────────────────────────────────

async def test_unreachable_gateway_changes_nothing(session: AsyncSession, gw):
    row = await _add(owner_kind="head", head_run_id=str(uuid.uuid4()), created_at=utcnow() - timedelta(hours=9))
    gw.reachable = False
    report = await _tick(session)
    assert report["gateway"] == "unreachable"
    assert (await _get(row.id)).status == "open"


async def test_open_session_the_gateway_forgot_is_registered_again(session: AsyncSession, gw, make_agent):
    agent = await make_agent(name="Alpha", slug="alpha")
    row = await _add(owner_kind="agent", agent_id=agent.id)
    await _tick(session)
    [put] = gw.calls("PUT")
    assert put.url.path == f"/mc/sessions/{svc.session_token(row.id)}"
    assert put.url.params["agent"] == "alpha"
    await _tick(session)
    assert len(gw.calls("PUT")) == 1  # known now: no second register


# ── agent working phase (decision a) ───────────────────────────────────────

async def test_first_agent_tab_opens_a_working_phase(session: AsyncSession, gw, make_agent):
    agent = await make_agent(name="Alpha", slug="alpha")
    gw.tab("A1", agent="alpha", idle=3.0)
    now = utcnow()
    report = await _tick(session, now)

    [row] = await _rows()
    assert report["opened"] == 1
    assert row.owner_kind == "agent" and row.agent_id == agent.id
    assert row.status == "live" and row.started_at is not None
    assert svc.aware(row.last_active_at) == pytest.approx(now - timedelta(seconds=3), abs=timedelta(seconds=1))
    # A second pass does not open another phase.
    await _tick(session)
    assert len(await _rows()) == 1


async def test_tabs_of_unknown_or_session_owned_tabs_open_no_phase(session: AsyncSession, gw):
    gw.tab("X1", agent="nobody-here")
    gw.tab("S1", agent=None, session=str(uuid.uuid4()))
    gw.tab("U1")
    await _tick(session)
    assert await _rows() == []


async def test_agent_phase_ends_after_30_minutes_without_activity(session: AsyncSession, gw, make_agent):
    """Sabotage: compare against the frame interval instead of the idle limit
    -> the phase ends far too early and the 'still active' half goes red."""
    agent = await make_agent(name="Alpha", slug="alpha")
    t0 = utcnow()
    gw.tab("A1", agent="alpha", idle=29 * 60)
    await _tick(session, t0)
    [row] = await _rows()
    assert row.status == "live"

    gw.targets[0]["idleSeconds"] = 31 * 60                 # two minutes later, still untouched
    gw.jpeg = b"\xff\xd8\xff-at-the-end"
    report = await _tick(session, t0 + timedelta(minutes=2))
    row = await _get(row.id)
    assert report["ended"] == 1
    assert row.status == "ended" and row.end_reason == "idle"
    [delete] = gw.calls("DELETE")
    assert delete.url.params["agent_tabs"] == "1"
    # A fresh last image was taken right before the cleanup (sabotage:
    # no capture on the way out -> the file still holds the first image).
    assert (settings.browser_sessions_root / str(row.id) / "last.jpg").read_bytes() == b"\xff\xd8\xff-at-the-end"
    snap_at = [i for i, r in enumerate(gw.requests) if r.url.path.endswith("/snapshot")]
    assert snap_at and snap_at[-1] < gw.requests.index(delete)


async def test_closing_agent_tabs_on_idle_end_can_be_switched_off(session: AsyncSession, gw, make_agent, monkeypatch):
    monkeypatch.setattr(settings, "browser_idle_close_agent_tabs", False)
    agent = await make_agent(name="Alpha", slug="alpha")
    await _add(owner_kind="agent", agent_id=agent.id, status="live", started_at=utcnow() - timedelta(hours=1),
               last_active_at=utcnow() - timedelta(minutes=40))
    await _tick(session)
    [delete] = gw.calls("DELETE")
    assert "agent_tabs" not in delete.url.params


async def test_a_leftover_idle_agent_tab_opens_no_phase(session: AsyncSession, gw, make_agent):
    """A tab idle past the limit is not a new working phase — otherwise a
    leftover tab would open and end a phase on every pass."""
    await make_agent(name="Alpha", slug="alpha")
    gw.tab("OLD", agent="alpha", idle=2 * 3600)
    await _tick(session)
    assert await _rows() == []


async def test_an_ambiguous_agent_slug_opens_no_phase(session: AsyncSession, gw, make_agent):
    await make_agent(name="Alpha One", slug="alpha")
    await make_agent(name="Alpha Two", slug="alpha")
    gw.tab("A1", agent="alpha", idle=1.0)
    await _tick(session)
    assert await _rows() == []


# ── heads: end with the run ────────────────────────────────────────────────

async def test_head_session_ends_when_the_run_has_ended(session: AsyncSession, gw, heads_root):  # noqa: F811
    running = make_run(heads_root, status={"phase": "running"}, heartbeat_age=5)
    exited = make_run(heads_root, status={"phase": "exited", "reason": "stopped"})
    alive = await _add(owner_kind="head", head_run_id=running)
    done = await _add(owner_kind="head", head_run_id=exited)
    gone = await _add(owner_kind="head", head_run_id=str(uuid.uuid4()))
    await _tick(session)

    assert (await _get(alive.id)).status == "open"
    assert (await _get(done.id)).status == "ended" and (await _get(done.id)).end_reason == "run_ended"
    assert (await _get(gone.id)).status == "ended" and (await _get(gone.id)).end_reason == "run_missing"


async def test_head_session_goes_live_and_records_its_tab(session: AsyncSession, gw, heads_root):  # noqa: F811
    run = make_run(heads_root, status={"phase": "running"}, heartbeat_age=5)
    row = await _add(owner_kind="head", head_run_id=run)
    gw.tab("H1", session=str(row.id), idle=2.0)
    await _tick(session)
    row = await _get(row.id)
    assert row.status == "live" and row.started_at is not None
    assert row.last_url == "https://shot.example/page" and row.last_title == "Shot"


async def test_any_session_ends_at_the_hard_age_limit(session: AsyncSession, gw, heads_root):  # noqa: F811
    run = make_run(heads_root, status={"phase": "running"}, heartbeat_age=5)
    row = await _add(owner_kind="head", head_run_id=run, status="live",
                     started_at=utcnow() - timedelta(seconds=settings.browser_session_max_age_s + 60))
    gw.tab("H1", session=str(row.id), idle=1.0)
    await _tick(session)
    row = await _get(row.id)
    assert row.status == "ended" and row.end_reason == "max_age"


async def test_a_session_that_never_got_a_tab_still_ends_at_the_age_limit(session: AsyncSession, gw, heads_root):  # noqa: F811
    run = make_run(heads_root, status={"phase": "running"}, heartbeat_age=5)
    row = await _add(owner_kind="head", head_run_id=run,
                     created_at=utcnow() - timedelta(seconds=settings.browser_session_max_age_s + 60))
    await _tick(session)
    row = await _get(row.id)
    assert row.status == "ended" and row.end_reason == "max_age"


# ── last image ─────────────────────────────────────────────────────────────

async def test_last_image_is_taken_only_while_active_and_at_most_every_60_s(session: AsyncSession, gw, make_agent):
    await make_agent(name="Alpha", slug="alpha")
    gw.tab("A1", agent="alpha", idle=1.0)
    t0 = utcnow()
    await _tick(session, t0)
    assert len(gw.calls("GET", "/snapshot")) == 1
    [row] = await _rows()
    frame = settings.browser_sessions_root / str(row.id) / "last.jpg"
    assert frame.read_bytes() == JPEG and row.last_frame_at is not None

    gw.targets[0]["idleSeconds"] = 21.0                    # activity at t0-1, not since
    await _tick(session, t0 + timedelta(seconds=20))
    assert len(gw.calls("GET", "/snapshot")) == 1
    gw.targets[0]["idleSeconds"] = 121.0                   # still nothing new, interval passed
    await _tick(session, t0 + timedelta(seconds=120))
    assert len(gw.calls("GET", "/snapshot")) == 1
    gw.targets[0]["idleSeconds"] = 2.0                     # activity again
    await _tick(session, t0 + timedelta(seconds=180))
    assert len(gw.calls("GET", "/snapshot")) == 2


async def test_failed_or_empty_snapshot_keeps_the_previous_image(session: AsyncSession, gw, make_agent):
    await make_agent(name="Alpha", slug="alpha")
    gw.tab("A1", agent="alpha", idle=1.0)
    gw.snapshot_status = 502
    await _tick(session)
    [row] = await _rows()
    assert row.last_frame_at is None and row.status == "live"
    assert not (settings.browser_sessions_root / str(row.id) / "last.jpg").exists()


async def test_last_images_are_deleted_30_days_after_the_end(session: AsyncSession, gw):
    old = await _add(owner_kind="head", head_run_id=str(uuid.uuid4()), status="ended",
                     ended_at=utcnow() - timedelta(days=31))
    recent = await _add(owner_kind="head", head_run_id=str(uuid.uuid4()), status="ended",
                        ended_at=utcnow() - timedelta(days=29))
    for row in (old, recent):
        folder = settings.browser_sessions_root / str(row.id)
        folder.mkdir(parents=True)
        (folder / "last.jpg").write_bytes(JPEG)
    await _tick(session)
    assert not (settings.browser_sessions_root / str(old.id)).exists()
    assert (settings.browser_sessions_root / str(recent.id) / "last.jpg").exists()


# ── API: last image ────────────────────────────────────────────────────────

async def test_api_serves_the_last_image(auth_client, gw):
    row = await _add(owner_kind="head", head_run_id=str(uuid.uuid4()))
    assert (await auth_client.get(f"/api/v1/browser-sessions/{row.id}/last-frame")).status_code == 404
    folder = settings.browser_sessions_root / str(row.id)
    folder.mkdir(parents=True)
    (folder / "last.jpg").write_bytes(JPEG)
    resp = await auth_client.get(f"/api/v1/browser-sessions/{row.id}/last-frame")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.content == JPEG
    listing = (await auth_client.get("/api/v1/browser-sessions")).json()
    assert listing[0]["has_last_frame"] is True
