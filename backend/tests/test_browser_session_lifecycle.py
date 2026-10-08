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
        self.orphans: list[dict] = []
        self.health = 200
        self.requests: list[httpx.Request] = []
        self.router_requests: list[httpx.Request] = []

    def tab(self, tid, *, agent=None, session=None, idle=5.0, url="https://x.example", title="X", creator_session=None):
        self.targets.append({
            "targetId": tid, "title": title, "url": url, "agent": agent, "session": session,
            "browserContextId": None, "idleSeconds": idle,
            "creatorSession": creator_session if creator_session is not None else session,
        })

    def handler(self, request: httpx.Request) -> httpx.Response:
        if not self.reachable:
            raise httpx.ConnectError("down", request=request)
        if request.url.path.startswith("/_router/"):
            # The playwright-mcp router (separate service): stopping a
            # session's child is checked in test_heads_browser.py.
            self.router_requests.append(request)
            return httpx.Response(200, json={"stopped": True})
        self.requests.append(request)
        path = request.url.path
        if path == "/mc/health":
            return httpx.Response(self.health, text="ok" if self.health == 200 else "watcher not connected")
        if path == "/mc/targets":
            return httpx.Response(200, json=self.targets)
        if path == "/mc/sessions":
            return httpx.Response(200, json=[
                {"sessionId": v["session"], "agent": v["agent"], "tabs": 0, "contexts": 0, "connections": 0}
                for v in self.registered.values()
            ])
        if path == "/mc/orphans":
            return httpx.Response(200, json=self.orphans)
        if path == "/mc/orphans/close":
            return httpx.Response(200, json={"closedTargets": 1, "disposedContexts": 0, "errors": []})
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


async def test_agent_phase_ends_after_30_minutes_without_activity(session: AsyncSession, gw, make_agent, monkeypatch):
    """Sabotage: compare against the frame interval instead of the idle limit
    -> the phase ends far too early and the 'still active' half goes red."""
    monkeypatch.setattr(settings, "browser_idle_close_agent_tabs", True)  # explicit (default on since the F3 live test)
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
    # A missing run folder gets one pass of grace (F5) before the end.
    assert (await _get(gone.id)).status == "open"
    await _tick(session)
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
                     ended_at=utcnow() - timedelta(days=31), last_frame_at=utcnow() - timedelta(days=31))
    recent = await _add(owner_kind="head", head_run_id=str(uuid.uuid4()), status="ended",
                        ended_at=utcnow() - timedelta(days=29), last_frame_at=utcnow() - timedelta(days=29))
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



# ── review L3: gateway sessions whose row has ended ────────────────────────

async def test_a_gateway_session_whose_row_ended_is_ended_there_too(session: AsyncSession, gw, heads_root):  # noqa: F811
    """open/end race or a gateway 5xx on DELETE can leave the token alive at
    the gateway while the row is ended: the pass ends it there as well — but
    never a session whose row is still open."""
    run = make_run(heads_root, status={"phase": "running"}, heartbeat_age=5)
    ended = await _add(owner_kind="head", head_run_id=str(uuid.uuid4()), status="ended", ended_at=utcnow())
    alive = await _add(owner_kind="head", head_run_id=run)
    stranger = uuid.uuid4()   # registered at the gateway, no row at all
    for sid in (ended.id, alive.id, stranger):
        gw.registered[svc.session_token(sid)] = {"session": str(sid), "agent": None}
    report = await _tick(session)
    deleted = {r.url.path.rsplit("/", 1)[-1] for r in gw.calls("DELETE")}
    assert deleted == {svc.session_token(ended.id), svc.session_token(stranger)}
    assert report["stale_ended"] == 2
    assert (await _get(alive.id)).status == "open"


# ── review L1: orphan tabs of an ended session ─────────────────────────────

async def test_tabs_left_behind_by_an_ended_session_are_swept(session: AsyncSession, gw, heads_root):  # noqa: F811
    run = make_run(heads_root, status={"phase": "running"}, heartbeat_age=5)
    ended = await _add(owner_kind="head", head_run_id=str(uuid.uuid4()), status="ended", ended_at=utcnow())
    alive = await _add(owner_kind="head", head_run_id=run)
    gw.tab("ORPHAN", creator_session=str(ended.id), session=str(ended.id))
    gw.tab("LIVE", session=str(alive.id))
    # The gateway lists ended sessions that still own tabs OR contexts (a
    # context without tabs never shows up in /mc/targets, re-review N2).
    gw.orphans = [{"sessionId": str(ended.id), "tabs": 1, "contexts": 0},
                  {"sessionId": str(alive.id), "tabs": 1, "contexts": 0}]   # racing open: never swept
    report = await _tick(session)
    [sweep] = gw.calls("POST", "/mc/orphans/close")
    assert sweep.url.params["session"] == str(ended.id)
    assert report["swept"] == 1


async def test_only_ended_or_unknown_sessions_are_ended_or_swept_at_the_gateway(session: AsyncSession, gw):
    """Defense in depth for steps 6: re-read from the database right before
    acting, so an open session is never ended or swept at the gateway."""
    open_row = await _add(owner_kind="head", head_run_id=str(uuid.uuid4()))
    ended_row = await _add(owner_kind="head", head_run_id=str(uuid.uuid4()), status="ended", ended_at=utcnow())
    assert await svc._ended_or_unknown(session, str(open_row.id)) is False
    assert await svc._ended_or_unknown(session, str(ended_row.id)) is True
    assert await svc._ended_or_unknown(session, str(uuid.uuid4())) is True
    assert await svc._ended_or_unknown(session, "not-a-uuid") is False


async def test_a_context_left_without_tabs_is_swept_too(session: AsyncSession, gw):
    ended = await _add(owner_kind="head", head_run_id=str(uuid.uuid4()), status="ended", ended_at=utcnow())
    gw.orphans = [{"sessionId": str(ended.id), "tabs": 0, "contexts": 1}]
    report = await _tick(session)
    [sweep] = gw.calls("POST", "/mc/orphans/close")
    assert sweep.url.params["session"] == str(ended.id) and report["swept"] == 1



# ── review #760: F1 agent phases roll over at the age limit ────────────────

async def test_age_limit_rolls_an_agent_phase_over_without_closing_its_tabs(
    session: AsyncSession, gw, make_agent, monkeypatch,
):
    """An agent working without a break for 8 h must not lose its tabs: the
    phase is closed in the record and a new one opens at once.
    Sabotage: treat the agent like a head (hard end with its tabs) -> red."""
    monkeypatch.setattr(settings, "browser_idle_close_agent_tabs", True)
    agent = await make_agent(name="Alpha", slug="alpha")
    old = await _add(owner_kind="agent", agent_id=agent.id, status="live",
                     started_at=utcnow() - timedelta(seconds=settings.browser_session_max_age_s + 1),
                     last_active_at=utcnow() - timedelta(seconds=2))
    gw.tab("A1", agent="alpha", idle=2.0)
    report = await _tick(session)
    old = await _get(old.id)
    assert old.status == "ended" and old.end_reason == "max_age_rollover"
    [delete] = gw.calls("DELETE")
    assert "agent_tabs" not in delete.url.params
    await _tick(session)
    rows = [r for r in await _rows() if r.status != "ended"]
    assert len(rows) == 1 and rows[0].agent_id == agent.id   # the next phase is open
    assert report["ended"] == 1


async def test_closing_agent_tabs_is_on_by_default():
    """F3: a live test showed the local omp agent recovers from a tab closed
    under its open connection (its next browser call opened a working tab),
    so an idle working phase also closes the agent's idle tabs by default."""
    from app.config import Settings

    assert Settings.model_fields["browser_idle_close_agent_tabs"].default is True


# ── F2: frame retention gets through every expired row ─────────────────────

async def test_frame_retention_purges_more_than_one_batch(session: AsyncSession, gw):
    """Sabotage: newest-first without marking purged rows re-reads the same
    batch every pass and the older rows are never purged."""
    ended_at = utcnow() - timedelta(days=40)
    ids = []
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        for i in range(260):
            row = BrowserSession(owner_kind="head", head_run_id=str(uuid.uuid4()), status="ended",
                                 ended_at=ended_at + timedelta(seconds=i), last_frame_at=ended_at)
            s.add(row)
            ids.append(row.id)
        await s.commit()
    for sid in ids:
        folder = settings.browser_sessions_root / str(sid)
        folder.mkdir(parents=True)
        (folder / "last.jpg").write_bytes(JPEG)
    for _ in range(3):
        await _tick(session)
    left = [sid for sid in ids if (settings.browser_sessions_root / str(sid)).exists()]
    assert left == []


# ── F4: heads end at their own time limit ──────────────────────────────────

async def test_head_session_ends_at_the_runs_own_time_limit(session: AsyncSession, gw, heads_root):  # noqa: F811
    short = make_run(heads_root, status={"phase": "running"}, heartbeat_age=5, time_limit_s=3600)
    long_ = make_run(heads_root, status={"phase": "running"}, heartbeat_age=5, time_limit_s=10 * 3600)
    margin = svc._HEAD_LIMIT_MARGIN_S
    over = await _add(owner_kind="head", head_run_id=short, status="live",
                      started_at=utcnow() - timedelta(seconds=3600 + margin + 60))
    within = await _add(owner_kind="head", head_run_id=long_, status="live",
                        started_at=utcnow() - timedelta(hours=9))     # past the old fixed 8 h
    await _tick(session)
    assert (await _get(over.id)).end_reason == "max_age"
    assert (await _get(within.id)).status == "live"


# ── F5: a vanished head process gets a grace pass ──────────────────────────

async def test_a_vanished_head_process_gets_one_pass_of_grace(session: AsyncSession, gw, heads_root):  # noqa: F811
    run = make_run(heads_root, status={"phase": "running"}, heartbeat_age=600)   # failed/process_vanished
    row = await _add(owner_kind="head", head_run_id=run)
    await _tick(session)
    assert (await _get(row.id)).status == "open"
    await _tick(session)
    assert (await _get(row.id)).end_reason == "run_ended"


async def test_the_grace_resets_when_the_head_is_back(session: AsyncSession, gw, heads_root):  # noqa: F811
    import json as _json
    import os
    import time

    run = make_run(heads_root, status={"phase": "running"}, heartbeat_age=600)
    row = await _add(owner_kind="head", head_run_id=run)
    await _tick(session)
    hb = heads_root / run / ".wrapper" / "heartbeat"
    os.utime(hb, (time.time(), time.time()))                       # heartbeat again
    await _tick(session)
    os.utime(hb, (time.time() - 600, time.time() - 600))           # vanished again: grace starts over
    await _tick(session)
    assert (await _get(row.id)).status == "open"


# ── F6: the lifecycle client does not flood the log ────────────────────────

def test_gateway_request_logs_are_kept_out_of_info():
    import logging

    flt = svc.GatewayRequestLogFilter()

    def rec(level, url):
        return logging.LogRecord("httpx", level, __file__, 1, 'HTTP Request: %s %s "HTTP/1.1 200 OK"',
                                 ("GET", httpx.URL(url)), None)

    assert flt.filter(rec(logging.INFO, svc.GATEWAY_BASE_URL + "/mc/targets")) is False
    assert flt.filter(rec(logging.INFO, "https://api.example.invalid/v1")) is True
    assert flt.filter(rec(logging.WARNING, svc.GATEWAY_BASE_URL + "/mc/targets")) is True


# ── F7: no query strings in the stored URL ─────────────────────────────────

async def test_last_url_is_stored_without_query_or_fragment(session: AsyncSession, gw, make_agent):
    await make_agent(name="Alpha", slug="alpha")
    gw.tab("A1", agent="alpha", idle=1.0)
    original = gw.handler

    def handler(request):
        resp = original(request)
        if request.url.path.endswith("/snapshot"):
            import json as _json
            body = _json.loads(resp.content)
            body["url"] = "https://login.example/callback?code=OAUTHSECRET&state=x#tok=FRAG"
            return httpx.Response(200, json=body)
        return resp

    import app.services.browser_sessions as mod
    mod._transport = httpx.MockTransport(handler)
    await _tick(session)
    [row] = await _rows()
    assert row.last_url == "https://login.example/callback"


# ── F8: no idle decisions while the gateway's watcher reconnects ───────────

async def test_no_idle_decisions_while_the_gateway_is_not_healthy(session: AsyncSession, gw, make_agent, heads_root):  # noqa: F811
    agent = await make_agent(name="Alpha", slug="alpha")
    await make_agent(name="Beta", slug="beta")
    stale = await _add(owner_kind="agent", agent_id=agent.id, status="live",
                       started_at=utcnow() - timedelta(hours=1), last_active_at=utcnow() - timedelta(minutes=40))
    exited = make_run(heads_root, status={"phase": "exited", "reason": "stopped"})
    head = await _add(owner_kind="head", head_run_id=exited)
    gw.tab("B1", agent="beta", idle=0.0)       # looks fresh only because the watcher just reset
    gw.health = 503
    report = await _tick(session)
    assert (await _get(stale.id)).status == "live"                 # no idle end
    assert [r for r in await _rows() if r.agent_id and r.agent_id != agent.id] == []   # no lazy phase
    assert (await _get(head.id)).end_reason == "run_ended"         # the run's own status still counts
    assert report["gateway"] == "degraded"
