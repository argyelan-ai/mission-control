"""Browser-session lifecycle support in the gateway (ADR-088, lifecycle step).

What MC's lifecycle loop needs from the gateway: how long each tab has been
idle (`idleSeconds`), the session's last image (`/mc/sessions/<token>/snapshot`),
and — when an agent's working phase ends — closing that agent's tabs too
(`DELETE /mc/sessions/<token>?agent_tabs=1`), without cutting the agent's own
connections.
"""
import asyncio
import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from cdp_gateway import CdpGateway, GatewayState, session_owner_key  # noqa: E402

TOKEN = "tok_" + "L" * 40
SID = "11111111-2222-3333-4444-555555555555"


class FakeHeaders(dict):
    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


def _created(state, tid, *, ctx=None, url="https://x.example", title=""):
    info = {"targetId": tid, "type": "page", "title": title, "url": url}
    if ctx:
        info["browserContextId"] = ctx
    state.apply_target_event("targetCreated", {"targetInfo": info})


async def _http(gateway, method, path):
    return await gateway.handle_http(path, FakeHeaders({"Host": "cdp-browser:9300"}), "10.0.0.5", method)


# ── idleSeconds ────────────────────────────────────────────────────────────

def test_targets_report_how_long_each_tab_has_been_idle():
    clock = Clock(100.0)
    state = GatewayState(now_fn=clock)
    _created(state, "T1")
    clock.t = 160.0
    _created(state, "T2")
    clock.t = 190.0
    rows = {r["targetId"]: r for r in state.as_mc_targets_json()}
    assert rows["T1"]["idleSeconds"] == pytest.approx(90.0)
    assert rows["T2"]["idleSeconds"] == pytest.approx(30.0)


# ── snapshot ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_snapshot_captures_the_sessions_most_recently_active_tab(monkeypatch):
    clock = Clock(100.0)
    gateway = CdpGateway(upstream_host="127.0.0.1", upstream_port=1)
    gateway.state.now_fn = clock
    gateway.state.register_session(TOKEN, SID, None)
    key = session_owner_key(SID)
    gateway.state.observe_response("Target.createTarget", {}, {"targetId": "OLD"}, agent=key)
    _created(gateway.state, "OLD", url="https://old.example", title="Old")
    clock.t = 150.0
    gateway.state.observe_response("Target.createTarget", {}, {"targetId": "NEW"}, agent=key)
    _created(gateway.state, "NEW", url="https://new.example", title="Néw")
    _created(gateway.state, "FOREIGN")  # newest overall, but not the session's
    calls = []

    async def fake_page_call(target_id, method, params):
        calls.append((target_id, method, params))
        return {"result": {"data": base64.b64encode(b"JPEGBYTES").decode()}}

    monkeypatch.setattr(gateway, "_page_call", fake_page_call)
    status, ctype, body = await _http(gateway, "GET", f"/mc/sessions/{TOKEN}/snapshot")
    assert status == 200 and ctype == "application/json"
    snap = json.loads(body)
    assert snap["targetId"] == "NEW" and snap["url"] == "https://new.example" and snap["title"] == "Néw"
    assert base64.b64decode(snap["data"]) == b"JPEGBYTES"
    assert snap["mime"] == "image/jpeg"
    [(tid, method, params)] = calls
    assert (tid, method) == ("NEW", "Page.captureScreenshot")
    assert params["format"] == "jpeg"


@pytest.mark.asyncio
async def test_snapshot_of_an_agent_session_uses_the_agents_tabs(monkeypatch):
    """An agent's working phase (ADR-088 decision a) is opened lazily while
    the agent keeps talking over `/a/<slug>/`: its tabs are the agent's."""
    gateway = CdpGateway(upstream_host="127.0.0.1", upstream_port=1)
    gateway.state.register_session(TOKEN, SID, "alpha")
    gateway.state.observe_response("Target.createTarget", {}, {"targetId": "A1"}, agent="alpha")
    _created(gateway.state, "A1")

    async def fake_page_call(target_id, method, params):
        return {"result": {"data": base64.b64encode(b"X").decode()}}

    monkeypatch.setattr(gateway, "_page_call", fake_page_call)
    status, _ct, body = await _http(gateway, "GET", f"/mc/sessions/{TOKEN}/snapshot")
    assert status == 200 and json.loads(body)["targetId"] == "A1"


@pytest.mark.asyncio
async def test_snapshot_without_a_tab_or_for_an_unknown_token():
    gateway = CdpGateway(upstream_host="127.0.0.1", upstream_port=1)
    gateway.state.register_session(TOKEN, SID, None)
    assert (await _http(gateway, "GET", f"/mc/sessions/{TOKEN}/snapshot"))[0] == 204
    assert (await _http(gateway, "GET", f"/mc/sessions/{'tok_' + 'Z' * 40}/snapshot"))[0] == 404
    assert (await _http(gateway, "POST", f"/mc/sessions/{TOKEN}/snapshot"))[0] == 405


@pytest.mark.asyncio
async def test_snapshot_reports_a_failed_capture_as_502(monkeypatch):
    gateway = CdpGateway(upstream_host="127.0.0.1", upstream_port=1)
    gateway.state.register_session(TOKEN, SID, None)
    gateway.state.observe_response("Target.createTarget", {}, {"targetId": "T"}, agent=session_owner_key(SID))
    _created(gateway.state, "T")

    async def failing(target_id, method, params):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(gateway, "_page_call", failing)
    assert (await _http(gateway, "GET", f"/mc/sessions/{TOKEN}/snapshot"))[0] == 502


# ── ending an agent's working phase ────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_with_agent_tabs_also_closes_the_agents_tabs(monkeypatch):
    gateway = CdpGateway(upstream_host="127.0.0.1", upstream_port=1)
    gateway.state.register_session(TOKEN, SID, "alpha")
    gateway.state.observe_response("Target.createTarget", {}, {"targetId": "A1"}, agent="alpha")
    _created(gateway.state, "A1")
    gateway.state.observe_response("Target.createTarget", {}, {"targetId": "B1"}, agent="beta")
    _created(gateway.state, "B1")
    sent = []

    async def fake_call(commands):
        sent.extend(commands)
        return [{"result": {}} for _ in commands]

    monkeypatch.setattr(gateway, "_cdp_call", fake_call)
    status, _ct, body = await _http(gateway, "DELETE", f"/mc/sessions/{TOKEN}?agent_tabs=1")
    assert status == 200
    assert json.loads(body)["closedTargets"] == 1
    assert sent == [("Target.closeTarget", {"targetId": "A1"})]


@pytest.mark.asyncio
async def test_delete_without_agent_tabs_leaves_the_agents_tabs(monkeypatch):
    gateway = CdpGateway(upstream_host="127.0.0.1", upstream_port=1)
    gateway.state.register_session(TOKEN, SID, "alpha")
    gateway.state.observe_response("Target.createTarget", {}, {"targetId": "A1"}, agent="alpha")
    _created(gateway.state, "A1")
    sent = []

    async def fake_call(commands):
        sent.extend(commands)
        return [{"result": {}} for _ in commands]

    monkeypatch.setattr(gateway, "_cdp_call", fake_call)
    status, _ct, body = await _http(gateway, "DELETE", f"/mc/sessions/{TOKEN}")
    assert status == 200 and json.loads(body)["closedTargets"] == 0
    assert sent == []


# ── review L1: orphan tabs of an ended session ─────────────────────────────

def test_targets_report_which_session_created_a_tab():
    state = GatewayState(now_fn=Clock())
    state.register_session(TOKEN, SID, None)
    state.observe_response("Target.createTarget", {}, {"targetId": "T"}, agent=session_owner_key(SID))
    _created(state, "T")
    state.observe_response("Target.createTarget", {}, {"targetId": "A"}, agent="alpha")
    _created(state, "A")
    rows = {r["targetId"]: r for r in state.as_mc_targets_json()}
    assert rows["T"]["creatorSession"] == SID
    assert rows["A"]["creatorSession"] is None


@pytest.mark.asyncio
async def test_orphan_tabs_of_an_ended_session_can_be_closed(monkeypatch):
    """A `/json/new` still in flight during DELETE leaves a tab created by a
    session that no longer exists. MC's sweep closes exactly those — and is
    refused for a session that is still registered."""
    gateway = CdpGateway(upstream_host="127.0.0.1", upstream_port=1)
    key = session_owner_key(SID)
    gateway.state.record_creator("ORPHAN", key)
    _created(gateway.state, "ORPHAN")
    gateway.state.observe_response("Target.createTarget", {}, {"targetId": "CLAIMED"}, agent="beta")
    _created(gateway.state, "CLAIMED")
    gateway.state.record_owner("CLAIMED", key)       # the ended session had only claimed it
    sent = []

    async def fake_call(commands, **kw):
        sent.extend(commands)
        return [{"result": {}} for _ in commands]

    monkeypatch.setattr(gateway, "_cdp_call", fake_call)
    status, _ct, body = await _http(gateway, "POST", f"/mc/orphans/close?session={SID}")
    assert status == 200 and json.loads(body)["closedTargets"] == 1
    assert sent == [("Target.closeTarget", {"targetId": "ORPHAN"})]

    gateway.state.register_session(TOKEN, SID, None)
    assert (await _http(gateway, "POST", f"/mc/orphans/close?session={SID}"))[0] == 409
    assert (await _http(gateway, "POST", "/mc/orphans/close?session=nope"))[0] == 400


# ── review M1 carried into agent phases ────────────────────────────────────

@pytest.mark.asyncio
async def test_ending_an_agent_phase_never_closes_a_tab_another_owner_created(monkeypatch):
    gateway = CdpGateway(upstream_host="127.0.0.1", upstream_port=1)
    gateway.state.register_session(TOKEN, SID, "alpha")
    gateway.state.observe_response("Target.createTarget", {}, {"targetId": "A1"}, agent="alpha")
    _created(gateway.state, "A1")
    _created(gateway.state, "BLANK")                      # created by nobody known (the startup tab)
    gateway.state.record_owner("BLANK", "alpha")          # alpha claimed it
    gateway.state.observe_response("Target.createTarget", {}, {"targetId": "B1"}, agent="beta")
    _created(gateway.state, "B1")
    gateway.state.record_owner("B1", "alpha")             # alpha claimed beta's tab
    sent = []

    async def fake_call(commands, **kw):
        sent.extend(commands)
        return [{"result": {}} for _ in commands]

    monkeypatch.setattr(gateway, "_cdp_call", fake_call)
    await _http(gateway, "DELETE", f"/mc/sessions/{TOKEN}?agent_tabs=1")
    assert sorted(p["targetId"] for _m, p in sent) == ["A1", "BLANK"]


# ── session_agent pruning ──────────────────────────────────────────────────

def test_session_agent_map_is_pruned_once_it_grows(monkeypatch):
    import cdp_gateway

    monkeypatch.setattr(cdp_gateway, "_OWNER_MAP_SOFT_CAP", 3)
    state = GatewayState(now_fn=Clock())
    _created(state, "LIVE")
    live_key = session_owner_key("aaaaaaaa-0000-4000-8000-000000000000")
    state.session_agent["aaaaaaaa-0000-4000-8000-000000000000"] = "alpha"
    state.record_owner("LIVE", live_key)
    for i in range(5):
        sid = f"bbbbbbbb-0000-4000-8000-00000000000{i}"
        state.register_session(f"tok_{i}" + "x" * 40, sid, "beta")
        state.unregister_session(f"tok_{i}" + "x" * 40)
    _created(state, "GONE")
    state.apply_target_event("targetDestroyed", {"targetId": "GONE"})
    assert set(state.session_agent) == {"aaaaaaaa-0000-4000-8000-000000000000"}
