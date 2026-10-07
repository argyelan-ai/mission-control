"""Isolation of browser sessions (ADR-088 isolation step, `CDP_GATEWAY_ISOLATE=1`).

A `/s/<token>/` connection only ever sees and touches what belongs to its
session: tabs in the session's own browser contexts (or tabs it created),
never another session's or an agent's. Two layers are tested here:

* `SessionIsolation` — the per-connection message filter, pure and
  synchronous apart from the injected "make my default context" call;
* the gateway over real sockets with a fake Chromium (a `websockets`
  server), for the frame plumbing: masking both ways, fragmented messages,
  the gateway's own injected commands, `Browser.close` never reaching
  Chromium, page sockets for foreign tabs refused, and the switch being off
  by default.

The real-Chromium half (a Puppeteer-pattern client next to Playwright,
including the waitForDebugger hang regression) is `../test_isolation.sh`.
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest
import pytest_asyncio
import websockets
from websockets.datastructures import Headers
from websockets.http11 import Response

sys.path.insert(0, str(Path(__file__).parent))

import cdp_gateway  # noqa: E402
from cdp_gateway import CdpGateway, GatewayState, SessionIsolation, TargetInfo, start_server  # noqa: E402

ME = "s/11111111-1111-4111-8111-111111111111"
OTHER = "s/22222222-2222-4222-8222-222222222222"
TOKEN_ME = "tok-me-" + "a" * 30
TOKEN_OTHER = "tok-other-" + "b" * 30


def _state() -> GatewayState:
    st = GatewayState()
    st.ctx_owner.update({"CTX-ME": ME, "CTX-OTHER": OTHER, "CTX-AGENT": "alpha"})
    for tid, ctx in (("T-ME", "CTX-ME"), ("T-OTHER", "CTX-OTHER"), ("T-AGENT", "CTX-AGENT"), ("T-DEFAULT", "CTX-DEFAULT")):
        st.targets[tid] = TargetInfo(id=tid, ctx=ctx, agent=st.ctx_owner.get(ctx))
    return st


def _iso(state=None, made=None):
    made = made if made is not None else []

    async def default_context():
        made.append(1)
        state_ = iso.state
        state_.ctx_owner["CTX-MINE-DEFAULT"] = ME
        return "CTX-MINE-DEFAULT"

    iso = SessionIsolation(state or _state(), ME, default_context)
    return iso, made


def _info(tid, ctx, kind="page"):
    return {"targetId": tid, "type": kind, "browserContextId": ctx, "url": "about:blank", "title": ""}


# ── client -> Chromium ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_new_tab_without_context_goes_into_the_sessions_own_context():
    iso, made = _iso()
    up, back, close = await iso.from_client({"id": 1, "method": "Target.createTarget", "params": {"url": "about:blank"}})
    assert back is None and not close
    assert up["params"]["browserContextId"] == "CTX-MINE-DEFAULT"


@pytest.mark.asyncio
async def test_a_new_tab_in_a_foreign_context_is_refused():
    iso, _ = _iso()
    for ctx in ("CTX-OTHER", "CTX-AGENT", "CTX-DEFAULT"):
        up, back, _ = await iso.from_client({"id": 7, "method": "Target.createTarget", "params": {"url": "x", "browserContextId": ctx}})
        assert up is None
        assert back["id"] == 7 and "error" in back
    up, back, _ = await iso.from_client({"id": 8, "method": "Target.createTarget", "params": {"browserContextId": "CTX-ME"}})
    assert up is not None and back is None                  # its own context is fine


@pytest.mark.asyncio
async def test_foreign_tabs_cannot_be_attached_closed_or_activated():
    iso, _ = _iso()
    for method in ("Target.attachToTarget", "Target.closeTarget", "Target.activateTarget", "Target.getTargetInfo"):
        for tid in ("T-OTHER", "T-AGENT", "T-DEFAULT", "T-UNKNOWN"):
            up, back, _ = await iso.from_client({"id": 3, "method": method, "params": {"targetId": tid}})
            assert up is None, (method, tid)
            assert back["error"]["message"] == "No target with given id found"
        up, back, _ = await iso.from_client({"id": 4, "method": method, "params": {"targetId": "T-ME"}})
        assert up is not None and back is None, method


@pytest.mark.asyncio
async def test_a_foreign_context_cannot_be_disposed():
    iso, _ = _iso()
    up, back, _ = await iso.from_client({"id": 5, "method": "Target.disposeBrowserContext", "params": {"browserContextId": "CTX-OTHER"}})
    assert up is None and "error" in back


@pytest.mark.asyncio
async def test_browser_close_never_reaches_chromium_and_ends_only_this_connection():
    iso, _ = _iso()
    for method in ("Browser.close", "Browser.crash"):
        up, back, close = await iso.from_client({"id": 9, "method": method})
        assert up is None and close
        assert back == {"id": 9, "result": {}}
    up, back, close = await iso.from_client({"id": 10, "method": "Browser.crashGpuProcess"})
    assert up is None and not close and back == {"id": 10, "result": {}}


@pytest.mark.asyncio
async def test_cookies_without_a_context_are_the_sessions_own():
    iso, _ = _iso()
    for method in ("Storage.getCookies", "Storage.setCookies", "Storage.clearCookies", "Browser.grantPermissions"):
        up, back, _ = await iso.from_client({"id": 11, "method": method, "params": {}})
        assert back is None and up["params"]["browserContextId"] == "CTX-MINE-DEFAULT", method


@pytest.mark.asyncio
async def test_browser_wide_commands_that_reach_other_sessions_are_refused():
    iso, _ = _iso()
    for method in ("Storage.clearDataForOrigin", "Tracing.start", "Browser.executeBrowserCommand"):
        up, back, _ = await iso.from_client({"id": 12, "method": method, "params": {"origin": "http://x"}})
        assert up is None and "error" in back, method


@pytest.mark.asyncio
async def test_a_session_id_this_connection_does_not_hold_is_refused():
    iso, _ = _iso()
    up, back, _ = await iso.from_client({"id": 13, "sessionId": "FOREIGN", "method": "Runtime.evaluate", "params": {}})
    assert up is None and back["sessionId"] == "FOREIGN" and "error" in back


# ── Chromium -> client ───────────────────────────────────────────────────


def test_foreign_target_events_are_dropped_own_ones_pass():
    iso, _ = _iso()
    for ev in ("Target.targetCreated", "Target.targetInfoChanged"):
        out, inject = iso.from_upstream({"method": ev, "params": {"targetInfo": _info("T-OTHER", "CTX-OTHER")}})
        assert out is None and inject == []
        out, _ = iso.from_upstream({"method": ev, "params": {"targetInfo": _info("T-ME", "CTX-ME")}})
        assert out is not None
    # destroyed/crashed carry no context: they pass for tabs this client was told about
    assert iso.from_upstream({"method": "Target.targetDestroyed", "params": {"targetId": "T-OTHER"}})[0] is None
    assert iso.from_upstream({"method": "Target.targetDestroyed", "params": {"targetId": "T-ME"}})[0] is not None


def test_a_new_tab_is_recognised_by_its_context_before_any_owner_is_recorded():
    # Chromium announces a tab (targetCreated, attachedToTarget) BEFORE it
    # answers the createTarget that made it — only the context says whose it is.
    iso, _ = _iso()
    out, _ = iso.from_upstream({"method": "Target.targetCreated", "params": {"targetInfo": _info("T-NEW", "CTX-ME")}})
    assert out is not None


def test_a_foreign_tab_waiting_for_the_debugger_is_resumed_and_let_go():
    """The hang regression: a Puppeteer/Playwright client auto-attaches with
    waitForDebuggerOnStart — every NEW tab in the browser waits until that
    client resumes it. Dropping the event without resuming would freeze
    every other session's new tabs."""
    iso, _ = _iso()
    out, inject = iso.from_upstream({
        "method": "Target.attachedToTarget",
        "params": {"sessionId": "S-FOREIGN", "targetInfo": _info("T-OTHER", "CTX-OTHER"), "waitingForDebugger": True},
    })
    assert out is None
    assert [m["method"] for m in inject] == ["Runtime.runIfWaitingForDebugger", "Target.detachFromTarget"]
    assert inject[0]["sessionId"] == "S-FOREIGN"
    assert inject[1]["params"] == {"sessionId": "S-FOREIGN"}
    assert all(m["id"] >= cdp_gateway._OWN_MSG_ID_BASE for m in inject)
    # Their answers never reach the client.
    for m in inject:
        reply = {"id": m["id"], "result": {}}
        if "sessionId" in m:
            reply["sessionId"] = m["sessionId"]
        assert iso.from_upstream(reply)[0] is None
    # Events of that foreign session in flight are dropped too.
    assert iso.from_upstream({"sessionId": "S-FOREIGN", "method": "Runtime.executionContextCreated", "params": {}})[0] is None


def test_a_foreign_tab_already_running_is_only_let_go():
    iso, _ = _iso()
    out, inject = iso.from_upstream({
        "method": "Target.attachedToTarget",
        "params": {"sessionId": "S-F2", "targetInfo": _info("T-AGENT", "CTX-AGENT"), "waitingForDebugger": False},
    })
    assert out is None and [m["method"] for m in inject] == ["Target.detachFromTarget"]


@pytest.mark.asyncio
async def test_an_own_auto_attached_tab_becomes_usable():
    iso, _ = _iso()
    out, inject = iso.from_upstream({
        "method": "Target.attachedToTarget",
        "params": {"sessionId": "S-MINE", "targetInfo": _info("T-ME", "CTX-ME"), "waitingForDebugger": True},
    })
    assert out is not None and inject == []
    up, back, _ = await iso.from_client({"id": 20, "sessionId": "S-MINE", "method": "Runtime.runIfWaitingForDebugger"})
    assert up is not None and back is None
    assert iso.from_upstream({"sessionId": "S-MINE", "method": "Page.loadEventFired", "params": {}})[0] is not None


@pytest.mark.asyncio
async def test_nested_auto_attach_follows_the_child_not_the_parent():
    iso, _ = _iso()
    iso.from_upstream({"method": "Target.attachedToTarget",
                       "params": {"sessionId": "S-MINE", "targetInfo": _info("T-ME", "CTX-ME"), "waitingForDebugger": False}})
    out, _ = iso.from_upstream({"sessionId": "S-MINE", "method": "Target.attachedToTarget",
                                "params": {"sessionId": "S-IFRAME", "targetInfo": _info("F-1", "CTX-ME", "iframe"), "waitingForDebugger": True}})
    assert out is not None
    out, inject = iso.from_upstream({"sessionId": "S-MINE", "method": "Target.attachedToTarget",
                                     "params": {"sessionId": "S-W", "targetInfo": _info("W-1", "CTX-OTHER", "worker"), "waitingForDebugger": True}})
    assert out is None
    assert inject[1] == {"id": inject[1]["id"], "sessionId": "S-MINE", "method": "Target.detachFromTarget", "params": {"sessionId": "S-W"}}


@pytest.mark.asyncio
async def test_target_and_context_lists_only_show_the_sessions_own():
    iso, _ = _iso()
    await iso.from_client({"id": 30, "method": "Target.getTargets", "params": {}})
    out, _ = iso.from_upstream({"id": 30, "result": {"targetInfos": [
        _info("T-ME", "CTX-ME"), _info("T-OTHER", "CTX-OTHER"), _info("T-AGENT", "CTX-AGENT"), _info("T-DEFAULT", "CTX-DEFAULT"),
    ]}})
    assert [t["targetId"] for t in out["result"]["targetInfos"]] == ["T-ME"]
    await iso.from_client({"id": 31, "method": "Target.getBrowserContexts"})
    out, _ = iso.from_upstream({"id": 31, "result": {"browserContextIds": ["CTX-ME", "CTX-OTHER", "CTX-AGENT"]}})
    assert out["result"]["browserContextIds"] == ["CTX-ME"]


@pytest.mark.asyncio
async def test_an_explicit_attach_to_an_own_tab_opens_a_usable_session():
    iso, _ = _iso()
    await iso.from_client({"id": 40, "method": "Target.attachToTarget", "params": {"targetId": "T-ME", "flatten": True}})
    iso.from_upstream({"id": 40, "result": {"sessionId": "S-EXPL"}})
    up, back, _ = await iso.from_client({"id": 41, "sessionId": "S-EXPL", "method": "Page.navigate", "params": {"url": "about:blank"}})
    assert up is not None and back is None
    out, _ = iso.from_upstream({"method": "Target.detachedFromTarget", "params": {"sessionId": "S-EXPL"}})
    assert out is not None
    up, back, _ = await iso.from_client({"id": 42, "sessionId": "S-EXPL", "method": "Page.navigate", "params": {}})
    assert up is None                                          # gone after detach


@pytest.mark.asyncio
async def test_a_default_context_that_cannot_be_made_is_an_error_answer():
    async def broken():
        raise RuntimeError("chromium unreachable")

    iso = SessionIsolation(_state(), ME, broken)
    up, back, close = await iso.from_client({"id": 50, "method": "Target.createTarget", "params": {"url": "x"}})
    assert up is None and not close and "error" in back


# ── over real sockets: gateway + fake Chromium ──────────────────────────


class FakeChromium:
    """A `websockets` server standing in for Chromium's debug port: answers
    /json/version, records every message, and lets a test push messages."""

    def __init__(self):
        self.received: list[dict] = []
        self.paths: list[str] = []
        self.conns: list = []
        self.contexts = 0
        self.server = None
        self.port = None

    async def start(self):
        async def process_request(connection, request):
            if request.headers.get("Upgrade", "").lower() == "websocket":
                return None
            body = json.dumps({"webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/browser/fake"}).encode()
            return Response(200, "OK", Headers({"Content-Type": "application/json", "Content-Length": str(len(body))}), body)

        async def handler(ws):
            self.paths.append(ws.request.path)
            self.conns.append(ws)
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    self.received.append(msg)
                    if msg.get("method") == "Target.createBrowserContext":
                        self.contexts += 1
                        await ws.send(json.dumps({"id": msg["id"], "result": {"browserContextId": f"CTX-GW-{self.contexts}"}}))
                    elif msg.get("method") == "Target.createTarget" and msg.get("id", 0) >= cdp_gateway._OWN_MSG_ID_BASE:
                        await ws.send(json.dumps({"id": msg["id"], "result": {"targetId": "T-HTTP"}}))
                    elif msg.get("id", 0) >= cdp_gateway._OWN_MSG_ID_BASE:
                        await ws.send(json.dumps({"id": msg["id"], "sessionId": msg.get("sessionId"), "result": {}}))
            except websockets.ConnectionClosed:
                pass

        self.server = await websockets.serve(handler, "127.0.0.1", 0, process_request=process_request, max_size=None)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def push(self, msg, conn=-1):
        await self.conns[conn].send(json.dumps(msg))


@pytest_asyncio.fixture
async def rig():
    fake = FakeChromium()
    await fake.start()

    async def make(isolate=True):
        gw = CdpGateway(upstream_host="127.0.0.1", upstream_port=fake.port, isolate=isolate)
        gw.state.register_session(TOKEN_ME, ME[2:], None)
        gw.state.register_session(TOKEN_OTHER, OTHER[2:], None)
        gw.state.ctx_owner.update({"CTX-OTHER": OTHER})
        gw.state.targets["T-OTHER"] = TargetInfo(id="T-OTHER", ctx="CTX-OTHER", agent=OTHER)
        server = await start_server(gw, "127.0.0.1", 0)
        made.append(server)
        return gw, server.sockets[0].getsockname()[1]

    made = []
    yield fake, make
    for server in made:
        server.close()
        await server.wait_closed()
    await fake.stop()


async def _recv(ws, timeout=2.0):
    return json.loads(await asyncio.wait_for(ws.recv(), timeout))


async def _quiet(ws, timeout=0.3):
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout)
    except asyncio.TimeoutError:
        return None
    return json.loads(raw)


@pytest.mark.asyncio
async def test_wire_new_tab_gets_the_session_context_and_foreign_events_never_arrive(rig):
    fake, make = rig
    _gw, port = await make()
    async with websockets.connect(f"ws://127.0.0.1:{port}/s/{TOKEN_ME}/devtools/browser/x", max_size=None) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Target.createTarget", "params": {"url": "about:blank"}}))
        for _ in range(50):
            sent = [m for m in fake.received if m.get("method") == "Target.createTarget"]
            if sent:
                break
            await asyncio.sleep(0.02)
        assert sent[0]["params"]["browserContextId"] == "CTX-GW-1"
        # A foreign tab waiting for the debugger: resumed + let go by the gateway, invisible to the client.
        await fake.push({"method": "Target.attachedToTarget",
                         "params": {"sessionId": "S-F", "targetInfo": _info("T-OTHER", "CTX-OTHER"), "waitingForDebugger": True}}, conn=0)
        await fake.push({"method": "Target.targetCreated", "params": {"targetInfo": _info("T-OTHER", "CTX-OTHER")}}, conn=0)
        await fake.push({"method": "Target.targetCreated", "params": {"targetInfo": _info("T-MINE", "CTX-GW-1")}}, conn=0)
        got = await _recv(ws)
        assert got["params"]["targetInfo"]["targetId"] == "T-MINE"
        assert await _quiet(ws) is None
        methods = [(m.get("method"), m.get("sessionId")) for m in fake.received]
        assert ("Runtime.runIfWaitingForDebugger", "S-F") in methods
        assert ("Target.detachFromTarget", None) in methods


@pytest.mark.asyncio
async def test_wire_one_session_context_reused_across_connections_and_disposed_with_the_session(rig):
    fake, make = rig
    gw, port = await make()
    for n in (1, 2):
        async with websockets.connect(f"ws://127.0.0.1:{port}/s/{TOKEN_ME}/devtools/browser/x") as ws:
            await ws.send(json.dumps({"id": n, "method": "Target.createTarget", "params": {"url": "about:blank"}}))
            for _ in range(50):
                if len([m for m in fake.received if m.get("method") == "Target.createTarget"]) == n:
                    break
                await asyncio.sleep(0.02)
    assert fake.contexts == 1
    assert gw.state.ctx_owner["CTX-GW-1"] == ME
    result = await gw.end_session(TOKEN_ME)
    disposed = [m for m in fake.received if m.get("method") == "Target.disposeBrowserContext"]
    assert disposed and disposed[0]["params"]["browserContextId"] == "CTX-GW-1"
    assert result["disposedContexts"] == 1
    assert ME[2:] not in gw.state.session_contexts


@pytest.mark.asyncio
async def test_wire_large_and_fragmented_messages_pass_intact(rig):
    fake, make = rig
    _gw, port = await make()
    async with websockets.connect(f"ws://127.0.0.1:{port}/s/{TOKEN_ME}/devtools/browser/x", max_size=None) as ws:
        big = "x" * 200_000
        await ws.send(json.dumps({"id": 2, "method": "Target.setDiscoverTargets", "params": {"discover": True, "pad": big}}))
        # A fragmented client message (websockets sends an iterable as fragments).
        frag = json.dumps({"id": 3, "method": "Target.setAutoAttach", "params": {"autoAttach": True, "pad": big}})
        await ws.send([frag[:1000], frag[1000:150_000], frag[150_000:]])
        for _ in range(100):
            if len(fake.received) >= 3:
                break
            await asyncio.sleep(0.02)
        assert [m["id"] for m in fake.received[-2:]] == [2, 3]
        assert fake.received[-1]["params"]["pad"] == big
        await fake.push({"id": 2, "result": {"pad": big}}, conn=0)
        assert (await _recv(ws))["result"]["pad"] == big


@pytest.mark.asyncio
async def test_wire_browser_close_is_answered_and_only_this_connection_ends(rig):
    fake, make = rig
    _gw, port = await make()
    async with websockets.connect(f"ws://127.0.0.1:{port}/s/{TOKEN_ME}/devtools/browser/x") as ws:
        await ws.send(json.dumps({"id": 4, "method": "Browser.close"}))
        assert await _recv(ws) == {"id": 4, "result": {}}
        with pytest.raises(websockets.ConnectionClosed):
            await asyncio.wait_for(ws.recv(), 2.0)
    assert not any(m.get("method") == "Browser.close" for m in fake.received)


@pytest.mark.asyncio
async def test_wire_a_page_socket_for_a_foreign_tab_is_refused(rig):
    _fake, make = rig
    _gw, port = await make()
    with pytest.raises(websockets.InvalidStatus) as exc:
        await websockets.connect(f"ws://127.0.0.1:{port}/s/{TOKEN_ME}/devtools/page/T-OTHER")
    assert exc.value.response.status_code == 404


@pytest.mark.asyncio
async def test_wire_agent_and_unprefixed_connections_are_never_filtered(rig):
    fake, make = rig
    _gw, port = await make()
    for path in ("/a/alpha/devtools/browser/x", "/devtools/browser/x"):
        async with websockets.connect(f"ws://127.0.0.1:{port}{path}") as ws:
            await asyncio.sleep(0.05)
            await fake.push({"method": "Target.targetCreated", "params": {"targetInfo": _info("T-OTHER", "CTX-OTHER")}})
            assert (await _recv(ws))["params"]["targetInfo"]["targetId"] == "T-OTHER", path


@pytest.mark.asyncio
async def test_wire_isolation_is_off_unless_switched_on(rig, monkeypatch):
    fake, make = rig
    assert cdp_gateway.ISOLATE is False                       # CDP_GATEWAY_ISOLATE unset in the test env
    _gw, port = await make(isolate=cdp_gateway.ISOLATE)
    async with websockets.connect(f"ws://127.0.0.1:{port}/s/{TOKEN_ME}/devtools/browser/x") as ws:
        await asyncio.sleep(0.05)
        await fake.push({"method": "Target.targetCreated", "params": {"targetInfo": _info("T-OTHER", "CTX-OTHER")}})
        assert (await _recv(ws))["params"]["targetInfo"]["targetId"] == "T-OTHER"   # byte for byte, as before


@pytest.mark.asyncio
async def test_wire_json_new_for_an_isolated_session_opens_the_tab_in_its_context(rig):
    fake, make = rig
    gw, port = await make()
    status, _ctype, body = await gw.handle_http(f"/s/{TOKEN_ME}/json/new?about:blank", {"Host": f"127.0.0.1:{port}"}, None, "PUT")
    created = [m for m in fake.received if m.get("method") == "Target.createTarget"]
    assert created and created[0]["params"]["browserContextId"].startswith("CTX-GW-")
    assert status == 200
    tab = json.loads(body)
    assert tab["id"] == "T-HTTP" and tab["type"] == "page"
    assert tab["webSocketDebuggerUrl"] == f"ws://127.0.0.1:{port}/s/{TOKEN_ME}/devtools/page/T-HTTP"
    assert gw.state.target_creator["T-HTTP"] == ME


@pytest.mark.asyncio
async def test_wire_json_close_and_activate_refuse_foreign_tabs(rig):
    _fake, make = rig
    gw, _port = await make()
    for route in ("close", "activate"):
        status, _c, _b = await gw.handle_http(f"/s/{TOKEN_ME}/json/{route}/T-OTHER", {}, None, "GET")
        assert status == 404, route
