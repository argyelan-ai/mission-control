"""Browser sessions (ADR-088): one registered address `/s/<token>/` per
agent session or head run, next to the existing `/a/<slug>/` identity.

Covers the gateway half of PR S1: the session register (`PUT|GET|DELETE
/mc/sessions`), identification by token, attribution of tabs and contexts to
the session, `/mc/targets` session fields, and cleanup on DELETE (tabs,
contexts and the session's open CDP connections).

Run: `python3 -m pytest docker/cdp-browser/gateway -q` (same CI step as the
other gateway suites: websockets + httpx + pytest-asyncio only).
"""
import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest
import websockets

sys.path.insert(0, str(Path(__file__).parent))

from cdp_gateway import (  # noqa: E402
    CdpGateway,
    GatewayState,
    session_owner_key,
    start_server,
    strip_agent_prefix,
    token_from_path,
)
from test_gateway_integration import FakeChromium  # noqa: E402

TOKEN = "tok_" + "A" * 40
OTHER_TOKEN = "tok_" + "B" * 40
SID = "11111111-2222-3333-4444-555555555555"
OTHER_SID = "99999999-8888-7777-6666-555555555555"


class FakeHeaders(dict):
    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


def _created(state, tid, *, ctx=None, opener=None, url="https://x.example"):
    info = {"targetId": tid, "type": "page", "title": "", "url": url}
    if ctx:
        info["browserContextId"] = ctx
    if opener:
        info["openerId"] = opener
    state.apply_target_event("targetCreated", {"targetInfo": info})


# ── path parsing ───────────────────────────────────────────────────────────

def test_token_from_path_reads_session_prefix():
    assert token_from_path(f"/s/{TOKEN}/json/version") == TOKEN
    assert token_from_path(f"/s/{TOKEN}") == TOKEN
    assert token_from_path("/a/alpha/json/version") is None
    assert token_from_path("/json/version") is None


def test_token_from_path_rejects_malformed_tokens():
    assert token_from_path("/s/short/json/version") is None
    assert token_from_path("/s/" + "A" * 40 + "%2F/json") is None
    assert token_from_path("/s/../../etc/json/version") is None


def test_strip_agent_prefix_also_strips_session_prefix():
    assert strip_agent_prefix(f"/s/{TOKEN}/json/version") == "/json/version"
    assert strip_agent_prefix(f"/s/{TOKEN}/devtools/browser/x") == "/devtools/browser/x"
    # The /a/ shape is unchanged.
    assert strip_agent_prefix("/a/alpha/json/version") == "/json/version"


# ── register ───────────────────────────────────────────────────────────────

def test_register_session_is_idempotent_and_refuses_reuse():
    state = GatewayState()
    assert state.register_session(TOKEN, SID, "alpha") == "created"
    assert state.register_session(TOKEN, SID, "alpha") == "exists"
    with pytest.raises(ValueError):
        state.register_session(TOKEN, OTHER_SID, None)  # token already names another session
    with pytest.raises(ValueError):
        state.register_session(OTHER_TOKEN, SID, None)  # session already has another token
    assert state.session_for_token(TOKEN).session_id == SID
    assert state.session_for_token(OTHER_TOKEN) is None


# ── attribution ────────────────────────────────────────────────────────────

def test_tab_created_by_a_session_carries_session_and_its_agent():
    state = GatewayState(now_fn=lambda: 1.0)
    state.register_session(TOKEN, SID, "alpha")
    key = session_owner_key(SID)
    state.observe_response("Target.createTarget", {}, {"targetId": "T1"}, agent=key)
    _created(state, "T1", ctx="CTX-DEFAULT")

    [row] = state.as_mc_targets_json()
    assert row["session"] == SID
    # A session opened for an agent still counts as that agent's tab, so the
    # existing per-agent panel (?agent=<slug>) keeps working unchanged.
    assert row["agent"] == "alpha"
    assert row["browserContextId"] == "CTX-DEFAULT"
    assert [t["targetId"] for t in state.as_mc_targets_json(agent="alpha")] == ["T1"]
    assert [t["targetId"] for t in state.as_mc_targets_json(session=SID)] == ["T1"]
    assert state.as_mc_targets_json(session=OTHER_SID) == []


def test_tab_in_a_context_the_session_created_belongs_to_the_session():
    """Ownership by construction: Playwright `--isolated` creates its own
    browser context over the session's connection; every tab in it (and every
    popup) is the session's, without a createTarget of our own to see."""
    state = GatewayState(now_fn=lambda: 1.0)
    state.register_session(TOKEN, SID, None)
    state.observe_response(
        "Target.createBrowserContext", {}, {"browserContextId": "CTX-S"}, agent=session_owner_key(SID),
    )
    _created(state, "T1", ctx="CTX-S")
    _created(state, "POPUP", ctx="CTX-S", opener="T1")

    rows = {t["targetId"]: t for t in state.as_mc_targets_json()}
    assert rows["T1"]["session"] == SID and rows["POPUP"]["session"] == SID
    assert rows["T1"]["agent"] is None  # head run: no agent behind it


def test_agent_tabs_report_no_session():
    state = GatewayState(now_fn=lambda: 1.0)
    state.observe_response("Target.createTarget", {}, {"targetId": "T1"}, agent="alpha")
    _created(state, "T1")
    [row] = state.as_mc_targets_json()
    assert row["agent"] == "alpha" and row["session"] is None


def test_session_resources_lists_tabs_and_contexts_to_clean_up():
    state = GatewayState(now_fn=lambda: 1.0)
    state.register_session(TOKEN, SID, "alpha")
    key = session_owner_key(SID)
    state.observe_response("Target.createBrowserContext", {}, {"browserContextId": "CTX-S"}, agent=key)
    _created(state, "IN-CTX", ctx="CTX-S")
    state.observe_response("Target.createTarget", {}, {"targetId": "DEFAULT-CTX-TAB"}, agent=key)
    _created(state, "DEFAULT-CTX-TAB")
    state.observe_response("Target.createTarget", {}, {"targetId": "FOREIGN"}, agent="beta")
    _created(state, "FOREIGN")

    targets, contexts = state.session_resources(SID)
    # Tabs inside an owned context go away with the context; only the tab in
    # the shared default context needs its own closeTarget.
    assert targets == ["DEFAULT-CTX-TAB"]
    assert contexts == ["CTX-S"]


# ── HTTP: register, list, unknown token ────────────────────────────────────

async def _http(gateway, method, path, headers=None):
    return await gateway.handle_http(path, FakeHeaders(headers or {"Host": "cdp-browser:9300"}), "10.0.0.5", method)


@pytest.mark.asyncio
async def test_put_session_registers_and_get_lists_without_tokens():
    gateway = CdpGateway(upstream_host="127.0.0.1", upstream_port=1)
    status, _ct, body = await _http(gateway, "PUT", f"/mc/sessions/{TOKEN}?session={SID}&agent=alpha")
    assert status == 201 and json.loads(body)["sessionId"] == SID
    status, _ct, _body = await _http(gateway, "PUT", f"/mc/sessions/{TOKEN}?session={SID}&agent=alpha")
    assert status == 200  # idempotent re-register (MC after a gateway restart)

    status, _ct, body = await _http(gateway, "GET", "/mc/sessions")
    assert status == 200
    [row] = json.loads(body)
    assert row["sessionId"] == SID and row["agent"] == "alpha"
    assert row["tabs"] == 0 and row["connections"] == 0
    # The token is the session's credential; the listing never shows it.
    assert TOKEN.encode() not in body


@pytest.mark.asyncio
async def test_put_session_rejects_bad_input_and_conflicts():
    gateway = CdpGateway(upstream_host="127.0.0.1", upstream_port=1)
    assert (await _http(gateway, "PUT", f"/mc/sessions/short?session={SID}"))[0] == 400
    assert (await _http(gateway, "PUT", f"/mc/sessions/{TOKEN}"))[0] == 400
    assert (await _http(gateway, "PUT", f"/mc/sessions/{TOKEN}?session=not-a-uuid"))[0] == 400
    assert (await _http(gateway, "PUT", f"/mc/sessions/{TOKEN}?session={SID}&agent=../x"))[0] == 400
    assert (await _http(gateway, "PUT", f"/mc/sessions/{TOKEN}?session={SID}"))[0] == 201
    assert (await _http(gateway, "PUT", f"/mc/sessions/{TOKEN}?session={OTHER_SID}"))[0] == 409
    assert (await _http(gateway, "POST", f"/mc/sessions/{TOKEN}?session={SID}"))[0] == 405


@pytest.mark.asyncio
async def test_unknown_session_token_is_refused_not_treated_as_shared():
    """The token is a credential, not a label: an unregistered (ended,
    mistyped, forged) address must fail loudly instead of silently becoming
    an unidentified `_shared` connection. Never reaches Chromium."""
    gateway = CdpGateway(upstream_host="127.0.0.1", upstream_port=1)  # nothing listens there
    status, _ct, body = await _http(gateway, "GET", f"/s/{TOKEN}/json/version")
    assert status == 404
    assert b"unknown browser session" in body


@pytest.mark.asyncio
async def test_mc_targets_accepts_session_filter():
    gateway = CdpGateway(upstream_host="127.0.0.1", upstream_port=1)
    gateway.state.register_session(TOKEN, SID, None)
    gateway.state.observe_response("Target.createTarget", {}, {"targetId": "T1"}, agent=session_owner_key(SID))
    _created(gateway.state, "T1")
    _created(gateway.state, "OTHER")
    status, _ct, body = await _http(gateway, "GET", f"/mc/targets?session={SID}")
    assert status == 200
    assert [t["targetId"] for t in json.loads(body)] == ["T1"]


# ── over real sockets (fake Chromium) ──────────────────────────────────────

@pytest.mark.asyncio
async def test_session_address_rewrites_and_attributes_over_the_wire():
    chromium = FakeChromium()
    await chromium.start()
    gateway = CdpGateway(upstream_host="127.0.0.1", upstream_port=chromium.port)
    gateway.state.register_session(TOKEN, SID, "alpha")
    try:
        async with await start_server(gateway, host="127.0.0.1", port=0) as server:
            port = server.sockets[0].getsockname()[1]
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"http://127.0.0.1:{port}/s/{TOKEN}/json/version")
            assert resp.status_code == 200
            assert resp.json()["webSocketDebuggerUrl"].startswith(f"ws://127.0.0.1:{port}/s/{TOKEN}/devtools/")

            async with websockets.connect(f"ws://127.0.0.1:{port}/s/{TOKEN}/devtools/browser/abc") as ws:
                await ws.send(json.dumps({"id": 1, "method": "Target.createTarget", "params": {"url": "about:blank"}}))
                await asyncio.wait_for(ws.recv(), timeout=5)
            await asyncio.sleep(0.1)
            assert gateway.state.owner_of("FAKE-T1") == session_owner_key(SID)
    finally:
        await chromium.stop()


@pytest.mark.asyncio
async def test_websocket_on_unknown_session_token_is_refused():
    chromium = FakeChromium()
    await chromium.start()
    gateway = CdpGateway(upstream_host="127.0.0.1", upstream_port=chromium.port)
    try:
        async with await start_server(gateway, host="127.0.0.1", port=0) as server:
            port = server.sockets[0].getsockname()[1]
            with pytest.raises(websockets.exceptions.InvalidStatus) as err:
                async with websockets.connect(f"ws://127.0.0.1:{port}/s/{TOKEN}/devtools/browser/abc"):
                    pass
            assert err.value.response.status_code == 404
            assert chromium.methods == []
    finally:
        await chromium.stop()


@pytest.mark.asyncio
async def test_delete_session_closes_its_tabs_contexts_and_connections():
    """End of a session = one call: the token stops working, the session's
    live CDP connections are cut (an agent can't keep driving a browser whose
    session MC has ended), its tabs and contexts are closed in Chromium.
    Sabotage: drop the connection cut in `end_session` -> the client below
    stays connected and this goes red."""
    chromium = FakeChromium()
    await chromium.start()
    gateway = CdpGateway(upstream_host="127.0.0.1", upstream_port=chromium.port)
    gateway.state.register_session(TOKEN, SID, None)
    key = session_owner_key(SID)
    gateway.state.observe_response("Target.createBrowserContext", {}, {"browserContextId": "CTX-S"}, agent=key)
    _created(gateway.state, "IN-CTX", ctx="CTX-S")
    gateway.state.observe_response("Target.createTarget", {}, {"targetId": "LOOSE"}, agent=key)
    _created(gateway.state, "LOOSE")
    _created(gateway.state, "NOT-OURS")
    try:
        async with await start_server(gateway, host="127.0.0.1", port=0) as server:
            port = server.sockets[0].getsockname()[1]
            ws = await websockets.connect(f"ws://127.0.0.1:{port}/s/{TOKEN}/devtools/browser/abc")
            await ws.send(json.dumps({"id": 1, "method": "Target.getTargets"}))
            await asyncio.wait_for(ws.recv(), timeout=5)

            async with httpx.AsyncClient() as client:
                resp = await client.delete(f"http://127.0.0.1:{port}/mc/sessions/{TOKEN}")
                assert resp.status_code == 200
                result = resp.json()
                assert result["closedTargets"] == 1 and result["disposedContexts"] == 1
                assert result["closedConnections"] == 1
                # The address is dead now.
                again = await client.get(f"http://127.0.0.1:{port}/s/{TOKEN}/json/version")
                assert again.status_code == 404
                gone = await client.delete(f"http://127.0.0.1:{port}/mc/sessions/{TOKEN}")
                assert gone.status_code == 404

            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await asyncio.wait_for(ws.recv(), timeout=2)
            assert "Target.closeTarget" in chromium.methods
            assert "Target.disposeBrowserContext" in chromium.methods
            assert gateway.state.session_for_token(TOKEN) is None
    finally:
        await chromium.stop()
