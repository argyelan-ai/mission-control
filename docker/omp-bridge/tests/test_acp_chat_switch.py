#!/usr/bin/env python3
"""S3 item C (ADR-105) — the omp daemon side of a chat switch.

Rules (plan S3 §3, item C):

* load first: a `resume` MC asks to try (`tryNativeLoad`) loads the pinned
  session; success needs no package and is reported `nativeResume`;
* load fails (or MC says `start`): `session/new`, the first prompt carries
  the package as `context` under its own cap (CONTEXT_MAX_CHARS_HANDOFF,
  not the run context's 8 000), the text is "Continue." or what is
  configured (empty allowed — measurement M2 is open);
* the outcome goes to MC's switch result route, never the pin route;
* F1 busy refused · F6 a refused handoff turn reports `failed` and puts the
  chat back on its old session · F7 switch back = `session/load` of the old
  id, no package · F8 the held queue goes back to MC, nothing dropped ·
  F9 open question cards end `expired` with reason `expired_by_switch`.

Two layers: end to end with real `ACPClient` children on fake_omp_memory.py
(it remembers every session on disk, `session/load` of an unknown id fails
like omp), the real socket server and the real `acp_chat_ctl.py` shim; and
in-process on FakeClient where a turn has to block or a question has to be
asked.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import acp_chat  # noqa: E402
import acp_chat_events  # noqa: E402
import acp_chat_pool  # noqa: E402
import acp_client  # noqa: E402
from test_acp_chat import (  # noqa: E402
    SESSION_RESULT,
    FakeClient,
    assistant_texts,
    entries_of_type,
    read_lines,
)
from test_acp_chat_elicitation import SELECT_PARAMS, ElicitingClient  # noqa: E402

A = "aaaaaaaa-0000-4000-8000-00000000000a"
EV1 = "e0000000-0000-4000-8000-000000000001"
EV2 = "e0000000-0000-4000-8000-000000000002"
TOKEN = "agent-token-for-tests"
PREFIX = "/api/v1/chat-record/agent/me/chats/"


# ── end to end: fake MC over HTTP, fake omp that remembers ───────────────────


class FakeMcServer:
    """MC's agent-token routes the daemon uses: resume plan, pin, and the
    switch result route (S3 item B, #1043)."""

    def __init__(self, known):
        self.known = set(known)
        self.pins: dict[str, str] = {}
        self.reports: list[tuple[str, str, dict]] = []
        self.requests: list[tuple[str, str]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _chat(self):
                outer.requests.append((self.command, self.path))
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    self._answer(401, {"detail": "no token"})
                    return None
                chat_id, _, rest = self.path[len(PREFIX):].partition("/")
                if not self.path.startswith(PREFIX) or chat_id not in outer.known:
                    self._answer(404, {"detail": "Chat not found"})
                    return None
                return chat_id, rest

            def do_GET(self):
                found = self._chat()
                if found is None:
                    return
                chat_id, _ = found
                native = outer.pins.get(chat_id)
                self._answer(200, {
                    "chatId": chat_id, "harness": "omp", "style": "acp", "agentDefault": False,
                    "nativeSessionId": native, "action": "resume" if native else "start",
                    "acp": {"method": "session/load", "sessionId": native} if native
                    else {"method": "session/new"},
                })

            def do_PUT(self):
                found = self._chat()
                if found is None:
                    return
                chat_id, rest = found
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if rest.startswith("switch/"):
                    event_id = rest.split("/")[1]
                    outer.reports.append((chat_id, event_id, body))
                    if body.get("status") == "done":
                        # report_result: the one re-pin of a pinned chat.
                        outer.pins[chat_id] = body["nativeSessionId"]
                    self._answer(200, {"eventId": event_id, **body})
                    return
                native = body["nativeSessionId"]
                if outer.pins.get(chat_id) not in (None, native):
                    self._answer(409, {"detail": {"reason": "already_pinned"}})
                    return
                outer.pins[chat_id] = native
                self._answer(200, {"id": chat_id, "nativeSessionId": native})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def wait_report(self, n=1, timeout=20.0):
        deadline = time.time() + timeout
        while len(self.reports) < n and time.time() < deadline:
            time.sleep(0.02)
        assert len(self.reports) >= n, self.reports
        return self.reports[n - 1]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Daemon:
    def __init__(self, tmp: Path, mc_url: str):
        self.store = tmp / "omp-store"
        self.sessions_dir = tmp / "omp-sessions" / "--workspace--"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        work = tmp / "work"
        work.mkdir(exist_ok=True)
        env = {"FAKE_OMP_STORE": str(self.store), "PATH": "/usr/bin:/bin"}
        command = [sys.executable, str(HERE / "fake_omp_memory.py")]
        self.default = acp_chat.ChatSession(
            client_factory=lambda: acp_client.ACPClient(command=command, cwd=str(work), env=env),
            cwd=str(work), state_dir=self.sessions_dir, sessions_dir=self.sessions_dir,
        )
        self.default.start()
        self.pool = acp_chat_pool.build_pool(
            self.default, mc=acp_chat_pool.McChats(mc_url, token=lambda: TOKEN))
        self.sock = Path(tempfile.mkdtemp(prefix="acpsw")) / "c.sock"
        self.server = acp_chat.ChatSocketServer(self.default, str(self.sock), pool=self.pool)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        deadline = time.time() + 3
        while not self.sock.exists() and time.time() < deadline:
            time.sleep(0.01)

    def ctl(self, op, payload=None):
        args = [sys.executable, str(ROOT / "acp_chat_ctl.py"), op, "--socket", str(self.sock)]
        if payload is not None:
            args[3:3] = ["--json", json.dumps(payload)]
        proc = subprocess.run(args, capture_output=True, text=True, timeout=60)
        return proc.returncode, json.loads(proc.stdout or "{}")

    def chat(self):
        chat = self.pool.wait_open(A, 30)
        assert chat is not None
        return chat

    def say(self, text):
        code, body = self.ctl("prompt", {"text": text, "chatId": A, "mode": "queue",
                                         "origin": "operator"})
        assert code == 0 and body["ok"] is True, body
        chat = self.chat()
        assert chat.wait_idle(20)
        return assistant_texts(read_lines(chat.transcript_path))[-1]

    def switch(self, **fields):
        code, body = self.ctl("switch", {"chatId": A, **fields})
        assert code == 0 and body["ok"] is True, body
        return body

    def told(self, session_id):
        """Everything fake omp's session `session_id` was told, in order."""
        return json.loads((self.store / f"{session_id}.json").read_text())

    def close(self):
        self.server.shutdown()
        self.pool.close()
        self.default.close()


@pytest.fixture
def e2e(tmp_path):
    mc = FakeMcServer(known=(A,))
    d = Daemon(tmp_path, mc.url)
    try:
        yield d, mc
    finally:
        d.close()
        mc.close()


def _wait_idle(chat, timeout=20.0):
    assert chat.wait_idle(timeout)


def test_load_first_native_resume_needs_no_package(e2e):
    d, mc = e2e
    assert d.say("my cat is Tom") == "I know: my cat is Tom"
    pinned = mc.pins[A]

    body = d.switch(switchEventId=EV1, action="resume", nativeSessionId=pinned,
                    tryNativeLoad=True, package="HANDOFF-PACKAGE")
    assert body["switching"] is True and body["handedOver"] == []
    chat_id, event, report = mc.wait_report()
    assert (chat_id, event) == (A, EV1)
    assert report == {"status": "done", "nativeSessionId": pinned, "nativeResume": True}
    # loaded natively: omp was never told the package, the context goes on
    assert d.told(pinned) == ["my cat is Tom"]
    assert d.say("what is my pet?") == "I know: my cat is Tom, what is my pet?"
    # MC learnt nothing through the pin route after the first pin
    assert [r for r in mc.requests if r[0] == "PUT" and r[1].endswith("/native-session")] \
        == [("PUT", f"{PREFIX}{A}/native-session")]


def test_load_failure_opens_a_new_session_with_the_package_above_8000_chars(e2e):
    d, mc = e2e
    d.say("my cat is Tom")
    pinned = mc.pins[A]
    (d.store / f"{pinned}.json").unlink()      # omp can no longer load it
    package = "P" * 20_000                       # > CONTEXT_MAX_CHARS (8 000)

    d.switch(switchEventId=EV1, action="resume", nativeSessionId=pinned,
             tryNativeLoad=True, package=package)
    _, _, report = mc.wait_report()
    assert report["status"] == "done" and report["nativeResume"] is False
    new = report["nativeSessionId"]
    assert new != pinned and mc.pins[A] == new
    _wait_idle(d.chat())
    told = d.told(new)
    # the whole package reached the model, in front of "Continue."
    assert told == [package + "\n\nContinue."]
    # the chat shows MC's "Continue." as an MC event, never the package
    users = [e["message"] for e in entries_of_type(read_lines(d.chat().transcript_path), "message")
             if e["message"]["role"] == "user"]
    assert [u["content"][0]["text"] for u in users] == ["Continue."]
    assert users[0]["attribution"] == "mc" and users[0]["origin"]["kind"] == "handoff"
    # the next message runs in the new session, after the package
    assert d.say("go on").startswith("I know: " + package)


def test_handoff_cap_and_first_text_are_configurable(e2e, monkeypatch):
    d, mc = e2e
    d.say("hello")
    monkeypatch.setenv("OMP_ACP_CONTEXT_MAX_CHARS_HANDOFF", "1000")
    monkeypatch.setenv("OMP_ACP_HANDOFF_TEXT", "")   # M2 open: a package alone
    d.switch(switchEventId=EV1, action="start", package="Q" * 5000)
    _, _, report = mc.wait_report()
    _wait_idle(d.chat())
    assert d.told(report["nativeSessionId"]) == ["Q" * 1000]
    # no bubble for an empty first text
    users = [e for e in entries_of_type(read_lines(d.chat().transcript_path), "message")
             if e["message"]["role"] == "user"]
    assert users == []


def test_switch_back_resumes_the_old_session_without_package(e2e):
    """F7: back to the old pair = `session/load` of the old id."""
    d, mc = e2e
    d.say("my cat is Tom")
    old = mc.pins[A]
    d.switch(switchEventId=EV1, action="start", package="PKG")   # other harness: no load
    _, _, first = mc.wait_report(1)
    new = first["nativeSessionId"]
    assert new != old and d.told(new) == ["PKG\n\nContinue."]
    _wait_idle(d.chat())

    d.switch(switchEventId=EV2, action="resume", nativeSessionId=old, tryNativeLoad=False)
    _, event, back = mc.wait_report(2)
    assert event == EV2
    assert back == {"status": "done", "nativeSessionId": old, "nativeResume": True}
    assert d.told(old) == ["my cat is Tom"]                  # no package went in
    assert d.say("and now?") == "I know: my cat is Tom, and now?"


def test_queue_handed_back_and_replayed_after_the_handoff_turn(e2e):
    """F8 through the shim: what MC got back runs after the handoff turn,
    in order, nothing dropped, no `queue_lost` card."""
    d, mc = e2e
    d.say("hi")
    code, stop = d.ctl("stop_for_switch", {"chatId": A})
    assert code == 0 and stop["handedOver"] == [] and stop["stopped"] is False
    d.switch(switchEventId=EV1, action="start", package="PKG")
    for text in ("one", "two", "three"):       # MC sends the queue again
        code, body = d.ctl("prompt", {"text": text, "chatId": A, "mode": "queue",
                                      "origin": "operator"})
        assert code == 0 and body["ok"] is True, body
    _, _, report = mc.wait_report()
    chat = d.chat()
    deadline = time.time() + 20
    while len(d.told(report["nativeSessionId"])) < 4 and time.time() < deadline:
        time.sleep(0.05)
    _wait_idle(chat)
    assert d.told(report["nativeSessionId"]) == ["PKG\n\nContinue.", "one", "two", "three"]
    errors = entries_of_type(read_lines(chat.transcript_path), "custom_message",
                             acp_chat_events.CHAT_ERROR_CUSTOM_TYPE)
    assert errors == []


def test_agent_default_chat_and_other_runtime_env_are_refused(e2e):
    d, _ = e2e
    code, body = d.ctl("switch", {"switchEventId": EV1, "action": "start"})
    assert code == 2 and body["error"] == "switch_unsupported"
    code, body = d.ctl("switch", {"chatId": A, "switchEventId": EV1, "action": "start",
                                  "runtimeEnv": {"OPENAI_BASE_URL": "http://elsewhere"}})
    assert code == 2 and body["error"] == "runtime_env_unsupported"
    code, body = d.ctl("switch", {"chatId": A, "switchEventId": "nope", "action": "start"})
    assert code == 2 and body["error"] == "bad_switch"


# ── in process: turns that block, questions, MC refusing ────────────────────


class FakeMc:
    def __init__(self, pinned=None, down=False):
        self.pins = {A: pinned} if pinned else {}
        self.reports: list[tuple[str, str, dict]] = []
        self.down = down

    def resume(self, chat_id):
        native = self.pins.get(chat_id)
        return {"chatId": chat_id, "agentDefault": False, "nativeSessionId": native,
                "action": "resume" if native else "start",
                "acp": {"method": "session/load", "sessionId": native} if native
                else {"method": "session/new"}}

    def pin(self, chat_id, native):
        held = self.pins.get(chat_id)
        if held is not None and held != native:
            raise acp_chat_pool.PinConflict("already_pinned")
        self.pins[chat_id] = native
        return {}

    def report(self, chat_id, event_id, body):
        if self.down:
            raise acp_chat_pool.McUnreachable("down")
        self.reports.append((chat_id, event_id, body))
        if body.get("status") == "done":
            self.pins[chat_id] = body["nativeSessionId"]
        return {}


class Pool:
    def __init__(self, tmp: Path, mc: FakeMc, clients: list):
        self.sessions_dir = tmp / "omp-sessions" / "--workspace--"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.default = acp_chat.ChatSession(
            client_factory=lambda: FakeClient(session_result=dict(SESSION_RESULT, sessionId="sid-d")),
            cwd=str(tmp / "work"), state_dir=self.sessions_dir, sessions_dir=self.sessions_dir,
            elicitation=True,
        )
        self.default.start()
        self.clients = list(clients)
        self.made: list = []

        def factory():
            client = self.clients.pop(0)
            self.made.append(client)
            return client

        self.pool = acp_chat_pool.build_pool(self.default, mc=mc, client_factory=factory)
        # further chats ask (elicitation on), like build_session's daemon
        self.pool._factory = self._chat_factory(self.pool._factory)

    @staticmethod
    def _chat_factory(inner):
        def make(chat_id):
            chat = inner(chat_id)
            chat._elicitation_enabled = True
            return chat
        return make

    def send(self, request):
        return acp_chat_pool.dispatch(self.pool, self.default, request)

    def chat(self):
        return self.pool.wait_open(A, 10)

    def close(self):
        self.pool.close()
        self.default.close()


def _client(cls=FakeClient, sid="sid-old", **kw):
    """A child whose turns answer "ok" unless the test scripts them."""
    kw.setdefault("turns", [{"chunks": ["ok"]} for _ in range(10)])
    return cls(session_result=dict(SESSION_RESULT, sessionId=sid), **kw)


def _open(p: Pool, text="hello", **extra):
    answer = p.send({"op": "prompt", "chatId": A, "text": text, "origin": "operator", **extra})
    assert answer["ok"] is True, answer
    chat = p.chat()
    assert chat is not None
    return chat


def _wait_for(cond, timeout=5.0):
    deadline = time.time() + timeout
    while not cond() and time.time() < deadline:
        time.sleep(0.01)
    assert cond()


def test_switch_is_refused_while_a_turn_runs(tmp_path):
    """F1: no hot switch — `busy`, the child and the session stay."""
    mc = FakeMc()
    old = _client(turns=[{"block": True}])
    p = Pool(tmp_path, mc, [old])
    try:
        chat = _open(p)
        _wait_for(lambda: chat.state()["busy"])
        answer = p.send({"op": "switch", "chatId": A, "switchEventId": EV1, "action": "start",
                         "package": "PKG"})
        assert answer == {"ok": False, "error": "busy", "chatId": A}
        assert old.closed is False and chat.state()["sessionId"] == "sid-old"
        assert mc.reports == []
    finally:
        p.close()


def test_stop_for_switch_keeps_the_queue_in_the_daemon(tmp_path):
    """F8: the queue stays in the daemon (nothing in MC's memory, nothing
    lost to a ctl timeout or a backend restart) and runs after the handoff
    turn, in order, on the new session — no `queue_lost`."""
    mc = FakeMc()
    old = _client(turns=[{"block": True}])
    new = _client(sid="sid-new", new_ids=["sid-new"])
    p = Pool(tmp_path, mc, [old, new])
    try:
        chat = _open(p, "first")
        _wait_for(lambda: chat.state()["busy"])
        for text in ("one", "two", "three"):
            assert p.send({"op": "prompt", "chatId": A, "text": text, "mode": "queue",
                           "origin": "operator"})["queued"] is True
        stop = p.send({"op": "stop_for_switch", "chatId": A})
        assert stop["ok"] is True and stop["stopped"] is True and stop["idle"] is True
        assert stop["handedOver"] == [] and stop["held"] == 3
        assert [j["text"] for j in chat.state()["queue"]] == ["one", "two", "three"]
        # nothing of the queue ran on the old pair
        assert [c[2] for c in old.calls if c[0] == "prompt"] == ["first"]

        answer = p.send({"op": "switch", "chatId": A, "switchEventId": EV1, "action": "start",
                         "package": "PKG"})
        assert answer["ok"] is True and answer["handedOver"] == []
        _wait_for(lambda: len([c for c in new.calls if c[0] == "prompt"]) == 4)
        assert chat.wait_idle(5)
        assert [c[2] for c in new.calls if c[0] == "prompt"] == \
            ["PKG\n\nContinue.", "one", "two", "three"]
        codes = [e["data"]["code"] for e in entries_of_type(
            read_lines(chat.transcript_path), "custom_message",
            acp_chat_events.CHAT_ERROR_CUSTOM_TYPE)] if chat.transcript_path else []
        assert "queue_lost" not in codes
    finally:
        p.close()


def test_stop_for_switch_answers_well_inside_the_ctl_timeout(tmp_path):
    """A turn that does not end on cancel: the answer still comes in
    STOP_WAIT_SECONDS (far below the backend's 10 s ctl timeout, docker
    exec start included) with idle=false — and the queue is still held."""
    assert acp_chat_pool.STOP_WAIT_SECONDS <= 3.0
    mc = FakeMc()
    gate = threading.Event()
    old = _client(turns=[{"gate": gate}, {"chunks": ["one done"]}])
    p = Pool(tmp_path, mc, [old])
    try:
        chat = _open(p, "first")
        _wait_for(lambda: chat.state()["busy"])
        p.send({"op": "prompt", "chatId": A, "text": "one", "mode": "queue", "origin": "operator"})
        began = time.time()
        stop = p.send({"op": "stop_for_switch", "chatId": A})
        assert time.time() - began < acp_chat_pool.STOP_WAIT_SECONDS + 1.0
        assert stop["ok"] is True and stop["idle"] is False and stop["handedOver"] == []
        assert [j["text"] for j in chat.state()["queue"]] == ["one"]
        gate.set()
    finally:
        p.close()


def test_held_queue_runs_on_the_old_session_when_no_switch_follows(tmp_path, monkeypatch):
    """MC stopped the turn but refused the switch (box lock, lost race):
    after SWITCH_HOLD_SECONDS the queue runs where it is — never dropped."""
    monkeypatch.setattr(acp_chat_pool, "SWITCH_HOLD_SECONDS", 1.5)
    mc = FakeMc()
    old = _client(turns=[{"block": True}, {"chunks": ["a"]}, {"chunks": ["b"]}])
    p = Pool(tmp_path, mc, [old])
    try:
        chat = _open(p, "first")
        _wait_for(lambda: chat.state()["busy"])
        for text in ("one", "two"):
            p.send({"op": "prompt", "chatId": A, "text": text, "mode": "queue",
                    "origin": "operator"})
        assert p.send({"op": "stop_for_switch", "chatId": A})["idle"] is True
        assert [c[2] for c in old.calls if c[0] == "prompt"] == ["first"]
        _wait_for(lambda: len([c for c in old.calls if c[0] == "prompt"]) == 3)
        assert chat.wait_idle(5)
        assert [c[2] for c in old.calls if c[0] == "prompt"] == ["first", "one", "two"]
    finally:
        p.close()


def test_a_message_between_stop_and_switch_waits_for_the_switch(tmp_path):
    """Race: after stop_for_switch the chat is idle; a message typed before
    MC sends `switch` must not start a turn (it would make the switch
    `busy`). It is held and runs after the handoff turn."""
    mc = FakeMc()
    old = _client(turns=[{"block": True}])
    new = _client(sid="sid-new", new_ids=["sid-new"])
    p = Pool(tmp_path, mc, [old, new])
    try:
        chat = _open(p, "first")
        _wait_for(lambda: chat.state()["busy"])
        assert p.send({"op": "stop_for_switch", "chatId": A})["idle"] is True
        typed = p.send({"op": "prompt", "chatId": A, "text": "typed", "origin": "operator"})
        assert typed["ok"] is True and typed.get("queued") is True
        assert [c[2] for c in old.calls if c[0] == "prompt"] == ["first"]
        assert p.send({"op": "switch", "chatId": A, "switchEventId": EV1, "action": "start",
                       "package": "PKG"})["ok"] is True
        _wait_for(lambda: len([c for c in new.calls if c[0] == "prompt"]) == 2)
        assert chat.wait_idle(5)
        assert [c[2] for c in new.calls if c[0] == "prompt"] == ["PKG\n\nContinue.", "typed"]
    finally:
        p.close()


def test_open_question_ends_expired_by_switch(tmp_path):
    """F9: the card is closed with its reason, omp gets its cancel."""
    mc = FakeMc()
    gate = threading.Event()
    old = _client(ElicitingClient, turns=[{"gate": gate}])
    p = Pool(tmp_path, mc, [old])
    try:
        chat = _open(p, "ask me")
        _wait_for(lambda: chat.state()["busy"])
        old.ask(7, SELECT_PARAMS)
        _wait_for(lambda: chat.state()["elicitations"])
        card_id = chat.state()["elicitations"][0]["id"]
        stop = p.send({"op": "stop_for_switch", "chatId": A})
        gate.set()
        assert stop["ok"] is True
        results = entries_of_type(read_lines(chat.transcript_path), "custom",
                                  acp_chat_events.ELICITATION_RESULT_CUSTOM_TYPE)
        assert [(r["data"]["elicitationId"], r["data"]["status"], r["data"].get("reason"))
                for r in results] == [(card_id, "expired", "expired_by_switch")]
        assert (7, {"action": "cancel"}) in old.replies
        assert chat.state()["elicitations"] == []
    finally:
        p.close()


def test_refused_handoff_turn_reports_failed_and_keeps_the_old_session(tmp_path):
    """F6/F2: the new model refuses the first prompt → `failed` with its
    code, the chat is back on the session MC still pins."""
    mc = FakeMc()
    old = _client()
    new = _client(sid="sid-new", new_ids=["sid-new"],
                  turns=[{"raise": acp_client.ACPError("429 rate limit exceeded")}])
    back = _client()
    p = Pool(tmp_path, mc, [old, new, back])
    try:
        chat = _open(p)
        assert chat.wait_idle(5)
        assert mc.pins[A] == "sid-old"
        assert p.send({"op": "switch", "chatId": A, "switchEventId": EV1, "action": "start",
                       "package": "PKG"})["ok"] is True
        _wait_for(lambda: mc.reports)
        assert chat.wait_idle(5)
        _, event, report = mc.reports[0]
        assert event == EV1 and report["status"] == "failed"
        assert report["error"]["code"] == "provider_error"
        assert mc.pins[A] == "sid-old"                         # nothing re-pinned
        assert chat.state()["sessionId"] == "sid-old"          # the daemon follows MC
        assert ("load_session", "sid-old", chat._cwd) in back.calls
        assert new.closed is True and chat.load_refused is False
        persisted = json.loads(chat.persist_file.read_text())
        assert persisted["sessionId"] == "sid-old"
    finally:
        p.close()


def test_failed_new_session_reports_and_resumes_the_old_one(tmp_path):
    mc = FakeMc()
    old = _client()
    new = _client(sid="sid-new")
    new._new_raises = acp_client.ACPError("boom")
    new.calls.append(("new_session", "x"))   # FakeClient raises from the 2nd call on
    back = _client()
    p = Pool(tmp_path, mc, [old, new, back])
    try:
        chat = _open(p)
        assert chat.wait_idle(5)
        p.send({"op": "switch", "chatId": A, "switchEventId": EV1, "action": "start",
                "package": "PKG"})
        _wait_for(lambda: mc.reports)
        assert chat.wait_idle(5)
        assert mc.reports[0][2]["error"]["code"] == "new_session_failed"
        assert chat.state()["sessionId"] == "sid-old"
    finally:
        p.close()


def test_load_first_failure_falls_back_to_new_session_in_process(tmp_path):
    """The load-first rule on FakeClient: a failed load is no failure while
    MC asked to TRY it — new session + package."""
    mc = FakeMc()
    old = _client()
    new = _client(sid="sid-new", new_ids=["sid-new"],
                  load_raises=acp_client.ACPError("Could not restore model mc-openai/gone"))
    p = Pool(tmp_path, mc, [old, new])
    try:
        chat = _open(p)
        assert chat.wait_idle(5)
        p.send({"op": "switch", "chatId": A, "switchEventId": EV1, "action": "resume",
                "nativeSessionId": "sid-old", "tryNativeLoad": True, "package": "PKG",
                "model": "mc-openai/other"})
        _wait_for(lambda: mc.reports)
        assert chat.wait_idle(5)
        report = mc.reports[0][2]
        assert report == {"status": "done", "nativeSessionId": "sid-new", "nativeResume": False}
        names = [c[0] for c in new.calls]
        assert names.index("load_session") < names.index("new_session")
        assert [c[2] for c in new.calls if c[0] == "prompt"] == ["PKG\n\nContinue."]
        # the pair MC asked for is pinned, not the agent's selector
        assert ("set_config_option", "sid-new", "model", "mc-openai/other") in new.calls
    finally:
        p.close()


def test_a_report_mc_did_not_take_goes_out_before_the_next_message(tmp_path, monkeypatch):
    monkeypatch.setattr(acp_chat_pool, "REPORT_RETRY_DELAYS", (0.0,))
    mc = FakeMc(down=False)
    old = _client()
    reloaded = _client()
    p = Pool(tmp_path, mc, [old, reloaded])
    try:
        chat = _open(p)
        assert chat.wait_idle(5)
        mc.down = True
        p.send({"op": "switch", "chatId": A, "switchEventId": EV1, "action": "resume",
                "nativeSessionId": "sid-old", "tryNativeLoad": True, "package": "PKG"})
        _wait_for(lambda: chat._pending_report is not None)
        assert chat.wait_idle(5)
        mc.down = False
        p.send({"op": "prompt", "chatId": A, "text": "next", "origin": "operator"})
        assert mc.reports and mc.reports[0][1] == EV1
        assert mc.reports[0][2]["nativeResume"] is True
    finally:
        p.close()


def test_handoff_helpers():
    assert acp_chat.handoff_context_cap({}) == acp_chat.CONTEXT_MAX_CHARS_HANDOFF == 160_000
    assert acp_chat.handoff_context_cap({"OMP_ACP_CONTEXT_MAX_CHARS_HANDOFF": "x"}) == 160_000
    assert acp_chat.handoff_context_cap({"OMP_ACP_CONTEXT_MAX_CHARS_HANDOFF": "0"}) == 160_000
    assert acp_chat.handoff_context_of("a" * 9000, {}) == "a" * 9000   # not the 8 000 cap
    assert acp_chat._context_of("a" * 9000) == "a" * 8000              # run context unchanged
    assert acp_chat.handoff_text_of(None, {}) == "Continue."
    assert acp_chat.handoff_text_of(None, {"OMP_ACP_HANDOFF_TEXT": ""}) == ""
    assert acp_chat.handoff_text_of("Go on.", {"OMP_ACP_HANDOFF_TEXT": ""}) == "Go on."
    assert acp_chat._model_text("", "PKG") == "PKG"
    assert acp_chat._model_text("hi", None) == "hi"


class RefusingMc(FakeMc):
    """MC answers the switch report with a 409 (switch_not_pending /
    switch_superseded / pinned_by_other_chat): e.g. the backend's ctl call
    timed out and it wrote `failed` while the daemon went on."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.refuse = True

    def report(self, chat_id, event_id, body):
        if self.down:
            raise acp_chat_pool.McUnreachable("down")
        self.reports.append((chat_id, event_id, body))
        if self.refuse:
            raise acp_chat_pool.PinConflict("switch_not_pending")
        if body.get("status") == "done":
            self.pins[chat_id] = body["nativeSessionId"]
        return {}


def _prompts(client):
    return [(c[1], c[2]) for c in client.calls if c[0] == "prompt"]


def test_refused_done_report_puts_the_chat_back_on_the_session_mc_pins(tmp_path):
    """MC refused `done`: the daemon must not keep running a session MC
    does not route to this chat — back to the pinned one, pointer too."""
    mc = RefusingMc()
    old = _client()
    new = _client(sid="sid-new", new_ids=["sid-new"])
    back = _client()
    p = Pool(tmp_path, mc, [old, new, back])
    try:
        chat = _open(p)
        assert chat.wait_idle(5)
        mc.pins[A] = "sid-old"
        assert p.send({"op": "switch", "chatId": A, "switchEventId": EV1, "action": "start",
                       "package": "PKG"})["ok"] is True
        _wait_for(lambda: mc.reports)
        assert chat.wait_idle(5)
        assert mc.reports[0][2]["status"] == "done"
        assert chat.state()["sessionId"] == "sid-old" == mc.pins[A]
        assert json.loads(chat.persist_file.read_text())["sessionId"] == "sid-old"
        assert new.closed is True
        assert ("load_session", "sid-old", chat._cwd) in back.calls
        p.send({"op": "prompt", "chatId": A, "text": "after", "origin": "operator"})
        assert chat.wait_idle(5)
        assert _prompts(back) == [("sid-old", "after")]
        codes = [e["data"]["code"] for e in entries_of_type(
            read_lines(chat.transcript_path), "custom_message",
            acp_chat_events.CHAT_ERROR_CUSTOM_TYPE)]
        assert "switch_refused" in codes
    finally:
        p.close()


def test_pointer_waits_for_mc_and_a_late_refusal_goes_back(tmp_path, monkeypatch):
    """The report did not arrive: the pointer still names the session MC
    pins (a restart resumes that one). MC later refuses it: before the next
    message runs, the chat goes back to the pinned session."""
    monkeypatch.setattr(acp_chat_pool, "REPORT_RETRY_DELAYS", (0.0,))
    mc = RefusingMc(down=True)
    old = _client()
    new = _client(sid="sid-new", new_ids=["sid-new"])
    back = _client()
    p = Pool(tmp_path, mc, [old, new, back])
    try:
        mc.down = False
        mc.refuse = False
        chat = _open(p)
        assert chat.wait_idle(5)
        mc.down, mc.refuse = True, True
        p.send({"op": "switch", "chatId": A, "switchEventId": EV1, "action": "start",
                "package": "PKG"})
        _wait_for(lambda: chat._pending_report is not None)
        assert chat.wait_idle(5)
        assert json.loads(chat.persist_file.read_text())["sessionId"] == "sid-old"
        mc.down = False
        p.send({"op": "prompt", "chatId": A, "text": "next", "origin": "operator"})
        assert chat.wait_idle(5)
        assert chat._pending_report is None
        assert chat.state()["sessionId"] == "sid-old"
        assert _prompts(back) == [("sid-old", "next")]
        assert [c for c in _prompts(new) if c[1] == "next"] == []
    finally:
        p.close()


def test_pointer_is_written_once_mc_takes_a_late_report(tmp_path, monkeypatch):
    monkeypatch.setattr(acp_chat_pool, "REPORT_RETRY_DELAYS", (0.0,))
    mc = FakeMc()
    old = _client()
    new = _client(sid="sid-new", new_ids=["sid-new"])
    p = Pool(tmp_path, mc, [old, new])
    try:
        chat = _open(p)
        assert chat.wait_idle(5)
        mc.down = True
        p.send({"op": "switch", "chatId": A, "switchEventId": EV1, "action": "start",
                "package": "PKG"})
        _wait_for(lambda: chat._pending_report is not None)
        assert chat.wait_idle(5)
        assert json.loads(chat.persist_file.read_text())["sessionId"] == "sid-old"
        mc.down = False
        p.send({"op": "prompt", "chatId": A, "text": "next", "origin": "operator"})
        assert chat.wait_idle(5)
        assert json.loads(chat.persist_file.read_text())["sessionId"] == "sid-new"
        assert _prompts(new)[-1] == ("sid-new", "next")
    finally:
        p.close()


def test_a_model_the_session_refuses_fails_the_switch(tmp_path):
    """Rule F12 on the omp side: the model was not applied → `failed`
    (MC keeps chats.model), never `done` with the requested model."""
    mc = FakeMc()
    old = _client()
    new = _client()                     # loads sid-old, refuses `boom*`
    back = _client()
    p = Pool(tmp_path, mc, [old, new, back])
    try:
        chat = _open(p)
        assert chat.wait_idle(5)
        p.send({"op": "switch", "chatId": A, "switchEventId": EV1, "action": "resume",
                "nativeSessionId": "sid-old", "tryNativeLoad": True,
                "model": "boom/not-offered"})
        _wait_for(lambda: mc.reports)
        assert chat.wait_idle(5)
        report = mc.reports[0][2]
        assert report["status"] == "failed"
        assert report["error"]["code"] == "model_unreachable"
        assert chat.state()["sessionId"] == "sid-old"
        assert chat._model != "boom/not-offered"
    finally:
        p.close()


def test_a_substituted_sibling_model_fails_the_switch(tmp_path):
    """`choose_model` would run a sibling of the same provider: the chat
    would run another model than MC records — `failed`, not `done`."""
    offered = dict(SESSION_RESULT, configOptions=[
        {"id": "model", "category": "model", "currentValue": "mc-openai/x",
         "options": [{"value": "mc-openai/x", "name": "X"}]}])
    mc = FakeMc()
    old = _client()
    new = FakeClient(session_result=dict(offered, sessionId="sid-new"), new_ids=["sid-new"],
                     turns=[{"chunks": ["ok"]}])
    back = _client()
    p = Pool(tmp_path, mc, [old, new, back])
    try:
        chat = _open(p)
        assert chat.wait_idle(5)
        p.send({"op": "switch", "chatId": A, "switchEventId": EV1, "action": "start",
                "package": "PKG", "model": "mc-openai/z"})
        _wait_for(lambda: mc.reports)
        assert chat.wait_idle(5)
        report = mc.reports[0][2]
        assert report["status"] == "failed" and report["error"]["code"] == "model_unreachable"
        assert "mc-openai/x" in report["error"]["detail"]
        assert _prompts(new) == []           # no handoff turn on the wrong model
    finally:
        p.close()


def test_a_handoff_turn_without_text_still_counts_as_done(tmp_path):
    """The model took the package but answered with tools only (no final
    text = `empty_turn` in the chat rules): the switch is done, not rolled
    back."""
    mc = FakeMc()
    old = _client()
    new = _client(sid="sid-new", new_ids=["sid-new"], turns=[{"chunks": []}])
    p = Pool(tmp_path, mc, [old, new])
    try:
        chat = _open(p)
        assert chat.wait_idle(5)
        p.send({"op": "switch", "chatId": A, "switchEventId": EV1, "action": "start",
                "package": "PKG"})
        _wait_for(lambda: mc.reports)
        assert chat.wait_idle(5)
        assert mc.reports[0][2]["status"] == "done"
        assert chat.state()["sessionId"] == "sid-new"
    finally:
        p.close()


def test_a_full_budget_package_crosses_the_shim_via_stdin(e2e):
    """Blocker: Linux caps ONE exec argument at 128 KiB (MAX_ARG_STRLEN).
    A full-budget package (32k tokens ~ 128k chars, umlauts escaped) is
    bigger as JSON — the shim reads it from stdin (`--json -`)."""
    d, mc = e2e
    d.say("hello")
    package = ("Grüße — Änderung in loader.py; " * 4200)[:128_000]
    payload = {"chatId": A, "switchEventId": EV1, "action": "start", "package": package}
    assert len(json.dumps(payload).encode()) > 131_072
    args = [sys.executable, str(ROOT / "acp_chat_ctl.py"), "switch", "--json", "-",
            "--socket", str(d.sock)]
    proc = subprocess.run(args, input=json.dumps(payload), capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    _, _, report = mc.wait_report()
    assert report["status"] == "done"
    _wait_idle(d.chat())
    assert d.told(report["nativeSessionId"]) == [package.strip() + "\n\nContinue."]
