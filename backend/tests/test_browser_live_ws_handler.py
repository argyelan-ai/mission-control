"""Integration tests for the /browser-live/ws handler's follow/switch logic
(review finding: "the core follow behaviour has no tests").

Strategy: a minimal FastAPI app mounting only the browser_live router (same
pattern as test_vault_stream.py's `_make_minimal_vault_app` — including its
`client.portal.call(...)` trick to seed the test user on the TestClient's
OWN event loop/thread, which is what the WS handler actually runs on), real
JWT auth via the legacy ?token= switch (like the other browser_live auth
tests), and a fake CDP layer:

- `run_target_watcher` is replaced so the test drives tab events directly
  (no real browser-level WS / httpx needed) — this is what lets tests (b)-(d)
  below be instant and deterministic.
- `websockets.connect` is replaced so each per-page CDP "connection" is an
  in-memory queue the test can push frames into or sever at will.
- `_list_page_targets` is replaced by a controllable page list.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import app.auth as auth_mod
import app.config
from app.auth import create_access_token
from app.models.user import User
from app.routers import browser_live as bl
from sqlmodel.ext.asyncio.session import AsyncSession
from tests.conftest import test_engine


# ── Fake CDP page connection ─────────────────────────────────────────────────


class FakePageConn:
    """Stands in for one `websockets.connect(<page ws url>)` result — usable
    both as `await websockets.connect(...)` (what attach_and_stream does
    post-fix) and as `async with websockets.connect(...) as ws:`."""

    def __init__(self, target_id: str):
        self.target_id = target_id
        self.sent: list[dict] = []
        self._queue: asyncio.Queue = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.closed = False

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def recv(self) -> str:
        item = await self._queue.get()
        if item is None:
            import websockets.exceptions as wse

            raise wse.ConnectionClosedOK(None, None)
        return item

    def push_frame(self) -> None:
        payload = json.dumps({
            "method": "Page.screencastFrame",
            "params": {"sessionId": 1, "data": "ZmFrZQ==", "metadata": {}},
        })
        assert self._loop is not None, "push_frame() before the fake connection ever recv()'d"
        self._loop.call_soon_threadsafe(self._queue.put_nowait, payload)

    def sever(self) -> None:
        """Simulate the page/tab closing under us (finding: must not kill
        the whole stream)."""
        assert self._loop is not None, "sever() before the fake connection ever recv()'d"
        self._loop.call_soon_threadsafe(self._queue.put_nowait, None)

    async def close(self) -> None:
        self.closed = True

    def __await__(self):
        async def _coro():
            return self

        return _coro().__await__()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()
        return False


class FakeCDPWorld:
    """Owns the page list + the connections handed out for them."""

    def __init__(self, pages: list[dict]):
        self.pages = pages
        self.conns: dict[str, FakePageConn] = {}
        # Every connect() call ever made, in order — `conns[id]` only ever
        # shows the LATEST connection for a target, which let a sabotage
        # (reconnecting to "a" a second time) hide behind the fact that
        # `len(conns["a"].sent) == 1` is still true for the NEW connection.
        # (finding: the background-tab-change test's own guard didn't guard.)
        self.all_conns: list[FakePageConn] = []

    def set_pages(self, pages: list[dict]) -> None:
        self.pages = pages

    async def list_page_targets(self) -> list[dict]:
        return list(self.pages)

    def connect_count(self, target_id: str) -> int:
        return sum(1 for c in self.all_conns if c.target_id == target_id)

    def connect(self, url: str, **kwargs) -> FakePageConn:
        # Our fake ws URLs are exactly f"ws://fake/{target_id}". Called
        # directly from `attach_and_stream` on the ASGI app's own event
        # loop/thread — capture it here (not in recv(), which the pump task
        # may not have reached yet by the time the test's main thread calls
        # push_frame()/sever()) so those can safely call_soon_threadsafe.
        target_id = url.rsplit("/", 1)[-1]
        conn = FakePageConn(target_id)
        conn._loop = asyncio.get_running_loop()
        self.conns[target_id] = conn
        self.all_conns.append(conn)
        return conn


def _page(tid: str, url: str = "https://example.com") -> dict:
    return {"id": tid, "title": tid, "url": url, "webSocketDebuggerUrl": f"ws://fake/{tid}"}


class EventBus:
    """Cross-thread-safe mailbox for (method, params) tuples: the test's
    main thread pushes CDP `Target.*` events, `_fake_watcher_driver` (below)
    consumes them on the TestClient's OWN event loop/thread. A plain
    `asyncio.Queue` is NOT safe for this — `put_nowait()` mutates its
    internal deque + waiter list without any cross-thread synchronization,
    which intermittently corrupted the app's event loop in early versions
    of this test (manifesting as an unrelated-looking `CancelledError` /
    `anyio.ClosedResourceError` at the WebSocket's `__exit__`)."""

    def __init__(self) -> None:
        self._items: list[tuple[str, dict]] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wakeup: asyncio.Event | None = None

    def bind(self) -> None:
        """Called once from the driver coroutine, on the app's own loop."""
        self._loop = asyncio.get_running_loop()
        self._wakeup = asyncio.Event()

    def push(self, method: str, params: dict) -> None:
        """Called from the test's thread."""
        assert self._loop is not None, "push() before the watcher driver started"

        def _apply() -> None:
            self._items.append((method, params))
            assert self._wakeup is not None
            self._wakeup.set()

        self._loop.call_soon_threadsafe(_apply)

    async def get(self) -> tuple[str, dict]:
        assert self._wakeup is not None
        while not self._items:
            self._wakeup.clear()
            await self._wakeup.wait()
        return self._items.pop(0)


def _fake_watcher_driver(events: EventBus):
    """Replacement for `run_target_watcher`: seeds from the current page list
    once, then applies CDP Target.* events the test pushes through `events`
    (method, params) — same shape `_watch_targets_browser_ws` would hand the
    watcher, so `TargetWatcher`'s real (already unit-tested) logic runs
    unmodified."""

    async def _driver(watcher, stop_event, *, initial_pages, ready_event=None):
        events.bind()
        watcher.replace_from_list(initial_pages)
        watcher.notify()
        if ready_event is not None:
            ready_event.set()
        while not stop_event.is_set():
            get = asyncio.ensure_future(events.get())
            stop = asyncio.ensure_future(stop_event.wait())
            try:
                done, pending = await asyncio.wait({get, stop}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                # `asyncio.wait()` does NOT cancel its member futures when the
                # coroutine AWAITING it is itself cancelled (only when `wait`
                # returns normally does the "for t in pending: t.cancel()"
                # below ever run) — so if this driver task is cancelled while
                # suspended here (the real-world shape of the handler's
                # `watcher_task.cancel()`), `get` (awaiting `events._wakeup`,
                # which nothing will ever set again) was left dangling
                # forever. A permanently-pending task like that can hang the
                # whole test process at event-loop/portal teardown — this
                # `finally` guarantees both futures are always cancelled,
                # cancellation-safe or not.
                for t in (get, stop):
                    if not t.done():
                        t.cancel()
            for t in pending:
                t.cancel()
            if stop in done:
                return
            method, params = get.result()
            if watcher.handle_event(method, params):
                watcher.notify()

    return _driver


def _make_app(world: FakeCDPWorld, events: EventBus) -> FastAPI:
    app = FastAPI()
    app.include_router(bl.router)

    driver = _fake_watcher_driver(events)

    async def _run_target_watcher(watcher, stop_event, *, ready_event=None):
        await driver(watcher, stop_event, initial_pages=world.pages, ready_event=ready_event)

    # Patched for the app's whole lifetime (module-level, restored by the
    # `with` block the test wraps the TestClient in).
    bl.run_target_watcher = _run_target_watcher  # type: ignore[assignment]
    bl._list_page_targets = world.list_page_targets  # type: ignore[assignment]
    return app


def _seed_user_and_token(client: TestClient, email: str) -> str:
    """Create the operator row and the legacy ?token= JWT on the TestClient's
    OWN event loop (its `portal`) — the WS handler's auth runs there, and a
    row inserted from pytest-asyncio's separate loop is invisible to it with
    SQLite's single-connection StaticPool test engine (this is exactly the
    pattern `test_vault_stream.py::_ticket_for` uses for the same reason)."""
    user_id = uuid.uuid4()

    async def _seed():
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            s.add(User(id=user_id, email=email, name="T", role="admin", is_active=True))
            await s.commit()

    client.portal.call(_seed)
    return create_access_token(str(user_id), "admin")


from contextlib import contextmanager


@contextmanager
def _ws(client: TestClient, url: str):
    """`client.websocket_connect(url)`, but swallows the teardown-only
    ``anyio.ClosedResourceError`` / ``CancelledError`` that Starlette's
    ``WebSocketTestSession.__exit__`` can raise when our handler's own
    `finally` (stop_event + `websocket.close()`) has already torn the
    server side down by the time the test's `with` block sends its own
    disconnect — a close-ordering race in the test harness, not something
    a real browser's close ever reports back to the app."""
    session = client.websocket_connect(url).__enter__()
    try:
        yield session
    finally:
        try:
            session.close()
        except Exception:
            pass


_RECV_TIMEOUT = 5.0


def _recv_with_timeout(ws, timeout: float):
    """`ws.receive_json()`, bounded by a wall-clock deadline. `anyio`'s test
    portal has no timeout parameter of its own, so the blocking call runs on
    a DAEMON thread we never join — a `ThreadPoolExecutor` used as a context
    manager calls `shutdown(wait=True)` on exit, which re-introduces the
    exact hang this is meant to prevent if the receive never returns (a
    regression that makes the handler stop sending). Raises
    `TimeoutError` on timeout; the leaked thread dies with the process."""
    import queue
    import threading

    q: "queue.Queue" = queue.Queue(maxsize=1)

    def _run():
        try:
            q.put(("ok", ws.receive_json()))
        except Exception as e:  # noqa: BLE001 - relayed to the caller's thread
            q.put(("err", e))

    threading.Thread(target=_run, daemon=True).start()
    try:
        kind, value = q.get(timeout=timeout)
    except queue.Empty:
        raise TimeoutError(f"no message within {timeout}s — server likely stopped sending") from None
    if kind == "err":
        raise value
    return value


def _recv_until(ws, predicate, max_messages: int = 50, timeout: float = _RECV_TIMEOUT):
    """Reads messages until `predicate` matches, with a per-call wall-clock
    deadline (finding: a stuck server used to hang this forever, needing an
    external alarm/kill in CI rather than a clear red test)."""
    deadline = time.monotonic() + timeout
    for _ in range(max_messages):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(
                f"no matching message within {timeout}s (predicate never matched — "
                "server likely stopped sending)"
            )
        try:
            msg = _recv_with_timeout(ws, remaining)
        except TimeoutError as e:
            raise AssertionError(str(e)) from None
        if predicate(msg):
            return msg
    raise AssertionError(f"predicate never matched within {max_messages} messages")


@pytest.fixture(autouse=True)
def _ws_session_on_test_engine(monkeypatch):
    """Same fix `_ticket_for` uses: point the WS auth path's own session
    factory straight at the test engine instead of patching the module-level
    `app.database.engine` (which the real `async_session_maker()` reads —
    but the StaticPool connection it wraps was created on pytest-asyncio's
    loop, not the TestClient portal's, for every test in this file)."""
    monkeypatch.setattr(
        auth_mod, "_ws_session", lambda: AsyncSession(test_engine, expire_on_commit=False)
    )


@pytest.fixture(autouse=True)
def _allow_query_token_auth(monkeypatch):
    monkeypatch.setattr(app.config.settings, "allow_query_token_auth", True)


@pytest.fixture
def real_run_target_watcher():
    """Original module functions, saved so each test restores them even if
    it swaps in a fake (module-level patch, not `unittest.mock.patch`, since
    the app object itself has no per-instance router state to patch onto)."""
    orig_watcher = bl.run_target_watcher
    orig_list = bl._list_page_targets
    orig_browser_ws = bl._watch_targets_browser_ws
    yield
    bl.run_target_watcher = orig_watcher
    bl._list_page_targets = orig_list
    bl._watch_targets_browser_ws = orig_browser_ws


def test_follow_switches_stream_and_sends_attached(real_run_target_watcher):
    """(e) active change with follow=1 → old screencast stopped, new one
    started, 'attached' sent for the new target."""
    world = FakeCDPWorld([_page("a"), _page("b")])
    events = EventBus()
    app = _make_app(world, events)

    with patch("websockets.connect", world.connect):
        with TestClient(app, raise_server_exceptions=True) as client:
            token = _seed_user_and_token(client, "follow-switch@mc.local")
            with _ws(client, f"/api/v1/browser-live/ws?token={token}&follow=1") as ws:
                first = _recv_until(ws, lambda m: m["type"] == "attached")
                assert first["target"]["id"] == "a"  # watcher's active_id() picks the newest (a)

                # "b" becomes active via a navigation (url change on an
                # existing tab) — the only thing that counts as activity.
                world.set_pages([_page("a"), _page("b", url="https://b.example/new")])
                events.push("targetInfoChanged", {"targetInfo": {
                    "targetId": "b", "type": "page", "title": "b", "url": "https://b.example/new",
                }})

                switched = _recv_until(ws, lambda m: m["type"] == "attached" and m["target"]["id"] == "b")
                assert switched["target"]["id"] == "b"
                assert any(m["method"] == "Page.stopScreencast" for m in world.conns["a"].sent)
                assert any(m["method"] == "Page.startScreencast" for m in world.conns["b"].sent)


def test_follow_off_does_not_switch(real_run_target_watcher):
    """(e) with follow=0 a tab becoming active must NOT move the stream."""
    world = FakeCDPWorld([_page("a"), _page("b")])
    events = EventBus()
    app = _make_app(world, events)

    with patch("websockets.connect", world.connect):
        with TestClient(app, raise_server_exceptions=True) as client:
            token = _seed_user_and_token(client, "follow-off@mc.local")
            with _ws(client, f"/api/v1/browser-live/ws?token={token}&follow=0&target=a") as ws:
                first = _recv_until(ws, lambda m: m["type"] == "attached")
                assert first["target"]["id"] == "a"

                world.set_pages([_page("a"), _page("b", url="https://b.example/new")])
                events.push("targetInfoChanged", {"targetInfo": {
                    "targetId": "b", "type": "page", "title": "b", "url": "https://b.example/new",
                }})

                # A `targets` push must arrive with the new activeId (the
                # picker still needs fresh data), but no `attached` for "b".
                msg = _recv_until(ws, lambda m: m["type"] == "targets" and m["activeId"] == "b")
                assert msg["followedId"] == "a"
                assert "b" not in world.conns  # never reconnected


def test_closing_the_shown_tab_switches_instead_of_ending_stream(real_run_target_watcher):
    """HIGH finding: closing the tab being shown must switch to active_id,
    not kill the WebSocket ('Stream ended' / manual Reconnect)."""
    world = FakeCDPWorld([_page("a"), _page("b")])
    events = EventBus()
    app = _make_app(world, events)

    with patch("websockets.connect", world.connect):
        with TestClient(app, raise_server_exceptions=True) as client:
            token = _seed_user_and_token(client, "closed-tab@mc.local")
            with _ws(client, f"/api/v1/browser-live/ws?token={token}&follow=1") as ws:
                first = _recv_until(ws, lambda m: m["type"] == "attached")
                assert first["target"]["id"] == "a"

                # Agent (Playwright) closes the shown tab.
                world.set_pages([_page("b")])
                world.conns["a"].sever()
                events.push("targetDestroyed", {"targetId": "a"})

                switched = _recv_until(ws, lambda m: m["type"] == "attached" and m["target"]["id"] == "b")
                assert switched["target"]["id"] == "b"
                # The socket is still open — prove it by getting one more frame.
                world.conns["b"].push_frame()
                frame = _recv_until(ws, lambda m: m["type"] == "frame")
                assert frame["data"] == "ZmFrZQ=="


def test_turning_follow_back_on_jumps_to_active_immediately(real_run_target_watcher):
    """MEDIUM finding: {"follow": true} must jump to active_id right away,
    not wait for an unrelated later tab event."""
    world = FakeCDPWorld([_page("a"), _page("b")])
    events = EventBus()
    app = _make_app(world, events)

    with patch("websockets.connect", world.connect):
        with TestClient(app, raise_server_exceptions=True) as client:
            token = _seed_user_and_token(client, "follow-resume@mc.local")
            with _ws(client, f"/api/v1/browser-live/ws?token={token}&follow=0&target=a") as ws:
                _recv_until(ws, lambda m: m["type"] == "attached" and m["target"]["id"] == "a")

                # "b" becomes active while follow is off.
                events.push("targetInfoChanged", {"targetInfo": {
                    "targetId": "b", "type": "page", "title": "b", "url": "https://b.example/now-active",
                }})
                _recv_until(ws, lambda m: m["type"] == "targets" and m["activeId"] == "b")

                ws.send_json({"follow": True})

                switched = _recv_until(ws, lambda m: m["type"] == "attached" and m["target"]["id"] == "b")
                assert switched["target"]["id"] == "b"


def test_background_tab_change_does_not_reattach_shown_stream(real_run_target_watcher):
    """MEDIUM finding: a tab event elsewhere must not tear down/reconnect
    the screencast for the tab that stays shown (no 'Connecting…' flash)."""
    world = FakeCDPWorld([_page("a"), _page("b")])
    events = EventBus()
    app = _make_app(world, events)

    with patch("websockets.connect", world.connect):
        with TestClient(app, raise_server_exceptions=True) as client:
            token = _seed_user_and_token(client, "background-tab@mc.local")
            with _ws(client, f"/api/v1/browser-live/ws?token={token}&follow=0&target=a") as ws:
                _recv_until(ws, lambda m: m["type"] == "attached" and m["target"]["id"] == "a")

                # A brand-new background tab appears — "a" must keep streaming
                # without a second "attached" (which the UI reads as a
                # reconnect and briefly blanks the view).
                world.set_pages([_page("a"), _page("b"), _page("c")])
                events.push("targetCreated", {"targetInfo": {
                    "targetId": "c", "type": "page", "title": "c", "url": "https://c.example",
                }})

                msg = _recv_until(ws, lambda m: m["type"] == "targets" and len(m["targets"]) == 3)
                assert {t["id"] for t in msg["targets"]} == {"a", "b", "c"}
                assert len(world.conns["a"].sent) == 1  # only the original startScreencast
                assert world.connect_count("a") == 1  # never reconnected — proven below, not just inferred
                assert "c" not in world.conns

                # Drain past the targets push and prove the ORIGINAL "a"
                # connection is still what's streaming: push a frame on it
                # and confirm no second "attached" for "a" ever arrives.
                # Sabotage (re-adding `frame_task.cancel()` on the
                # switch_wait branch) makes this fail: the reconnect to "a"
                # replaces `world.conns["a"]` with a fresh FakePageConn,
                # `connect_count("a")` becomes 2, and a second "attached"
                # for "a" shows up right after the targets push.
                world.conns["a"].push_frame()
                frame = _recv_until(ws, lambda m: m["type"] == "frame")
                assert frame["data"] == "ZmFrZQ=="
                assert world.connect_count("a") == 1


def test_seed_race_first_message_is_never_empty_targets_or_no_page(real_run_target_watcher):
    """HIGH-ish finding: the first message out of a fresh connection must
    never be the hollow `{targets: []}` / `status: no_page` pair that shows
    up every time the watcher's initial `/json/list` seed is still in
    flight when the client connects. Uses the REAL `run_target_watcher` (not
    the instant fake driver) with an artificially slow seed, so the race
    this finding describes is actually exercised."""
    world = FakeCDPWorld([_page("a")])
    app = FastAPI()
    app.include_router(bl.router)

    real_list_page_targets = bl._list_page_targets

    async def _slow_list_page_targets():
        await asyncio.sleep(0.05)  # probe: realistic seed latency
        return await world.list_page_targets()

    async def _broken_browser_ws(watcher, stop_event):
        raise OSError("cdp-browser browser-level socket refused")

    bl._watch_targets_browser_ws = _broken_browser_ws  # type: ignore[assignment]
    bl._list_page_targets = _slow_list_page_targets  # type: ignore[assignment]

    _RealTargetWatcher = bl.TargetWatcher
    bl.TargetWatcher = lambda: _RealTargetWatcher(poll_interval=0.05)  # type: ignore[assignment]

    try:
        with patch("websockets.connect", world.connect):
            with TestClient(app, raise_server_exceptions=True) as client:
                token = _seed_user_and_token(client, "seed-race@mc.local")
                with _ws(client, f"/api/v1/browser-live/ws?token={token}&follow=1") as ws:
                    seen = []
                    for _ in range(10):
                        msg = ws.receive_json()
                        seen.append(msg)
                        if msg["type"] == "attached":
                            break
                    else:
                        raise AssertionError(f"never got 'attached' within 10 messages: {seen}")
                    for msg in seen:
                        if msg["type"] == "targets":
                            assert msg["targets"], f"got an empty targets push before 'attached': {seen}"
                        assert not (
                            msg["type"] == "status" and msg.get("code") == "no_page"
                        ), f"got 'no_page' status before 'attached': {seen}"
    finally:
        bl.TargetWatcher = _RealTargetWatcher  # type: ignore[assignment]
        bl._list_page_targets = real_list_page_targets  # type: ignore[assignment]


def test_last_tab_closing_sends_empty_targets_once_not_repeated_status(real_run_target_watcher):
    """MEDIUM finding: when the last tab closes, the client must get a real
    `targets: []` push (so the picker clears and the frontend drops the
    frozen last frame) and the `no_page` status exactly once — not spammed
    every poll tick."""
    world = FakeCDPWorld([_page("a")])
    events = EventBus()
    app = _make_app(world, events)

    with patch("websockets.connect", world.connect):
        with TestClient(app, raise_server_exceptions=True) as client:
            token = _seed_user_and_token(client, "last-tab-closes@mc.local")
            with _ws(client, f"/api/v1/browser-live/ws?token={token}&follow=1") as ws:
                _recv_until(ws, lambda m: m["type"] == "attached" and m["target"]["id"] == "a")

                world.set_pages([])
                world.conns["a"].sever()
                events.push("targetDestroyed", {"targetId": "a"})

                empty_targets = _recv_until(ws, lambda m: m["type"] == "targets" and m["targets"] == [])
                assert empty_targets["activeId"] is None

                # Collect the next several messages: exactly one `no_page`
                # status, never a repeat, never another empty targets push.
                statuses = []
                targets_pushes = []
                deadline = time.monotonic() + 1.5
                while time.monotonic() < deadline:
                    try:
                        msg = _recv_with_timeout(ws, max(deadline - time.monotonic(), 0.01))
                    except TimeoutError:
                        break
                    if msg["type"] == "status":
                        statuses.append(msg)
                    elif msg["type"] == "targets":
                        targets_pushes.append(msg)
                assert len(statuses) == 1 and statuses[0]["code"] == "no_page"
                assert targets_pushes == []  # no repeat of the already-sent empty push


def test_transient_json_list_error_is_not_fatal(real_run_target_watcher):
    """LOW finding: a `_list_page_targets()` failure from inside
    `attach_and_stream` (cdp-browser restarting, a brief network hiccup)
    must retry, never close the client socket with 'Stream ended'."""
    world = FakeCDPWorld([_page("a")])
    events = EventBus()
    app = _make_app(world, events)

    calls = {"n": 0}
    real_list = world.list_page_targets

    async def _flaky_list_page_targets():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("cdp-browser: connection reset")
        return await real_list()

    bl._list_page_targets = _flaky_list_page_targets  # type: ignore[assignment]

    with patch("websockets.connect", world.connect):
        with TestClient(app, raise_server_exceptions=True) as client:
            token = _seed_user_and_token(client, "flaky-list@mc.local")
            with _ws(client, f"/api/v1/browser-live/ws?token={token}&follow=1") as ws:
                # Must recover to 'attached' on its own — no client Reconnect,
                # and no fatal 'status: stream_error' in between.
                seen = []
                msg = _recv_until(ws, lambda m: (seen.append(m), m["type"] == "attached")[-1])
                assert msg["type"] == "attached"
                assert not any(m["type"] == "status" and m.get("code") == "stream_error" for m in seen)
                assert calls["n"] >= 2  # the retry actually happened


def test_falls_back_to_polling_when_browser_ws_unavailable(real_run_target_watcher):
    """(d)/finding: browser-level WS failure must not take the stream down —
    `run_target_watcher`'s real fallback path (polling /json/list) keeps the
    `targets` push alive. Uses the real `run_target_watcher`; only the
    browser-level CDP connect and `_list_page_targets` are faked."""
    world = FakeCDPWorld([_page("a")])
    app = FastAPI()
    app.include_router(bl.router)

    async def _broken_browser_ws(watcher, stop_event):
        raise OSError("cdp-browser browser-level socket refused")

    bl._watch_targets_browser_ws = _broken_browser_ws  # type: ignore[assignment]
    bl._list_page_targets = world.list_page_targets  # type: ignore[assignment]

    _RealTargetWatcher = bl.TargetWatcher
    # Fast polling so the fallback path doesn't make this test wait 2s.
    bl.TargetWatcher = lambda: _RealTargetWatcher(poll_interval=0.05)  # type: ignore[assignment]

    try:
        with patch("websockets.connect", world.connect):
            with TestClient(app, raise_server_exceptions=True) as client:
                token = _seed_user_and_token(client, "poll-fallback@mc.local")
                with _ws(client, f"/api/v1/browser-live/ws?token={token}&follow=1") as ws:
                    first = _recv_until(ws, lambda m: m["type"] == "attached")
                    assert first["target"]["id"] == "a"

                    # Second page appears — only reachable via the polling
                    # fallback since the browser-level WS is broken.
                    world.set_pages([_page("a"), _page("b2", url="https://new.example")])
                    msg = _recv_until(
                        ws, lambda m: m["type"] == "targets" and len(m["targets"]) == 2, max_messages=200,
                    )
                    assert {t["id"] for t in msg["targets"]} == {"a", "b2"}
    finally:
        bl.TargetWatcher = _RealTargetWatcher  # type: ignore[assignment]


def test_navigation_in_shown_tab_sends_targets_with_new_url(real_run_target_watcher):
    """MEDIUM finding (round 4): navigating the tab that is being shown must
    still trigger a `targets` push carrying the new url/title — the old
    signature was just (ids, activeId, followedId), so a navigation inside
    the one tab the agent is working in (the common case) changed none of
    those and no `targets` message went out at all. That left the URL row
    under the header and the picker label showing the pre-navigation page
    for as long as the panel stayed open."""
    world = FakeCDPWorld([_page("a")])
    events = EventBus()
    app = _make_app(world, events)

    with patch("websockets.connect", world.connect):
        with TestClient(app, raise_server_exceptions=True) as client:
            token = _seed_user_and_token(client, "nav-same-tab@mc.local")
            with _ws(client, f"/api/v1/browser-live/ws?token={token}&follow=1") as ws:
                _recv_until(ws, lambda m: m["type"] == "attached" and m["target"]["id"] == "a")

                world.set_pages([_page("a", url="https://new.example/")])
                events.push("targetInfoChanged", {"targetInfo": {
                    "targetId": "a", "type": "page", "title": "a", "url": "https://new.example/",
                }})

                msg = _recv_until(
                    ws,
                    lambda m: m["type"] == "targets"
                    and any(t["url"] == "https://new.example/" for t in m["targets"]),
                )
                assert msg["targets"][0]["url"] == "https://new.example/"


def test_handler_closes_cleanly_when_watcher_is_still_running(real_run_target_watcher):
    """Regression guard (finding, round 4 fix-verification): in production
    the real watcher loops forever and so is always still running when the
    handler's `finally` cancels it on a normal panel close/disconnect; this
    fake mirrors that by never returning on its own until cancelled. The
    handler must tear down cleanly either way — no hang, no uncaught
    exception escaping the WebSocket route.

    NOTE: the reviewed fix for this finding (catching `asyncio.CancelledError`
    around `await watcher_task` so it never escapes the `finally` block, per
    the original suggestion) was tried and REJECTED: under this exact
    TestClient/anyio harness it made `await watcher_task` hang forever at
    `TestClient.__exit__` (confirmed with `py-spy`-equivalent thread dumps —
    the event loop never got a `loop.stop()` because the portal's teardown
    kept waiting), for EVERY test in this file, not just this one. Swallowing
    that CancelledError without re-raising appears to leave anyio's asyncio
    backend unable to tell the surrounding cancel scope has actually been
    exited. Catching it is more dangerous than the log noise it was meant to
    prevent (a hung WebSocket teardown vs. a log line), so the handler still
    lets it propagate (original, unchanged behavior) and this test instead
    locks down the thing that actually matters operationally: no hang."""
    world = FakeCDPWorld([_page("a")])
    app = FastAPI()
    app.include_router(bl.router)

    async def _never_ending_browser_ws(watcher, stop_event):
        # Mirrors the real `_watch_targets_browser_ws`: blocks until the
        # connection is torn down, which in this handler only ever happens
        # via task cancellation.
        await asyncio.Event().wait()

    bl._watch_targets_browser_ws = _never_ending_browser_ws  # type: ignore[assignment]
    bl._list_page_targets = world.list_page_targets  # type: ignore[assignment]

    with patch("websockets.connect", world.connect):
        # raise_server_exceptions=True: an uncaught CancelledError escaping
        # the handler's finally block would surface as a test failure here,
        # either directly or by `websocket.close()` never having run.
        with TestClient(app, raise_server_exceptions=True) as client:
            token = _seed_user_and_token(client, "watcher-cancel@mc.local")
            with _ws(client, f"/api/v1/browser-live/ws?token={token}&follow=1") as ws:
                _recv_until(ws, lambda m: m["type"] == "attached")
            # Exiting `_ws(...)` closes the client side; exiting the
            # `TestClient` context manager below tears the ASGI app down,
            # which is where the handler's finally block (and the real
            # watcher-cancellation path) actually runs.
