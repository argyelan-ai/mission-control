#!/usr/bin/env python3
"""Incident 21.10.2026 — a chat reset leaves a card in the NEW conversation,
and the daemon keeps a record of every reset for the backend.

The operator's chat got a new, empty session (`new_session` job, `/new` or
`/clear` through the control socket) and nothing in the chat said so: the
history looked gone. The old session was untouched on disk. Nobody could say
who sent it.

Contract (docs/specs/chat-over-acp.md, "A new conversation says so"):

* Every conversation the daemon opens IN PLACE of another one starts with
  ONE `chat_notice` line, code `new_conversation` (frontend:
  chat.notice.new_conversation — "New conversation started · 17:07 · the
  previous one is saved"). `data.previous`/`data.current` are the two session
  ids (the slot the later "show previous" link reads), `data.via` says how:
  `new_session` (the control op — `/new`, `/clear`), `start` (a daemon
  start that could not resume: daemon restart or container recreate),
  `child_restart` (the omp child died and the respawn could not resume).
* The card is the FIRST line of the new conversation and never lands in the
  old one. No reset path is silent: when the previous copy is NOT on disk the
  card still comes, without the promise (`data.kept: false`).
* `data.requestId`: the id MC's backend sent with `new_session`, so the card
  and the activity event name the same request.
* The state mirror keeps the last RESET_HISTORY_MAX resets (`resets`, newest
  last): every reset JOB with its outcome — `done`, `failed` (omp refused),
  `withdrawn` (taken back with queue_clear), `lost` (a restart dropped it) —
  and every reset the daemon did on its own. Each with its own `id`, the
  request id when MC sent one, both session ids, time. It survives a daemon
  restart. The backend turns it into activity events (who asked, or "not MC").
* No reset, no card: a normal chat, a successful resume and a brand-new
  agent stay exactly as before.

Every assertion reads what the backend reads: the JSONL transcript the chat
view tails and the state mirror next to it. The ACP side is the in-memory
fake of test_acp_chat (`FakeClient`), plus one run through the real control
socket and the `acp_chat_ctl.py` shim.

Run:  OPENAI_MODEL=ci-test-model python3 -m pytest tests/test_acp_chat_reset_card.py -q
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import pytest  # noqa: E402

import acp_chat  # noqa: E402
import acp_client  # noqa: E402
from test_acp_chat import (  # noqa: E402
    SESSION_RESULT,
    FakeClient,
    entries_of_type,
    make_session,
    read_lines,
    user_texts,
)
from test_acp_chat_pointer import (  # noqa: E402
    MISSING,
    S,
    client,
    container_session,
    layout,
    mc_copy,
    write_pointer,
)

NEW = "new_conversation"
TEXT = "New conversation started — the previous one is saved."
TEXT_NOT_KEPT = "New conversation started."


def notices(path) -> list[dict]:
    return entries_of_type(read_lines(path), "custom_message", "chat_notice")


def errors(path) -> list[dict]:
    return entries_of_type(read_lines(path), "custom_message", "chat_error")


def new_cards(path) -> list[dict]:
    return [n for n in notices(path) if (n.get("data") or {}).get("code") == NEW]


def body(path) -> list[dict]:
    """Every line after the session header."""
    return read_lines(path)[1:]


def mirror(sess) -> dict:
    return json.loads(sess.state_file.read_text(encoding="utf-8"))


def resets(sess) -> list[dict]:
    return mirror(sess)["resets"]


def outline(rows) -> list[tuple]:
    """(outcome, via, previous, current, requestId) per history row."""
    return [(r.get("outcome"), r.get("via"), r.get("previousSessionId"), r.get("sessionId"),
             r.get("requestId")) for r in rows]


def _wait_busy(sess, timeout=3.0):
    deadline = time.time() + timeout
    while not sess.state()["busy"] and time.time() < deadline:
        time.sleep(0.01)
    assert sess.state()["busy"], "the first turn never started"


# ── 1. /new and /clear: the new_session op ──────────────────────────────────


def test_new_session_starts_the_new_conversation_with_the_card(tmp_path):
    c = FakeClient(session_result=SESSION_RESULT, new_ids=["sid-1", "sid-2"],
                   turns=[{"chunks": ["before"]}, {"chunks": ["after"]}])
    sess = make_session(tmp_path, c)
    sess.start()
    try:
        sess.prompt("old context")
        assert sess.wait_idle(timeout=5)
        old = sess.transcript_path

        assert sess.new_session(request_id="req-1")["ok"] is True
        assert sess.wait_idle(timeout=5)
        sess.prompt("fresh start")
        assert sess.wait_idle(timeout=5)
    finally:
        sess.close()

    new = sess.transcript_path
    assert new != old
    # The card is the first thing in the new conversation ...
    first = body(new)[0]
    assert first["type"] == "custom_message" and first["customType"] == "chat_notice"
    assert first["data"] == {"code": NEW, "previous": "sid-1", "current": "sid-2",
                             "via": "new_session", "kept": True, "requestId": "req-1"}
    assert first["content"] == TEXT
    assert first["display"] is True
    # ... exactly once, before the operator's next message ...
    assert len(new_cards(new)) == 1
    assert user_texts(read_lines(new)) == ["fresh start"]
    # ... and never in the old one, which keeps its history untouched.
    assert new_cards(old) == []
    assert user_texts(read_lines(old)) == ["old context"]


def test_the_history_names_the_reset_and_the_request_that_asked(tmp_path):
    c = FakeClient(session_result=SESSION_RESULT, new_ids=["sid-1", "sid-2"])
    sess = make_session(tmp_path, c)
    sess.start()
    try:
        assert mirror(sess)["resets"] == [], "no reset yet, nothing to name"
        sess.new_session(request_id="req-1")
        assert sess.wait_idle(timeout=5)
        (row,) = resets(sess)
    finally:
        sess.close()

    assert outline([row]) == [("done", "new_session", "sid-1", "sid-2", "req-1")]
    assert row["kept"] is True
    assert row["at"].endswith("Z")
    assert isinstance(row["id"], str) and 8 <= len(row["id"]) <= 32
    assert "reason" not in row


def test_a_reset_without_request_id_is_on_record_as_such(tmp_path):
    """`acp_chat_ctl.py new_session` from a shell, or the agent's own tools:
    no request id — the backend records it as "not sent by MC"."""
    c = FakeClient(session_result=SESSION_RESULT, new_ids=["sid-1", "sid-2"])
    sess = make_session(tmp_path, c)
    sess.start()
    try:
        sess.new_session()
        assert sess.wait_idle(timeout=5)
    finally:
        sess.close()

    assert outline(resets(sess)) == [("done", "new_session", "sid-1", "sid-2", None)]
    assert "requestId" not in resets(sess)[0]
    assert "requestId" not in new_cards(sess.transcript_path)[0]["data"]


@pytest.mark.parametrize("bad", ["x" * 65, "a b", "../etc", "", 42, {"id": 1}])
def test_a_request_id_of_the_wrong_shape_is_never_stored(tmp_path, bad):
    """Anyone who reaches the socket picks the field — only a short token
    is kept (it lands in the mirror and the card)."""
    c = FakeClient(session_result=SESSION_RESULT, new_ids=["sid-1", "sid-2"])
    sess = make_session(tmp_path, c)
    sess.start()
    try:
        assert sess.new_session(request_id=bad)["ok"] is True
        assert sess.wait_idle(timeout=5)
    finally:
        sess.close()
    assert outline(resets(sess)) == [("done", "new_session", "sid-1", "sid-2", None)]


def test_a_new_session_queued_behind_a_turn_gets_the_card_when_it_runs(tmp_path):
    gate = threading.Event()
    c = FakeClient(session_result=SESSION_RESULT, new_ids=["sid-1", "sid-2"],
                   turns=[{"chunks": ["one"], "gate": gate}, {"chunks": ["two"]}])
    sess = make_session(tmp_path, c)
    sess.start()
    try:
        sess.prompt("first", mode="queue")
        _wait_busy(sess)
        answer = sess.new_session(mode="queue", request_id="req-q")
        assert answer["queued"] is True
        # The held job names its request in the mirror's queue.
        assert [(j["kind"], j.get("requestId")) for j in mirror(sess)["queue"]] \
            == [("new_session", "req-q")]
        sess.prompt("after the reset", mode="queue")
        first_transcript = sess.transcript_path
        gate.set()
        assert sess.wait_idle(timeout=5)
    finally:
        sess.close()

    assert new_cards(first_transcript) == []
    cards = new_cards(sess.transcript_path)
    assert [(n["data"]["previous"], n["data"]["current"], n["data"]["requestId"])
            for n in cards] == [("sid-1", "sid-2", "req-q")]
    assert body(sess.transcript_path)[0]["data"]["code"] == NEW
    assert outline(resets(sess)) == [("done", "new_session", "sid-1", "sid-2", "req-q")]


def test_two_queued_resets_each_name_the_session_they_replaced(tmp_path):
    """Critic finding: two `/new` behind a running reply. The second one
    replaces the conversation the FIRST one opened — not the one that was
    open when it was typed."""
    gate = threading.Event()
    c = FakeClient(session_result=SESSION_RESULT, new_ids=["sid-1", "sid-2", "sid-3"],
                   turns=[{"chunks": ["one"], "gate": gate}])
    sess = make_session(tmp_path, c)
    sess.start()
    try:
        sess.prompt("first", mode="queue")
        _wait_busy(sess)
        sess.new_session(mode="queue", request_id="req-a")
        sess.new_session(mode="queue", request_id="req-b")
        gate.set()
        assert sess.wait_idle(timeout=5)
    finally:
        sess.close()

    assert outline(resets(sess)) == [
        ("done", "new_session", "sid-1", "sid-2", "req-a"),
        ("done", "new_session", "sid-2", "sid-3", "req-b"),
    ]


def test_two_resets_in_a_row_each_start_with_their_own_card(tmp_path):
    c = FakeClient(session_result=SESSION_RESULT, new_ids=["sid-1", "sid-2", "sid-3"])
    sess = make_session(tmp_path, c)
    sess.start()
    try:
        sess.new_session()
        assert sess.wait_idle(timeout=5)
        second = sess.transcript_path
        sess.new_session()
        assert sess.wait_idle(timeout=5)
    finally:
        sess.close()

    assert [n["data"]["previous"] for n in new_cards(second)] == ["sid-1"]
    assert [n["data"]["previous"] for n in new_cards(sess.transcript_path)] == ["sid-2"]


def test_a_reset_taken_back_before_it_ran_is_on_record(tmp_path):
    """Critic finding: queue_clear takes a held `/new` back — the record says
    it never ran (the backend's event must not keep saying "runs when the
    reply ends")."""
    gate = threading.Event()
    c = FakeClient(session_result=SESSION_RESULT, new_ids=["sid-1", "sid-2"],
                   turns=[{"chunks": ["one"], "gate": gate}])
    sess = make_session(tmp_path, c)
    sess.start()
    try:
        sess.prompt("first", mode="queue")
        _wait_busy(sess)
        sess.new_session(mode="queue", request_id="req-w")
        dropped = sess.queue_clear()["dropped"]
        assert [(j["kind"], j.get("requestId")) for j in dropped] == [("new_session", "req-w")]
        gate.set()
        assert sess.wait_idle(timeout=5)
        assert sess.state()["sessionId"] == "sid-1"
    finally:
        sess.close()

    assert outline(resets(sess)) == [("withdrawn", "new_session", "sid-1", None, "req-w")]
    assert new_cards(sess.transcript_path) == []


def test_a_reset_a_restart_dropped_is_on_record(tmp_path):
    """A daemon restart drops the held `/new` (queue_lost card) — and the one
    that was running: both requests are on record as lost."""
    gate = threading.Event()
    c1 = FakeClient(session_result=SESSION_RESULT, new_ids=["sid-1"],
                    turns=[{"chunks": ["one"], "gate": gate}])
    sess = make_session(tmp_path, c1)
    sess.start()
    sess.prompt("first", mode="queue")
    _wait_busy(sess)
    sess.new_session(mode="queue", request_id="req-held")
    sess.close()  # the restart: the mirror still holds the queue
    gate.set()

    c2 = FakeClient(session_result=dict(SESSION_RESULT, sessionId="sid-1"))
    again = make_session(tmp_path, c2)
    again.start()
    try:
        assert outline(resets(again)) == [("lost", "new_session", "sid-1", None, "req-held")]
        assert [e["data"]["code"] for e in errors(again.transcript_path)][-1] == "queue_lost"
    finally:
        again.close()


def test_a_reset_that_was_running_when_the_daemon_stopped_is_lost(tmp_path):
    """The mirror's `current` names a `new_session` job (the daemon stopped
    while session/new ran)."""
    _, folder = layout(tmp_path)
    copy = mc_copy(folder, S)
    write_pointer(folder, S, copy)
    (folder / "acp-chat-state.json").write_text(json.dumps({
        "version": 1, "sessionId": S, "busy": True,
        "current": {"kind": "new_session", "turn": None, "text": None,
                    "requestId": "req-run", "startedAt": "2026-10-21T17:07:40Z"},
        "queue": [],
    }), encoding="utf-8")
    c = client(session_result=dict(SESSION_RESULT, sessionId=S))
    sess = container_session(folder, c)
    try:
        sess.start()
        assert outline(resets(sess)) == [("lost", "new_session", S, None, "req-run")]
    finally:
        sess.close()


def test_a_refused_new_session_keeps_the_session_and_is_on_record_as_failed(tmp_path):
    c = FakeClient(session_result=SESSION_RESULT,
                   new_raises=acp_client.ACPError("session/new failed: boom"))
    sess = make_session(tmp_path, c)
    sess.start()
    try:
        sess.new_session(request_id="req-f")
        assert sess.wait_idle(timeout=5)
        assert sess.state()["sessionId"] == "sid-1"
    finally:
        sess.close()

    assert new_cards(sess.transcript_path) == []
    assert [e["data"]["code"] for e in errors(sess.transcript_path)] == ["new_session_failed"]
    assert outline(resets(sess)) == [("failed", "new_session", "sid-1", None, "req-f")]


def test_new_without_the_old_copy_still_starts_with_a_card(tmp_path):
    """Critic finding: MC's copy of the old conversation is gone. The card
    comes anyway — without the promise that it is saved. A silent reset is
    exactly the 21.10. symptom."""
    c = FakeClient(session_result=SESSION_RESULT, new_ids=["sid-1", "sid-2"])
    sess = make_session(tmp_path, c)
    sess.start()
    try:
        sess.transcript_path.unlink()
        sess.new_session(request_id="req-1")
        assert sess.wait_idle(timeout=5)
    finally:
        sess.close()

    first = body(sess.transcript_path)[0]
    assert first["data"] == {"code": NEW, "previous": "sid-1", "current": "sid-2",
                             "via": "new_session", "kept": False, "requestId": "req-1"}
    assert first["content"] == TEXT_NOT_KEPT
    assert resets(sess)[0]["kept"] is False


def test_the_history_survives_a_daemon_restart(tmp_path):
    """A restart rewrites the mirror; the resets it names stay readable (the
    backend may look a moment after the daemon came back)."""
    c1 = FakeClient(session_result=SESSION_RESULT, new_ids=["sid-1", "sid-2"])
    sess = make_session(tmp_path, c1)
    sess.start()
    sess.new_session(request_id="req-1")
    assert sess.wait_idle(timeout=5)
    before = resets(sess)
    sess.close()

    c2 = FakeClient(session_result=dict(SESSION_RESULT, sessionId="sid-2"))
    again = make_session(tmp_path, c2)
    again.start()
    try:
        assert ("load_session", "sid-2", str(tmp_path / "work")) in c2.calls
        assert resets(again) == before
        # The resume itself is no reset: no second card.
        assert len(new_cards(again.transcript_path)) == 1
    finally:
        again.close()


def test_the_history_keeps_only_the_newest_resets(tmp_path):
    n = acp_chat.RESET_HISTORY_MAX + 3
    c = FakeClient(session_result=SESSION_RESULT, new_ids=[f"sid-{i}" for i in range(1, n + 2)])
    sess = make_session(tmp_path, c)
    sess.start()
    try:
        for i in range(n):
            sess.new_session(request_id=f"req-{i}")
            assert sess.wait_idle(timeout=5)
    finally:
        sess.close()
    rows = resets(sess)
    assert len(rows) == acp_chat.RESET_HISTORY_MAX
    assert rows[-1]["requestId"] == f"req-{n - 1}"
    assert rows[0]["requestId"] == f"req-{n - acp_chat.RESET_HISTORY_MAX}"


def test_a_garbled_history_in_the_mirror_is_dropped_not_carried(tmp_path):
    """The mirror sits on the agent's own mount. What a previous daemon left
    there is carried over only in the shape this daemon writes."""
    _, folder = layout(tmp_path)
    copy = mc_copy(folder, S)
    write_pointer(folder, S, copy)
    (folder / "acp-chat-state.json").write_text(json.dumps({
        "version": 1, "sessionId": S, "busy": False,
        "resets": [
            {"id": "a1", "at": "2026-10-21T17:07:46Z", "via": "new_session",
             "outcome": "done", "previousSessionId": "p", "sessionId": "q"},
            {"id": "x" * 500, "via": "new_session", "outcome": "done"},
            {"id": "a2", "via": "evil", "outcome": "done"},
            "not a row",
            {"id": "a3", "at": "2026-10-21T17:08:00Z", "via": "new_session",
             "outcome": "done", "sessionId": "y" * 5000},
        ],
    }), encoding="utf-8")
    c = client(session_result=dict(SESSION_RESULT, sessionId=S))
    sess = container_session(folder, c)
    try:
        sess.start()
        assert [r["id"] for r in resets(sess)] == ["a1"]
    finally:
        sess.close()


def test_the_card_through_the_control_socket_and_the_ctl_shim(tmp_path):
    """The way the backend reaches the daemon: `acp_chat_ctl.py new_session`
    over the unix socket, with its request id."""
    c = FakeClient(session_result=SESSION_RESULT, new_ids=["sid-1", "sid-2"])
    sess = make_session(tmp_path, c)
    sess.start()
    sock = Path(tempfile.mkdtemp(prefix="acpreset")) / "c.sock"
    server = acp_chat.ChatSocketServer(sess, str(sock))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    deadline = time.time() + 3
    while not sock.exists() and time.time() < deadline:
        time.sleep(0.01)

    def ctl(*args):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "acp_chat_ctl.py"), *args, "--socket", str(sock)],
            capture_output=True, text=True, timeout=30,
        )
        return proc.returncode, json.loads(proc.stdout or "{}")

    try:
        code, answer = ctl("new_session", "--json",
                           json.dumps({"mode": "queue", "requestId": "req-ctl"}))
        assert code == 0 and answer == {"ok": True, "previousSessionId": "sid-1"}
        assert sess.wait_idle(timeout=5)
        code, state = ctl("state")
        assert code == 0
        assert outline(state["resets"]) == [("done", "new_session", "sid-1", "sid-2", "req-ctl")]
    finally:
        server.shutdown()
        sess.close()

    assert [n["data"]["current"] for n in new_cards(sess.transcript_path)] == ["sid-2"]


def test_the_backend_keeps_every_row_and_card_the_daemon_writes(tmp_path):
    """Two sides check the same shapes: the daemon what it keeps, the backend
    (`chat_reset_audit`) what it trusts from the agent's mount. A row the
    daemon writes must never be dropped by the backend, and the card must
    reach the frontend with `kept` (`omp_chat` parser)."""
    sys.path.insert(0, str(ROOT.parent.parent / "backend"))
    from app.services.chat_reset_audit import reset_rows
    from app.services.omp_chat import OmpLineParser

    gate = threading.Event()
    c = FakeClient(session_result=SESSION_RESULT, new_ids=["sid-1", "sid-2", "sid-3"],
                   turns=[{"chunks": ["one"], "gate": gate}])
    sess = make_session(tmp_path, c)
    sess.start()
    try:
        sess.prompt("first", mode="queue")
        _wait_busy(sess)
        sess.new_session(mode="queue", request_id="0123456789abcdef0123456789abcdef")
        sess.queue_clear()
        gate.set()
        assert sess.wait_idle(timeout=5)
        sess.transcript_path.unlink()
        sess.new_session()
        assert sess.wait_idle(timeout=5)
        state = mirror(sess)
    finally:
        sess.close()

    rows = state["resets"]
    assert [r["outcome"] for r in rows] == ["withdrawn", "done"]
    assert reset_rows(state) == rows

    parser = OmpLineParser()
    events = [ev for line in sess.transcript_path.read_text().splitlines()
              for ev in parser(line)]
    (card,) = [e for e in events if (e.get("notice") or {}).get("code") == NEW]
    assert card["notice"] == {"code": NEW, "previous": "sid-1", "current": "sid-2",
                              "kept": False}


# ── 2. the daemon has to start over on its own ──────────────────────────────


def test_a_start_that_cannot_load_the_conversation_starts_with_the_card(tmp_path):
    """Daemon restart or container recreate: the pointer names S, omp refuses
    the load. The new conversation says it began — and the red card says
    why."""
    _, folder = layout(tmp_path)
    old_copy = mc_copy(folder, S)
    write_pointer(folder, S, old_copy)
    c = client(new_ids=["sid-new"],
               load_raises=acp_client.ACPError("session/load failed: load was cancelled"))
    sess = container_session(folder, c)
    try:
        sess.start()
        new = sess.transcript_path
        assert new != old_copy
        first = body(new)[0]
        assert first["customType"] == "chat_notice"
        assert first["data"] == {"code": NEW, "previous": S, "current": "sid-new",
                                 "via": "start", "reason": "load_failed", "kept": True}
        assert [e["data"]["code"] for e in errors(new)] == ["session_reset"]
        assert new_cards(old_copy) == []
        (row,) = resets(sess)
        assert outline([row]) == [("done", "start", S, "sid-new", None)]
        assert row["reason"] == "load_failed"
    finally:
        sess.close()


def test_a_missing_model_at_start_names_the_reason_on_the_card(tmp_path):
    _, folder = layout(tmp_path)
    write_pointer(folder, S, mc_copy(folder, S))
    c = client(new_ids=["sid-new"], load_raises=MISSING)
    sess = container_session(folder, c)
    try:
        sess.start()
        cards = new_cards(sess.transcript_path)
        assert [(n["data"]["via"], n["data"]["reason"]) for n in cards] == [("start", "model_missing")]
    finally:
        sess.close()


def test_a_child_restart_that_cannot_load_starts_with_the_card(tmp_path):
    """The omp child died; the respawn could not resume the live session."""
    _, folder = layout(tmp_path)
    healthy = client(new_ids=["sid-live"])
    broken = client(new_ids=["sid-new"], load_raises=MISSING)
    sess = container_session(folder, healthy, broken)
    try:
        sess.start()
        live = sess.transcript_path
        sess._restart_child()
        new = sess.transcript_path
        assert new != live
        cards = new_cards(new)
        assert [(n["data"]["previous"], n["data"]["current"], n["data"]["via"])
                for n in cards] == [("sid-live", "sid-new", "child_restart")]
        assert body(new)[0]["data"]["code"] == NEW
        assert new_cards(live) == []
        assert outline(resets(sess)) == [("done", "child_restart", "sid-live", "sid-new", None)]
    finally:
        sess.close()


def test_a_copy_that_is_gone_gets_the_card_without_the_promise(tmp_path):
    """"The previous one is saved" would be false: MC's copy is gone. The
    card still says a new conversation began (no reset is silent); the red
    card (`transcript_missing`) says why."""
    _, folder = layout(tmp_path)
    write_pointer(folder, S, folder / f"2026-10-20T03-36-53_{S}.jsonl")
    c = client(new_ids=["sid-new"])
    sess = container_session(folder, c)
    try:
        sess.start()
        first = body(sess.transcript_path)[0]
        assert first["data"] == {"code": NEW, "previous": S, "current": "sid-new",
                                 "via": "start", "reason": "transcript_missing", "kept": False}
        assert first["content"] == TEXT_NOT_KEPT
        assert [(e["data"]["code"], e["data"]["detail"]) for e in errors(sess.transcript_path)] \
            == [("session_reset", "transcript_missing")]
        (row,) = resets(sess)
        assert (row["via"], row["reason"], row["sessionId"], row["kept"]) \
            == ("start", "transcript_missing", "sid-new", False)
    finally:
        sess.close()


# ── 3. no reset, no card ────────────────────────────────────────────────────


def test_a_normal_chat_gets_no_card(tmp_path):
    c = FakeClient(session_result=SESSION_RESULT,
                   turns=[{"chunks": ["one"]}, {"chunks": ["two"]}])
    sess = make_session(tmp_path, c)
    sess.start()
    try:
        sess.prompt("hello")
        assert sess.wait_idle(timeout=5)
        sess.prompt("again")
        assert sess.wait_idle(timeout=5)
    finally:
        sess.close()
    assert notices(sess.transcript_path) == []
    assert mirror(sess)["resets"] == []


def test_a_successful_resume_gets_no_card(tmp_path):
    _, folder = layout(tmp_path)
    copy = mc_copy(folder, S)
    write_pointer(folder, S, copy)
    c = client(session_result=dict(SESSION_RESULT, sessionId=S))
    sess = container_session(folder, c)
    try:
        sess.start()
        assert sess.transcript_path == copy
    finally:
        sess.close()
    assert new_cards(copy) == []
    assert mirror(sess)["resets"] == []


def test_a_brand_new_agent_gets_no_card(tmp_path):
    _, folder = layout(tmp_path)
    c = client(new_ids=["sid-first"])
    sess = container_session(folder, c)
    try:
        sess.start()
    finally:
        sess.close()
    assert notices(sess.transcript_path) == []
    assert mirror(sess)["resets"] == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
