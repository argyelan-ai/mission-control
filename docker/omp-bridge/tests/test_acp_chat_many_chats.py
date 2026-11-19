#!/usr/bin/env python3
"""Chats slice S2 — many chats on one agent's chat daemon (acp_chat_pool).

Contract (acp_chat_pool docstring):

* no `chatId` = the default session, byte-for-byte the old op table — and
  MC is never asked (the backend sends `chatId` only with its record flag on);
* `chatId` = that chat's own session: `session/new` + pin in MC the first
  time, `session/load` of the pinned id after a restart, own pointer and
  state mirror under `.mc/chats/<id>/`, transcript copy in `.mc/` named by
  its own session id (the record's importer routes it by that id);
* MC is the truth about the native session; a chat MC does not know is
  refused; MC down = the chat's own pointer, never a new chat;
* `/new` on a further chat is refused (`chat_pinned`); the agent's default
  chat (`follow_agent`) is the default session;
* at most `max_open` further chats hold a child; the least recently used
  idle one gives way and loads again on its next message.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import acp_chat  # noqa: E402
import acp_chat_pool  # noqa: E402
from test_acp_chat import FakeClient, SESSION_RESULT, read_lines, user_texts  # noqa: E402

A = "aaaaaaaa-0000-4000-8000-000000000001"
B = "bbbbbbbb-0000-4000-8000-000000000002"
C = "cccccccc-0000-4000-8000-000000000003"
DEFAULT_CHAT = "dddddddd-0000-4000-8000-000000000004"


class FakeMc:
    """MC's two agent-token routes, with resume_plan's rules (S2a #1041)."""

    def __init__(self, known=(A, B, C), default_chat=DEFAULT_CHAT, down=False):
        self.known = set(known)
        self.default_chat = default_chat
        self.pins: dict[str, str] = {}
        self.calls: list[tuple] = []
        self.down = down
        self.conflict = False

    def resume(self, chat_id):
        self.calls.append(("resume", chat_id))
        if self.down:
            raise acp_chat_pool.McUnreachable("down")
        if chat_id == self.default_chat:
            return {"chatId": chat_id, "agentDefault": True, "action": "follow_agent"}
        if chat_id not in self.known:
            raise acp_chat_pool.ChatUnknown(chat_id)
        native = self.pins.get(chat_id)
        plan = {"chatId": chat_id, "harness": "omp", "style": "acp", "agentDefault": False,
                "nativeSessionId": native, "action": "resume" if native else "start"}
        plan["acp"] = {"method": "session/load", "sessionId": native} if native else {"method": "session/new"}
        return plan

    def pin(self, chat_id, native):
        self.calls.append(("pin", chat_id, native))
        if self.down:
            raise acp_chat_pool.McUnreachable("down")
        if self.conflict:
            raise acp_chat_pool.PinConflict("pinned_by_other_chat")
        self.pins[chat_id] = native
        return {"id": chat_id, "nativeSessionId": native}


class Harness:
    """A sessions tree, a default session and a pool, all on fakes."""

    def __init__(self, tmp: Path, mc: FakeMc, *, ids=None, max_open=3, turns=None):
        self.tmp = tmp
        self.mc = mc
        self.sessions_dir = tmp / "omp-sessions" / "--workspace--"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.default_client = FakeClient(session_result=dict(SESSION_RESULT, sessionId="sid-default"),
                                         turns=turns)
        self.default = acp_chat.ChatSession(
            client_factory=lambda: self.default_client,
            cwd=str(tmp / "work"), state_dir=self.sessions_dir, sessions_dir=self.sessions_dir,
        )
        self.default.start()
        self.clients: list[FakeClient] = []
        self._ids = list(ids or [])

        def client_factory():
            client = FakeClient(session_result=dict(SESSION_RESULT),
                                new_ids=[self._ids.pop(0)] if self._ids else None)
            self.clients.append(client)
            return client

        self.pool = acp_chat_pool.build_pool(self.default, mc=mc, client_factory=client_factory,
                                             max_open=max_open)

    def send(self, request):
        return acp_chat_pool.dispatch(self.pool, self.default, request)

    def chat(self, chat_id):
        session, _ = self.pool.get(chat_id, start=False)
        return session

    def close(self):
        self.pool.close()
        self.default.close()


def _wait(session):
    assert session.wait_idle(5)


# ── without chatId: exactly today's behaviour ─────────────────────────────


def test_no_chat_id_is_the_default_session_and_never_asks_mc(tmp_path):
    mc = FakeMc()
    h = Harness(tmp_path, mc)
    try:
        answer = h.send({"op": "prompt", "text": "hello"})
        assert answer["ok"] is True and "chatId" not in answer
        _wait(h.default)
        assert ("prompt", "sid-default", "hello") in h.default_client.calls
        assert h.clients == [] and mc.calls == []
        state = h.send({"op": "state"})
        assert state["sessionId"] == "sid-default"
        # the same answer the old op table gives
        assert set(state) == set({"ok": True, **h.default.state()})
        assert h.send({"op": "new_session"})["ok"] is True
    finally:
        h.close()


def test_a_server_without_a_pool_refuses_chat_ids(tmp_path):
    h = Harness(tmp_path, FakeMc())
    try:
        answer = acp_chat_pool.dispatch(None, h.default, {"op": "prompt", "text": "x", "chatId": A})
        assert answer == {"ok": False, "error": "chats_unsupported"}
        assert h.default_client.calls.count(("prompt", "sid-default", "x")) == 0
    finally:
        h.close()


# ── a further chat: its own session, pinned in MC ─────────────────────────


def test_first_message_opens_its_own_session_and_pins_it(tmp_path):
    mc = FakeMc()
    h = Harness(tmp_path, mc, ids=["sid-a"])
    try:
        answer = h.send({"op": "prompt", "text": "for A", "chatId": A})
        assert answer["ok"] is True and answer["chatId"] == A
        chat = h.chat(A)
        _wait(chat)
        client = h.clients[0]
        assert ("new_session", str(tmp_path / "work")) in client.calls
        assert ("prompt", "sid-a", "for A") in client.calls
        assert mc.pins == {A: "sid-a"}
        # its own transcript copy, named by its own session id, in `.mc/`
        assert chat.transcript_path.parent == h.sessions_dir / ".mc"
        assert chat.transcript_path.name.endswith("_sid-a.jsonl")
        assert user_texts(read_lines(chat.transcript_path)) == ["for A"]
        # its own pointer + state mirror, deeper than the backend's pointer search
        own = h.sessions_dir / ".mc" / "chats" / A
        assert json.loads((own / "acp-chat.json").read_text())["sessionId"] == "sid-a"
        assert json.loads((own / "acp-chat-state.json").read_text())["sessionId"] == "sid-a"
        # the agent's default chat did not move
        default_state = json.loads((h.sessions_dir / "acp-chat-state.json").read_text())
        assert default_state["sessionId"] == "sid-default"
        assert ("prompt", "sid-default", "for A") not in h.default_client.calls
    finally:
        h.close()


def test_two_chats_alternate_each_on_its_own_session(tmp_path):
    mc = FakeMc()
    h = Harness(tmp_path, mc, ids=["sid-a", "sid-b"])
    try:
        for text, chat_id in (("a1", A), ("b1", B), ("a2", A), ("b2", B)):
            assert h.send({"op": "prompt", "text": text, "chatId": chat_id})["ok"]
            _wait(h.chat(chat_id))
        a, b = h.chat(A), h.chat(B)
        assert user_texts(read_lines(a.transcript_path)) == ["a1", "a2"]
        assert user_texts(read_lines(b.transcript_path)) == ["b1", "b2"]
        # one child per chat, each prompting only its own session
        assert [c for c in h.clients[0].calls if c[0] == "prompt"] == [
            ("prompt", "sid-a", "a1"), ("prompt", "sid-a", "a2")]
        assert [c for c in h.clients[1].calls if c[0] == "prompt"] == [
            ("prompt", "sid-b", "b1"), ("prompt", "sid-b", "b2")]
        assert mc.pins == {A: "sid-a", B: "sid-b"}
        listed = {c["chatId"]: c["sessionId"] for c in h.send({"op": "chats"})["chats"]}
        assert listed == {A: "sid-a", B: "sid-b"}
    finally:
        h.close()


def test_daemon_restart_loads_each_pinned_session_and_keeps_its_file(tmp_path):
    mc = FakeMc()
    h = Harness(tmp_path, mc, ids=["sid-a", "sid-b"])
    for text, chat_id in (("a1", A), ("b1", B)):
        h.send({"op": "prompt", "text": text, "chatId": chat_id})
        _wait(h.chat(chat_id))
    files = {A: h.chat(A).transcript_path, B: h.chat(B).transcript_path}
    h.close()

    h2 = Harness(tmp_path, mc)  # a new daemon: new pool, new children
    try:
        for text, chat_id in (("b2", B), ("a2", A)):
            assert h2.send({"op": "prompt", "text": text, "chatId": chat_id})["ok"]
            _wait(h2.chat(chat_id))
        loads = [c for client in h2.clients for c in client.calls if c[0] in ("load_session", "new_session")]
        assert loads == [("load_session", "sid-b", str(tmp_path / "work")),
                         ("load_session", "sid-a", str(tmp_path / "work"))]
        assert h2.chat(A).transcript_path == files[A]
        assert user_texts(read_lines(files[A])) == ["a1", "a2"]
        assert user_texts(read_lines(files[B])) == ["b1", "b2"]
        # nothing re-pinned: the id did not change
        assert [c for c in mc.calls if c[0] == "pin"] == [("pin", A, "sid-a"), ("pin", B, "sid-b")]
    finally:
        h2.close()


def test_mc_names_the_session_even_when_the_local_pointer_is_gone(tmp_path):
    mc = FakeMc()
    mc.pins[A] = "sid-a"
    h = Harness(tmp_path, mc)
    try:
        assert h.send({"op": "prompt", "text": "again", "chatId": A})["ok"]
        chat = h.chat(A)
        _wait(chat)
        assert ("load_session", "sid-a", str(tmp_path / "work")) in h.clients[0].calls
        assert not any(c[0] == "new_session" for c in h.clients[0].calls)
        assert chat.transcript_path.name.endswith("_sid-a.jsonl")
    finally:
        h.close()


def test_unknown_chat_is_refused_without_a_child(tmp_path):
    h = Harness(tmp_path, FakeMc(known=()))
    try:
        assert h.send({"op": "prompt", "text": "x", "chatId": A}) == {"ok": False, "error": "chat_unknown"}
        assert h.clients == []
        assert not (h.sessions_dir / ".mc" / "chats" / A / "acp-chat.json").exists()
    finally:
        h.close()


def test_bad_chat_id_never_reaches_a_path(tmp_path):
    mc = FakeMc()
    h = Harness(tmp_path, mc)
    try:
        for bad in ("../x", "", 5, "a" * 36):
            assert h.send({"op": "prompt", "text": "x", "chatId": bad}) == {"ok": False, "error": "bad_chat_id"}
        assert mc.calls == [] and h.clients == []
    finally:
        h.close()


def test_the_agents_default_chat_is_the_default_session(tmp_path):
    h = Harness(tmp_path, FakeMc())
    try:
        assert h.send({"op": "prompt", "text": "main", "chatId": DEFAULT_CHAT})["ok"]
        _wait(h.default)
        assert ("prompt", "sid-default", "main") in h.default_client.calls
        assert h.clients == []
    finally:
        h.close()


def test_new_on_a_further_chat_is_refused_its_session_is_pinned(tmp_path):
    h = Harness(tmp_path, FakeMc(), ids=["sid-a"])
    try:
        h.send({"op": "prompt", "text": "x", "chatId": A})
        _wait(h.chat(A))
        assert h.send({"op": "new_session", "chatId": A}) == {"ok": False, "error": "chat_pinned", "chatId": A}
        assert [c for c in h.clients[0].calls if c[0] == "new_session"] == [("new_session", str(tmp_path / "work"))]
        assert h.send({"op": "new_session", "chatId": B}) == {"ok": False, "error": "chat_pinned"}
    finally:
        h.close()


def test_mc_down_resumes_the_local_pointer_and_pins_later(tmp_path):
    mc = FakeMc()
    h = Harness(tmp_path, mc, ids=["sid-a"])
    mc.down = True
    try:
        # never opened, MC down: refused, not invented
        refused = h.send({"op": "prompt", "text": "x", "chatId": A})
        assert refused["ok"] is False and refused["error"] == "mc_unreachable"
        mc.down = False
        h.send({"op": "prompt", "text": "a1", "chatId": A})
        _wait(h.chat(A))
    finally:
        h.close()
    mc.pins.clear()   # the pin never reached MC (lost on its side)
    mc.down = True
    h2 = Harness(tmp_path, mc)
    try:
        assert h2.send({"op": "prompt", "text": "a2", "chatId": A})["ok"]
        _wait(h2.chat(A))
        assert ("load_session", "sid-a", str(tmp_path / "work")) in h2.clients[0].calls
        mc.down = False
        assert h2.send({"op": "prompt", "text": "a3", "chatId": A})["ok"]
        _wait(h2.chat(A))
        assert mc.pins == {A: "sid-a"}
    finally:
        h2.close()


def test_a_refused_pin_is_a_visible_card(tmp_path):
    mc = FakeMc()
    mc.conflict = True
    h = Harness(tmp_path, mc, ids=["sid-a"])
    try:
        h.send({"op": "prompt", "text": "x", "chatId": A})
        chat = h.chat(A)
        _wait(chat)
        errors = [l for l in read_lines(chat.transcript_path)
                  if "chat_pin_failed" in json.dumps(l)]
        assert errors, "the refused pin must be a card in the chat"
        pins = [c for c in mc.calls if c[0] == "pin"]
        h.send({"op": "prompt", "text": "y", "chatId": A})
        _wait(chat)
        assert [c for c in mc.calls if c[0] == "pin"] == pins  # one card, not one per message
    finally:
        h.close()


def test_state_of_a_chat_that_is_not_open_starts_nothing(tmp_path):
    mc = FakeMc()
    h = Harness(tmp_path, mc)
    try:
        assert h.send({"op": "state", "chatId": A}) == {"ok": True, "chatId": A, "open": False}
        assert h.send({"op": "cancel", "chatId": A}) == {"ok": True}
        assert h.send({"op": "queue_clear", "chatId": A})["ok"] is True
        assert h.clients == [] and mc.calls == []
    finally:
        h.close()


def test_the_least_recently_used_idle_chat_gives_way_and_comes_back(tmp_path):
    mc = FakeMc()
    h = Harness(tmp_path, mc, ids=["sid-a", "sid-b", "sid-c"], max_open=2)
    try:
        for text, chat_id in (("a1", A), ("b1", B), ("c1", C)):
            assert h.send({"op": "prompt", "text": text, "chatId": chat_id})["ok"]
            _wait(h.chat(chat_id))
        assert h.chat(A) is None and h.clients[0].closed  # A was idle the longest
        assert h.send({"op": "prompt", "text": "a2", "chatId": A})["ok"]
        _wait(h.chat(A))
        assert ("load_session", "sid-a", str(tmp_path / "work")) in h.clients[3].calls
        assert user_texts(read_lines(h.chat(A).transcript_path)) == ["a1", "a2"]
    finally:
        h.close()


def test_all_chats_busy_refuses_instead_of_dropping(tmp_path):
    mc = FakeMc()
    gate = threading.Event()
    h = Harness(tmp_path, mc, ids=["sid-a", "sid-b"], max_open=1)
    try:
        h.send({"op": "prompt", "text": "a1", "chatId": A})
        h.clients[0].turns.append({"gate": gate})
        h.send({"op": "prompt", "text": "a2", "chatId": A})
        assert h.send({"op": "prompt", "text": "b1", "chatId": B}) == {"ok": False, "error": "chats_busy"}
        gate.set()
        _wait(h.chat(A))
    finally:
        gate.set()
        h.close()
