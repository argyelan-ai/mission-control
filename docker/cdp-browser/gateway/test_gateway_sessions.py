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


@pytest.mark.asyncio
async def test_end_session_does_not_report_already_gone_tabs_or_contexts_as_errors(monkeypatch):
    """Lab finding (real Chromium 124 + Playwright): cutting Playwright's
    connection disposes its `disposeOnDetach` context before our own
    disposeBrowserContext arrives -> "Failed to find context". That is the
    goal state, not a cleanup failure; anything else still is."""
    gateway = CdpGateway(upstream_host="127.0.0.1", upstream_port=1)
    gateway.state.register_session(TOKEN, SID, None)
    key = session_owner_key(SID)
    gateway.state.observe_response("Target.createBrowserContext", {}, {"browserContextId": "CTX-S"}, agent=key)
    gateway.state.observe_response("Target.createTarget", {}, {"targetId": "LOOSE"}, agent=key)
    _created(gateway.state, "LOOSE")
    gateway.state.observe_response("Target.createTarget", {}, {"targetId": "STUCK"}, agent=key)
    _created(gateway.state, "STUCK")

    async def fake_call(commands):
        replies = {
            "LOOSE": {"error": {"code": -32602, "message": "No target with given id found"}},
            "STUCK": {"error": {"code": -32000, "message": "Target is busy"}},
            "CTX-S": {"error": {"code": -32602, "message": "Failed to find context with id CTX-S"}},
        }
        return [replies[p.get("targetId") or p.get("browserContextId")] for _m, p in commands]

    monkeypatch.setattr(gateway, "_cdp_call", fake_call)
    result = await gateway.end_session(TOKEN)
    assert result["errors"] == ["Target.closeTarget: Target is busy"]


@pytest.mark.asyncio
async def test_agent_json_list_includes_tabs_of_its_own_context():
    """`/a/<slug>/json/list` used to match only tabs with a recorded owner
    entry (createTarget, claim); a tab that inherited its owner from the
    agent's own context (or opener) was missing. It now lists every tab
    `/mc/targets` attributes to the agent — the same rule, one place."""
    gateway = CdpGateway(upstream_host="127.0.0.1", upstream_port=1)
    gateway.state.observe_response("Target.createBrowserContext", {}, {"browserContextId": "CTX-A"}, agent="alpha")
    _created(gateway.state, "IN-CTX", ctx="CTX-A")
    _created(gateway.state, "OTHER")

    async def fake_upstream(method, path):
        pages = [{"id": tid, "type": "page", "webSocketDebuggerUrl": f"ws://127.0.0.1:9222/devtools/page/{tid}"}
                 for tid in ("IN-CTX", "OTHER")]
        return 200, "application/json", json.dumps(pages).encode()

    gateway._upstream_http = fake_upstream
    status, _ct, body = await _http(gateway, "GET", "/a/alpha/json/list")
    assert status == 200
    assert [p["id"] for p in json.loads(body)] == ["IN-CTX"]


# ── review fix M1: cleanup closes only what the session created ────────────

OTHER_TOKEN_2 = "tok_" + "C" * 40


def test_ending_a_session_never_closes_a_tab_it_only_claimed():
    """A Puppeteer/omp client on /s/<token>/ that does `pages()[0].goto()`
    claims a foreign tab (claim on use rewrites the display owner). That tab
    was never the session's to close: an agent's tab (BETA-TAB) or a tab in
    another session's own context (B-TAB) must survive the end of session A.
    Sabotage: base cleanup on the claim owner again -> both reappear here."""
    state = GatewayState(now_fn=lambda: 1.0)
    state.register_session(TOKEN, SID, None)
    state.register_session(OTHER_TOKEN_2, OTHER_SID, None)
    a, b = session_owner_key(SID), session_owner_key(OTHER_SID)
    # beta (a fixed agent) created BETA-TAB in the default context
    state.observe_response("Target.createTarget", {}, {"targetId": "BETA-TAB"}, agent="beta")
    _created(state, "BETA-TAB")
    # session B created a context and B-TAB inside it
    state.observe_response("Target.createBrowserContext", {}, {"browserContextId": "CTX-B"}, agent=b)
    _created(state, "B-TAB", ctx="CTX-B")
    # session A created its own tab
    state.observe_response("Target.createTarget", {}, {"targetId": "A-TAB"}, agent=a)
    _created(state, "A-TAB")
    # session A attaches to both foreign tabs and navigates them (claim on use)
    for tid, sess in (("BETA-TAB", "S-1"), ("B-TAB", "S-2")):
        state.observe_response("Target.attachToTarget", {"targetId": tid}, {"sessionId": sess}, agent=a)
        state.observe_command("Page.navigate", sess, a)
    assert state.owner_of("BETA-TAB") == a and state.owner_of("B-TAB") == a  # display follows the claim

    tabs, contexts = state.session_resources(SID)
    assert tabs == ["A-TAB"]
    assert contexts == []


def test_tabs_a_session_opened_by_popup_or_json_new_are_its_to_close():
    state = GatewayState(now_fn=lambda: 1.0)
    state.register_session(TOKEN, SID, None)
    a = session_owner_key(SID)
    state.observe_response("Target.createTarget", {}, {"targetId": "A-TAB"}, agent=a)
    _created(state, "A-TAB")
    _created(state, "POPUP", opener="A-TAB")          # window.open() from A's tab
    state.record_creator("JSON-NEW", a)               # PUT /json/new over A's address
    _created(state, "JSON-NEW")
    tabs, _ = state.session_resources(SID)
    assert tabs == ["A-TAB", "JSON-NEW", "POPUP"]


# ── review fix L5: cleanup answers within a deadline ───────────────────────

@pytest.mark.asyncio
async def test_cleanup_answers_within_its_deadline_even_if_chromium_hangs(monkeypatch):
    """The backend waits a bounded time for DELETE; the gateway must answer
    inside it even when Chromium never replies. Commands are sent in one go
    and unanswered ones are reported, not waited on one by one."""
    import cdp_gateway

    monkeypatch.setattr(cdp_gateway, "_CLEANUP_DEADLINE", 0.3)
    gateway = CdpGateway(upstream_host="127.0.0.1", upstream_port=1)
    gateway.state.register_session(TOKEN, SID, None)
    key = session_owner_key(SID)
    for tid in ("T1", "T2", "T3"):
        gateway.state.observe_response("Target.createTarget", {}, {"targetId": tid}, agent=key)
        _created(gateway.state, tid)

    async def hanging_version(path):
        return {"webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/browser/x"}

    class HangingWs:
        def __init__(self):
            self.sent = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def send(self, data):
            self.sent.append(json.loads(data))

        async def recv(self):
            await asyncio.sleep(3600)

    ws = HangingWs()
    monkeypatch.setattr(gateway, "_upstream_json", hanging_version)
    monkeypatch.setattr(cdp_gateway.websockets, "connect", lambda *a, **k: ws)
    start = asyncio.get_event_loop().time()
    # Bounded here too: a broken deadline must turn this red, not hang it.
    result = await asyncio.wait_for(gateway.end_session(TOKEN), timeout=2)
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed < 1.5
    assert len(ws.sent) == 3                       # all sent at once, not one per round trip
    assert result["closedTargets"] == 3
    assert any("unanswered" in e for e in result["errors"])


# ── re-review N1: a stalled client must not block the end ──────────────────

@pytest.mark.asyncio
async def test_ending_does_not_wait_for_a_client_that_stopped_reading():
    """A frozen harness stops reading while Chromium keeps sending: its send
    buffer fills and a polite close never completes. Ending the session must
    still answer promptly (cut hard) and go on to the tab cleanup. Before the
    fix DELETE hung forever. Probe from the independent re-review."""
    import socket
    import time

    upstream_writers = []

    async def upstream(reader, writer):
        upstream_writers.append(writer)
        while (await reader.readline()) not in (b"\r\n", b""):
            pass
        writer.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
        frame = b"\x82\x7e\xff\xff" + b"x" * 65535  # binary frames, 64 KiB each
        try:
            while True:
                writer.write(frame)
                await writer.drain()
        except Exception:
            pass

    up = await asyncio.start_server(upstream, "127.0.0.1", 0)
    gateway = CdpGateway(upstream_host="127.0.0.1", upstream_port=up.sockets[0].getsockname()[1])
    gateway.state.register_session(TOKEN, SID, None)
    cleanup_ran = asyncio.Event()

    async def no_cdp(commands, **kw):
        cleanup_ran.set()
        return []

    gateway._cdp_call = no_cdp
    gateway.state.observe_response("Target.createTarget", {}, {"targetId": "T"}, agent=session_owner_key(SID))
    _created(gateway.state, "T")
    client = socket.socket()
    # No `async with server`: its exit waits for every handler, and a hung
    # handler is exactly what this test catches — fail, don't hang.
    server = await start_server(gateway, host="127.0.0.1", port=0)
    try:
        port = server.sockets[0].getsockname()[1]
        client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        client.setblocking(False)
        await asyncio.get_running_loop().sock_connect(client, ("127.0.0.1", port))
        import base64

        ws_key = base64.b64encode(b"the sample nonce").decode()  # RFC 6455's example, built at runtime
        client.send((
            f"GET /s/{TOKEN}/devtools/browser/x HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {ws_key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        await asyncio.sleep(1.0)  # the client never reads; buffers fill up
        start = time.monotonic()
        result = await asyncio.wait_for(gateway.end_session(TOKEN), timeout=6)
        assert time.monotonic() - start < 4
        assert result["closedConnections"] == 1
        assert cleanup_ran.is_set()  # the tab cleanup was not skipped
    finally:
        client.close()
        for w in upstream_writers:
            w.transport.abort()
        server.close()
        up.close()
        for conns in gateway._live_conns.values():
            for task in conns:
                task.cancel()
