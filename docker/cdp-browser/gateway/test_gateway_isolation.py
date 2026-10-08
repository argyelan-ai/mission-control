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
import base64
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
            up, back, _ = await iso.from_client({"id": 3, "method": method, "params": {"targetId": tid, "flatten": True}})
            assert up is None, (method, tid)
            assert back["error"]["message"] == "No target with given id found"
        up, back, _ = await iso.from_client({"id": 4, "method": method, "params": {"targetId": "T-ME", "flatten": True}})
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
    for method in (
        "Storage.clearDataForOrigin", "Tracing.start", "Browser.executeBrowserCommand",
        "SystemInfo.getProcessInfo", "Target.setRemoteLocations", "Browser.getHistograms", "Tethering.bind",
    ):
        up, back, _ = await iso.from_client({"id": 12, "method": method, "params": {"origin": "http://x"}})
        assert up is None and "error" in back, method


@pytest.mark.asyncio
async def test_a_session_id_this_connection_does_not_hold_is_refused():
    iso, _ = _iso()
    up, back, _ = await iso.from_client({"id": 13, "sessionId": "FOREIGN", "method": "Runtime.evaluate", "params": {}})
    assert up is None and back["sessionId"] == "FOREIGN" and "error" in back


@pytest.mark.asyncio
async def test_wrapped_non_flat_sessions_are_refused():
    """A non-flat session carries commands inside a string the filter cannot
    read (`Target.sendMessageToTarget`): only flat sessions are allowed."""
    iso, _ = _iso()
    for msg in (
        {"id": 60, "method": "Target.sendMessageToTarget", "params": {"message": "{}", "sessionId": "S"}},
        {"id": 61, "method": "Target.attachToTarget", "params": {"targetId": "T-ME"}},
        {"id": 62, "method": "Target.attachToTarget", "params": {"targetId": "T-ME", "flatten": False}},
        {"id": 63, "method": "Target.setAutoAttach", "params": {"autoAttach": True, "waitForDebuggerOnStart": True}},
    ):
        up, back, _ = await iso.from_client(msg)
        assert up is None and "error" in back, msg


@pytest.mark.asyncio
async def test_a_session_id_inside_params_must_be_one_this_connection_holds():
    iso, _ = _iso()
    up, back, _ = await iso.from_client({"id": 64, "method": "Target.detachFromTarget", "params": {"sessionId": "S-FOREIGN"}})
    assert up is None and "error" in back
    iso.from_upstream({"method": "Target.attachedToTarget",
                       "params": {"sessionId": "S-MINE", "targetInfo": _info("T-ME", "CTX-ME"), "waitingForDebugger": False}})
    up, back, _ = await iso.from_client({"id": 65, "method": "Target.detachFromTarget", "params": {"sessionId": "S-MINE"}})
    assert up is not None and back is None


@pytest.mark.asyncio
async def test_only_windows_of_own_tabs_can_be_moved():
    iso, _ = _iso()
    up, back, _ = await iso.from_client({"id": 66, "method": "Browser.setWindowBounds", "params": {"windowId": 7, "bounds": {}}})
    assert up is None and "error" in back
    up, back, _ = await iso.from_client({"id": 67, "method": "Browser.getWindowForTarget", "params": {}})
    assert up is None and "error" in back                        # at browser level: name the tab
    await iso.from_client({"id": 68, "method": "Browser.getWindowForTarget", "params": {"targetId": "T-ME"}})
    iso.from_upstream({"id": 68, "result": {"windowId": 7, "bounds": {}}})
    up, back, _ = await iso.from_client({"id": 69, "method": "Browser.setWindowBounds", "params": {"windowId": 7, "bounds": {}}})
    assert up is not None and back is None


@pytest.mark.asyncio
async def test_the_gateway_made_session_context_cannot_be_disposed_by_the_client():
    st = _state()
    st.session_contexts[ME[2:]] = "CTX-ME"
    iso, _ = _iso(st)
    up, back, _ = await iso.from_client({"id": 70, "method": "Target.disposeBrowserContext", "params": {"browserContextId": "CTX-ME"}})
    assert up is None and "error" in back
    st.ctx_owner["CTX-MINE-OWN"] = ME                              # a context the client made itself
    up, back, _ = await iso.from_client({"id": 71, "method": "Target.disposeBrowserContext", "params": {"browserContextId": "CTX-MINE-OWN"}})
    assert up is not None and back is None


@pytest.mark.asyncio
async def test_command_ids_the_gateway_uses_are_refused_for_the_client():
    iso, _ = _iso()
    up, back, _ = await iso.from_client({"id": cdp_gateway._OWN_MSG_ID_BASE, "method": "Target.getTargets"})
    assert up is None and "error" in back


def test_foreign_targets_are_not_remembered():
    iso, _ = _iso()
    for n in range(50):
        iso.from_upstream({"method": "Target.targetCreated", "params": {"targetInfo": _info(f"T-F{n}", "CTX-OTHER")}})
        iso.from_upstream({"method": "Target.attachedToTarget",
                           "params": {"sessionId": f"S{n}", "targetInfo": _info(f"T-G{n}", "CTX-OTHER"), "waitingForDebugger": False}})
    assert iso._known_ctx == {} and iso._announced == set()


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
        self.ctx_delay = 0.0
        self.binary: list[bytes] = []
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
                    if isinstance(raw, bytes):
                        self.binary.append(raw)
                        continue
                    msg = json.loads(raw)
                    self.received.append(msg)
                    if msg.get("method") == "Target.createBrowserContext":
                        await asyncio.sleep(self.ctx_delay)
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
        frag = json.dumps({"id": 3, "method": "Target.setAutoAttach", "params": {"autoAttach": True, "flatten": True, "pad": big}})
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


@pytest.mark.asyncio
async def test_wire_a_context_made_while_the_session_ends_is_disposed_at_once(rig):
    fake, make = rig
    gw, _port = await make()
    fake.ctx_delay = 0.3
    attempt = asyncio.ensure_future(gw._session_context(ME))
    await asyncio.sleep(0.1)
    await gw.end_session(TOKEN_ME)
    with pytest.raises(RuntimeError):
        await attempt
    disposed = [m["params"]["browserContextId"] for m in fake.received if m.get("method") == "Target.disposeBrowserContext"]
    assert disposed == ["CTX-GW-1"]
    assert "CTX-GW-1" not in gw.state.ctx_owner


@pytest.mark.asyncio
async def test_wire_json_new_needs_put_like_chromium(rig):
    _fake, make = rig
    gw, port = await make()
    status, _c, _b = await gw.handle_http(f"/s/{TOKEN_ME}/json/new?about:blank", {"Host": f"127.0.0.1:{port}"}, None, "GET")
    assert status == 405


async def _raw_ws(port, path):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  f"Sec-WebSocket-Key: {base64.b64encode(bytes(16)).decode()}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    await writer.drain()
    head = await reader.readuntil(b"\r\n\r\n")
    assert b" 101 " in head.split(b"\r\n", 1)[0]
    return reader, writer


def _client_frame(opcode, payload, fin=True):
    head = bytes([(0x80 if fin else 0) | opcode])
    mask = b"\x01\x02\x03\x04"
    n = len(payload)
    head += bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + n.to_bytes(2, "big")
    return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


@pytest.mark.asyncio
async def test_wire_a_ping_between_fragments_and_a_close_round_trip_pass(rig):
    fake, make = rig
    _gw, port = await make()
    reader, writer = await _raw_ws(port, f"/s/{TOKEN_ME}/devtools/browser/x")
    body = json.dumps({"id": 80, "method": "Target.getBrowserContexts"}).encode()
    writer.write(_client_frame(0x1, body[:10], fin=False))
    writer.write(_client_frame(0x9, b"hi"))                       # ping in the middle of a message
    writer.write(_client_frame(0x0, body[10:], fin=True))
    await writer.drain()
    for _ in range(50):
        if any(m.get("id") == 80 for m in fake.received):
            break
        await asyncio.sleep(0.02)
    assert any(m.get("id") == 80 for m in fake.received)          # the joined message arrived whole
    pong = await asyncio.wait_for(reader.readexactly(4), 2)       # Chromium's pong came back through
    assert pong[0] & 0x0F == 0xA
    writer.write(_client_frame(0x8, (1000).to_bytes(2, "big")))  # close: answered, connection ends
    await writer.drain()
    rest = await asyncio.wait_for(reader.read(), 3)
    assert rest[:1] and rest[0] & 0x0F == 0x8
    writer.close()


# ── review of #766: fail closed, every session, codec ───────────────────


async def _iso_with_tab_session():
    iso, made = _iso()
    iso._sessions.add("S-ME")                     # an own tab's flat session (attach answered earlier)
    return iso, made


@pytest.mark.asyncio
async def test_browser_close_on_an_own_tab_session_or_page_socket_never_reaches_chromium():
    """Real Chromium 154 exits on Browser.close sent on a TAB session too."""
    iso, _ = await _iso_with_tab_session()
    for method in ("Browser.close", "Browser.crash"):
        up, back, close = await iso.from_client({"id": 1, "sessionId": "S-ME", "method": method})
        assert up is None and close and back["result"] == {}, method
    up, back, close = await iso.from_client({"id": 2, "sessionId": "S-ME", "method": "Browser.crashGpuProcess"})
    assert up is None and not close
    page, _ = _iso()
    page._page_socket = True
    up, back, close = await page.from_client({"id": 3, "method": "Browser.close"})
    assert up is None and close


@pytest.mark.asyncio
async def test_context_less_commands_on_an_own_tab_session_or_page_socket_use_the_sessions_context():
    """Storage's cookie trio, createTarget and the permission/download
    commands take their context from the parameter on EVERY session — none
    means Chromium's shared default context (verified on 154)."""
    methods = ("Storage.getCookies", "Storage.setCookies", "Storage.clearCookies", "Target.createTarget",
               "Browser.grantPermissions", "Browser.resetPermissions", "Browser.setPermission",
               "Browser.setDownloadBehavior", "Browser.cancelDownload")
    iso, _ = await _iso_with_tab_session()
    page, _ = _iso()
    page._page_socket = True
    for method in methods:
        up, back, _ = await iso.from_client({"id": 4, "sessionId": "S-ME", "method": method, "params": {}})
        assert back is None and up["params"]["browserContextId"] == "CTX-MINE-DEFAULT", method
        up, back, _ = await page.from_client({"id": 5, "method": method, "params": {}})
        assert back is None and up["params"]["browserContextId"] == "CTX-MINE-DEFAULT", method


@pytest.mark.asyncio
async def test_browser_wide_commands_are_refused_on_tab_sessions_too():
    iso, _ = await _iso_with_tab_session()
    for method in ("Target.setRemoteLocations", "Browser.executeBrowserCommand", "Browser.getHistograms"):
        up, back, _ = await iso.from_client({"id": 6, "sessionId": "S-ME", "method": method, "params": {}})
        assert up is None and "error" in back, method
    # A tab's own commands are not limited.
    for method in ("Runtime.evaluate", "Page.navigate", "Network.getAllCookies", "Browser.getVersion"):
        up, back, _ = await iso.from_client({"id": 7, "sessionId": "S-ME", "method": method, "params": {}})
        assert up is not None and back is None, method


@pytest.mark.asyncio
async def test_anything_that_is_not_a_well_formed_command_ends_the_connection():
    """Fail closed: Chromium runs `"id": 33.0`, which a check for int ids
    would wave through as "not a command"."""
    iso, _ = _iso()
    for msg in (
        {"id": 5.0, "method": "Target.attachToTarget", "params": {"targetId": "T-OTHER", "flatten": True}},
        {"id": True, "method": "Storage.getCookies"},
        {"id": "7", "method": "Storage.getCookies"},
        {"method": "Storage.getCookies"},
        {"id": 8},
        {"id": 9, "method": 3},
        {"id": 10, "method": "Target.getTargets", "params": []},
        {"id": 11, "method": "Target.getTargets", "sessionId": 4},
        {"id": -1, "method": "Target.getTargets"},
    ):
        up, back, close = await iso.from_client(msg)
        assert up is None and close, msg


class RawUpstream:
    """Raw TCP 'Chromium': answers the handshake with 101, records bytes."""

    def __init__(self):
        self.got = bytearray()
        self.writer = None

    async def start(self):
        async def handle(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                         b"Sec-WebSocket-Accept: x\r\n\r\n")
            await writer.drain()
            self.writer = writer
            while data := await reader.read(65536):
                self.got += data

        self.server = await asyncio.start_server(handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]


def _frame(b0, payload, masked=True, rsv=0):
    n = len(payload)
    head = bytes([b0 | rsv]) + (bytes([(0x80 if masked else 0) | n]) if n < 126
                                else bytes([(0x80 if masked else 0) | 126]) + n.to_bytes(2, "big"))
    if not masked:
        return head + payload
    mask = b"\x05\x06\x07\x08"
    return head + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(payload))


@pytest.mark.asyncio
async def test_switch_off_a_session_connection_is_passed_byte_for_byte_both_ways():
    """Compared as bytes, including frames the isolated path refuses."""
    up = RawUpstream()
    port_up = await up.start()
    gw = CdpGateway(upstream_host="127.0.0.1", upstream_port=port_up, isolate=False)
    gw.state.register_session(TOKEN_ME, ME[2:], None)
    server = await start_server(gw, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await _raw_ws_any(port, f"/s/{TOKEN_ME}/devtools/browser/x")
    sent = b"".join([
        _frame(0x81, b'{"id":1,"method":"Target.attachToTarget","params":{"targetId":"T-OTHER","x":"\t"}}'),
        _frame(0x01, b'{"id":2,'), _frame(0x89, b"ping"), _frame(0x80, b'"method":"Browser.close"}'),
        _frame(0x81, b'{"id":3,"method":"Browser.close"}', rsv=0x40),
        _frame(0x81, b'{"id":4,"method":"Browser.crash"}', masked=False),
        _frame(0x82, bytes(range(256)) * 200),
    ])
    writer.write(sent)
    await writer.drain()
    for _ in range(100):
        if len(up.got) >= len(sent):
            break
        await asyncio.sleep(0.02)
    assert bytes(up.got) == sent
    back = b"\x81\x7e\x01\x00" + b"y" * 256 + b"\x01\x03abc" + b"\x80\x01d" + b"\x8a\x00"
    up.writer.write(back)
    await up.writer.drain()
    assert await asyncio.wait_for(reader.readexactly(len(back)), 2) == back
    writer.close()
    server.close()
    up.server.close()


async def _raw_ws_any(port, path):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  f"Sec-WebSocket-Key: {base64.b64encode(bytes(16)).decode()}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    await writer.drain()
    await reader.readuntil(b"\r\n\r\n")
    return reader, writer


async def _ends_with_close(reader, timeout=3):
    """True when the gateway answers with a close frame and/or ends the connection."""
    try:
        data = await asyncio.wait_for(reader.read(), timeout)
    except asyncio.TimeoutError:
        return False
    return data == b"" or (data[0] & 0x0F) == 0x8


@pytest.mark.asyncio
async def test_wire_unparseable_or_binary_client_messages_end_the_connection(rig):
    """Chromium's JSON parser takes a raw TAB/LF inside a string and a float
    id, which Python's does not: such a frame must never be forwarded raw."""
    fake, make = rig
    _gw, port = await make()
    for payload, opcode in (
        (b'{"id":1,"method":"Target.getTargetInfo","params":{"targetId":"T-OTHER","x":"a,}}', 0x1),  # broken
        (b'{"id":2,"method":"Target.getTargetInfo","params":{"targetId":"T-OTHER",}}', 0x1),       # trailing comma
        (b'{"id":33.0,"method":"Storage.getCookies"}', 0x1),
        (b'{"id":4,"method":"Target.getTargetInfo","params":{"targetId":"T-OTHER","x":"\xff"}}', 0x1),  # bad UTF-8
        (json.dumps({"id": 5, "method": "Target.getTargets"}).encode(), 0x2),                      # binary
    ):
        before = len(fake.received)
        reader, writer = await _raw_ws_any(port, f"/s/{TOKEN_ME}/devtools/browser/x")
        writer.write(_frame(0x80 | opcode, payload))
        await writer.drain()
        assert await _ends_with_close(reader), payload
        assert len(fake.received) == before, payload           # nothing reached Chromium
        writer.close()


@pytest.mark.asyncio
async def test_wire_a_raw_tab_inside_a_string_is_checked_and_forwarded_as_the_filter_read_it(rig):
    fake, make = rig
    _gw, port = await make()
    reader, writer = await _raw_ws_any(port, f"/s/{TOKEN_ME}/devtools/browser/x")
    writer.write(_frame(0x81, b'{"id":6,"method":"Target.getTargetInfo","params":{"targetId":"T-OTHER","x":"a\tb"}}'))
    writer.write(_frame(0x81, b'{"id":7,"method":"Target.setDiscoverTargets","params":{"discover":true,"x":"a\tb"}}'))
    await writer.drain()
    head = await asyncio.wait_for(reader.readexactly(2), 2)
    answer = json.loads(await reader.readexactly(head[1] & 0x7F))
    assert answer["id"] == 6 and "error" in answer                 # the foreign tab is refused
    for _ in range(50):
        if any(m.get("id") == 7 for m in fake.received):
            break
        await asyncio.sleep(0.02)
    got = [m for m in fake.received if m.get("id") in (6, 7)]
    assert [m["id"] for m in got] == [7] and got[0]["params"]["x"] == "a\tb"
    writer.close()


@pytest.mark.asyncio
async def test_wire_frame_rules_end_the_connection(rig):
    """A new data frame inside an open fragmented message, an unmasked client
    frame, an oversized or fragmented control frame."""
    fake, make = rig
    _gw, port = await make()
    for frames in (
        [_frame(0x01, b'{"id":1,'), _frame(0x81, b'{"id":2,"method":"Target.getTargets"}')],
        [_frame(0x81, b'{"id":3,"method":"Target.getTargets"}', masked=False)],
        [_frame(0x89, b"p" * 126)],
        [_frame(0x09, b"ping")],
    ):
        before = len(fake.received)
        reader, writer = await _raw_ws_any(port, f"/s/{TOKEN_ME}/devtools/browser/x")
        writer.write(b"".join(frames))
        await writer.drain()
        assert await _ends_with_close(reader), frames
        assert len(fake.received) == before
        writer.close()


@pytest.mark.asyncio
async def test_wire_an_oversized_message_ends_the_connection_and_says_so_without_the_token(rig, monkeypatch, caplog):
    fake, make = rig
    monkeypatch.setattr(cdp_gateway, "_ISOLATED_MAX_MESSAGE", 1000)
    _gw, port = await make()
    reader, writer = await _raw_ws_any(port, f"/s/{TOKEN_ME}/devtools/browser/x")
    big = json.dumps({"id": 1, "method": "Target.getTargets", "params": {"pad": "x" * 1200}}).encode()
    writer.write(_frame(0x01, big[:600]) + _frame(0x80, big[600:]))
    await writer.drain()
    with caplog.at_level("INFO", logger="cdp_gateway"):
        assert await _ends_with_close(reader)
        await asyncio.sleep(0.1)
    assert any("too large" in r.getMessage() for r in caplog.records), [r.getMessage() for r in caplog.records]
    assert not any(TOKEN_ME in r.getMessage() for r in caplog.records)
    writer.close()
