#!/usr/bin/env python3
"""S1-R5 — an interrupted reply is a visible card, and nothing is resent.

Before S1-R5 a chat daemon that restarted while a reply was running
(container restart or recreate, a bridge reload) left no trace in the chat:
the operator's message was simply never answered. When the omp child died
mid-turn the chat showed `process_exit`, but without the text — retyping it
was the only way back.

Contract (docs/specs/chat-over-acp.md, "Interrupted turns"):

* While a job runs, the state mirror `acp-chat-state.json` carries
  `current: {kind, turn, text, truncated, startedAt}`; `null` when idle.
  Set when the job is claimed; the next queued job becomes `current`.
  `text` is the whole prompt when it has at most 8000 characters, else
  `null` with `truncated: true` — the button must never send a text the
  operator did not write (Pruefung v2, F3).
* `start()` reads the previous mirror BEFORE anything rewrites it:
  `busy` + a `prompt` -> ONE `chat_error` `turn_interrupted` card with
  `data.retryText` (whole text only); `busy` + `new_session` -> `/new` in
  the existing `queue_lost` card; `busy` without `current` (an image before
  S1-R5) -> the card without `retryText`.
* `process_exit` (the omp child died mid-turn) carries `retryText` too.
* Nothing is ever sent again automatically: no `prompt()` from `start()` or
  from the child restart.
* `ACPEventMapper.map_chat_error(..., data=None)`: extra card fields;
  `code`/`detail` stay; without `data` the line is byte for byte the old one.

Every assertion reads what the backend reads: the state mirror and the
JSONL transcript the chat view tails.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REPO = ROOT.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import acp_chat  # noqa: E402
import acp_chat_events  # noqa: E402
import acp_client  # noqa: E402
from test_acp_chat import (  # noqa: E402
    SESSION_RESULT,
    FakeClient,
    _RestartRaceClient,
    entries_of_type,
    make_session,
    read_lines,
    user_texts,
)

INTERRUPTED_TEXT = (
    "The chat restarted while a reply was running — nothing was resent "
    "automatically."
)
LIMIT = 8000


# ── helpers ─────────────────────────────────────────────────────────────────


def cards(path, code=None) -> list[dict]:
    """The `chat_error` lines of a transcript, optionally of one code."""
    out = entries_of_type(read_lines(path), "custom_message",
                          acp_chat_events.CHAT_ERROR_CUSTOM_TYPE)
    if code is not None:
        out = [c for c in out if (c.get("data") or {}).get("code") == code]
    return out


def mirror(sess: acp_chat.ChatSession) -> dict:
    return json.loads(sess.state_file.read_text(encoding="utf-8"))


def prompts(client) -> list[str]:
    return [c[2] for c in client.calls if c[0] == "prompt"]


def wait_for(predicate, timeout=3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def die_mid_reply(tmp_path: Path, text: str, *, held=()) -> tuple[Path, FakeClient]:
    """A daemon that stops while a reply runs: the turn blocks, the daemon is
    closed (container stop, bridge reload), then the blocked call returns —
    the retired session must leave its mirror as it was. Returns the
    transcript and the dead daemon's client."""
    gate = threading.Event()
    client = FakeClient(session_result=SESSION_RESULT,
                        turns=[{"chunks": ["part of a reply"], "gate": gate}])
    sess = make_session(tmp_path, client)
    sess.start()
    assert sess.prompt(text, mode="queue")["ok"] is True
    assert wait_for(lambda: sess.state()["busy"])
    for item in held:
        assert sess.prompt(item, mode="queue").get("queued") is True
    assert wait_for(lambda: text in user_texts(read_lines(sess.transcript_path)))
    transcript = sess.transcript_path
    sess.close()
    gate.set()
    assert sess.wait_idle(timeout=5)
    return transcript, client


def restart(tmp_path: Path) -> tuple[acp_chat.ChatSession, FakeClient]:
    """The next daemon: a new ChatSession object on the same folders."""
    client = FakeClient(session_result=SESSION_RESULT)
    sess = make_session(tmp_path, client)
    sess.start()
    return sess, client


def seed_mirror(tmp_path: Path, **fields) -> None:
    """A state mirror as a previous daemon left it."""
    sessions = tmp_path / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "acp-chat-state.json").write_text(json.dumps({
        "version": 1, "driver": "omp", "sessionId": "sid-1", "turn": 4,
        "transcript": None, **fields,
    }), encoding="utf-8")


# ── 1. a restart mid-reply: ONE card with the text, nothing resent ─────────


def test_1_restart_mid_reply_is_one_card_with_the_text_and_nothing_resent(tmp_path):
    text = "Count to twenty, one number every two seconds."
    transcript, dead = die_mid_reply(tmp_path, text)

    again, client = restart(tmp_path)
    try:
        time.sleep(0.2)  # a resend would be asynchronous — give it room
        found = cards(transcript, "turn_interrupted")
        assert len(found) == 1, f"{len(found)} turn_interrupted cards"
        card = found[0]
        assert card["content"] == INTERRUPTED_TEXT
        assert card["data"] == {"code": "turn_interrupted", "detail": "turn 1",
                                "retryText": text}
        assert prompts(client) == [], "start() sent the interrupted text again"
        assert prompts(dead) == [text], "the dead daemon's client was prompted again"
        assert user_texts(read_lines(transcript)) == [text], "a second user line appeared"
        assert again.state()["busy"] is False
        assert mirror(again)["current"] is None
    finally:
        again.close()

    third, client3 = restart(tmp_path)  # nothing ran this time -> no second card
    third.close()
    assert len(cards(transcript, "turn_interrupted")) == 1
    assert prompts(client3) == []
    print("PASS test_1_restart_mid_reply_is_one_card_with_the_text_and_nothing_resent")


def test_1b_the_interrupted_turn_comes_before_the_held_messages(tmp_path):
    transcript, _ = die_mid_reply(tmp_path, "the running one", held=("held one",))
    again, client = restart(tmp_path)
    again.close()
    codes = [(c.get("data") or {}).get("code") for c in cards(transcript)]
    assert codes == ["turn_interrupted", "queue_lost"], codes
    lost = cards(transcript, "queue_lost")[0]
    assert "held one" in lost["content"] and "the running one" not in lost["content"]
    assert prompts(client) == []


# ── 2. a quiet previous daemon: no card ─────────────────────────────────────


def test_2a_a_turn_that_ended_leaves_no_card(tmp_path):
    client = FakeClient(session_result=SESSION_RESULT, turns=[{"chunks": ["done"]}])
    sess = make_session(tmp_path, client)
    sess.start()
    sess.prompt("a finished question")
    assert sess.wait_idle(timeout=5)
    transcript = sess.transcript_path
    assert mirror(sess)["busy"] is False and mirror(sess)["current"] is None
    sess.close()

    again, client2 = restart(tmp_path)
    again.close()
    assert cards(transcript, "turn_interrupted") == []
    assert prompts(client2) == []


@pytest.mark.parametrize("fields", [
    {"busy": False},
    {"busy": False, "current": {"kind": "prompt", "turn": 3, "text": "stale",
                                "truncated": False, "startedAt": "x"}},
    {"busy": "true", "current": {"kind": "prompt", "turn": 3, "text": "not a bool"}},
    {},
], ids=["idle", "idle-with-stale-current", "busy-not-a-bool", "no-busy"])
def test_2b_an_idle_or_unclear_mirror_gives_no_card(tmp_path, fields):
    seed_mirror(tmp_path, **fields)
    again, client = restart(tmp_path)
    again.close()
    assert cards(again.transcript_path, "turn_interrupted") == []
    assert prompts(client) == []


# ── 3. an image before S1-R5: busy, but no `current` ────────────────────────


@pytest.mark.parametrize("current", [None, "missing", "not-a-dict"])
def test_3_an_older_daemon_without_current_gives_a_card_without_text(tmp_path, current):
    fields = {"busy": True, "queue": []}
    if current != "missing":
        fields["current"] = current
    seed_mirror(tmp_path, **fields)
    again, client = restart(tmp_path)
    again.close()
    found = cards(again.transcript_path, "turn_interrupted")
    assert len(found) == 1
    assert found[0]["content"] == INTERRUPTED_TEXT
    assert "retryText" not in found[0]["data"], "no text known — no button"
    assert found[0]["data"]["detail"] == "turn unknown"
    assert prompts(client) == []


# ── 4. the omp child dies mid-turn: process_exit carries the text ──────────


class _Proc:
    def __init__(self) -> None:
        self.dead = False

    def poll(self):
        return 1 if self.dead else None


class DyingClient(FakeClient):
    """The omp child exits during the first prompt (stdout closes)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self._proc = _Proc()

    def prompt(self, session_id, text, timeout=600.0):
        self.calls.append(("prompt", session_id, text))
        self._proc.dead = True
        raise acp_client.ACPError("omp acp stdout closed unexpectedly")


def child_dies(tmp_path: Path, text: str):
    dying = DyingClient(session_result=SESSION_RESULT)
    respawned = FakeClient(session_result=SESSION_RESULT)
    made = [dying, respawned]
    sess = acp_chat.ChatSession(
        client_factory=lambda: made.pop(0),
        cwd=str(tmp_path / "work"),
        state_dir=tmp_path / "state",
        sessions_dir=tmp_path / "sessions",
    )
    sess.start()
    sess.prompt(text)
    assert sess.wait_idle(timeout=5)
    time.sleep(0.1)
    return sess, dying, respawned


def test_4_process_exit_carries_the_text_of_the_turn(tmp_path):
    text = "Summarise the open pull requests."
    sess, dying, respawned = child_dies(tmp_path, text)
    try:
        found = cards(sess.transcript_path, "process_exit")
        assert len(found) == 1
        assert found[0]["data"]["retryText"] == text
        assert found[0]["data"]["code"] == "process_exit"
        assert ("load_session", "sid-1", str(tmp_path / "work")) in respawned.calls
        assert prompts(respawned) == [], "the child restart sent the text again"
        assert prompts(dying) == [text]
        assert mirror(sess)["busy"] is False and mirror(sess)["current"] is None
    finally:
        sess.close()


# ── 5. `current` follows the job: null after it, the next queued job next ──


def test_5_current_is_null_after_the_turn_and_the_next_job_becomes_current(tmp_path):
    first_gate, second_gate = threading.Event(), threading.Event()
    client = FakeClient(session_result=SESSION_RESULT, turns=[
        {"chunks": ["one"], "gate": first_gate},
        {"chunks": ["two"], "gate": second_gate},
    ])
    sess = make_session(tmp_path, client)
    sess.start()
    try:
        assert mirror(sess)["current"] is None, "idle after start"
        sess.prompt("first", mode="queue")
        assert wait_for(lambda: sess.state()["busy"])
        cur = mirror(sess)["current"]
        assert {k: cur[k] for k in ("kind", "turn", "text", "truncated")} == {
            "kind": "prompt", "turn": 1, "text": "first", "truncated": False}
        assert isinstance(cur["startedAt"], str) and cur["startedAt"]
        assert sess.prompt("second", mode="queue").get("queued") is True
        assert sess.prompt("third", mode="queue").get("queued") is True
        assert mirror(sess)["current"]["text"] == "first", "a queued job is not current"
        first_gate.set()
        assert wait_for(lambda: (mirror(sess)["current"] or {}).get("text") == "second")
        assert mirror(sess)["current"]["turn"] == 2
        assert [j["text"] for j in mirror(sess)["queue"]] == ["third"]
        second_gate.set()
        assert sess.wait_idle(timeout=5)
        assert mirror(sess)["busy"] is False
        assert mirror(sess)["current"] is None
        assert sess.state()["current"] is None
    finally:
        first_gate.set()
        second_gate.set()
        sess.close()


def test_5b_a_running_new_is_current_and_a_restart_lists_it_as_lost(tmp_path):
    gate = threading.Event()
    client = FakeClient(session_result=SESSION_RESULT, new_ids=["sid-1", "sid-2"],
                        turns=[{"chunks": ["x"], "gate": gate}])
    sess = make_session(tmp_path, client)
    sess.start()
    try:
        sess.prompt("before the reset", mode="queue")
        assert wait_for(lambda: sess.state()["busy"])
        sess.new_session(mode="queue")
        gate.set()
        assert sess.wait_idle(timeout=5)
        assert mirror(sess)["current"] is None
    finally:
        gate.set()
        sess.close()

    # A daemon that stopped while `/new` ran: no turn card, `/new` is lost.
    seed_mirror(tmp_path, busy=True, current={
        "kind": "new_session", "turn": None, "text": "", "truncated": False,
        "startedAt": "x"}, queue=[{"id": "q1", "kind": "prompt", "text": "held one",
                                   "at": "x"}])
    again, client2 = restart(tmp_path)
    again.close()
    assert cards(again.transcript_path, "turn_interrupted") == []
    lost = cards(again.transcript_path, "queue_lost")
    assert len(lost) == 1
    assert lost[0]["content"].splitlines()[1:] == ["- /new", "- held one"]
    assert "2 queued" in lost[0]["content"]
    assert prompts(client2) == []


def test_5c_the_running_new_alone_still_says_it_was_not_run(tmp_path):
    seed_mirror(tmp_path, busy=True, current={"kind": "new_session", "turn": None,
                                              "text": "", "startedAt": "x"})
    again, _ = restart(tmp_path)
    again.close()
    lost = cards(again.transcript_path, "queue_lost")
    assert len(lost) == 1 and lost[0]["content"].splitlines()[1:] == ["- /new"]
    assert cards(again.transcript_path, "turn_interrupted") == []


def test_5d_a_running_new_is_the_current_job(tmp_path):
    gate = threading.Event()
    client = FakeClient(session_result=SESSION_RESULT, new_ids=["sid-1", "sid-2"])
    real_new = client.new_session
    seen: list = []

    def slow_new(cwd, mcp_servers=None, timeout=60.0):
        if len([c for c in client.calls if c[0] == "new_session"]) >= 1:
            seen.append(json.loads(sess.state_file.read_text(encoding="utf-8")))
            gate.wait(timeout=5)
        return real_new(cwd, mcp_servers, timeout)

    client.new_session = slow_new
    sess = make_session(tmp_path, client)
    sess.start()
    try:
        assert sess.new_session()["ok"] is True
        assert wait_for(lambda: seen)
        cur = seen[0]["current"]
        assert seen[0]["busy"] is True
        assert cur["kind"] == "new_session" and cur["turn"] is None
        gate.set()
        assert sess.wait_idle(timeout=5)
        assert mirror(sess)["current"] is None
    finally:
        gate.set()
        sess.close()


# ── 6. Hermes: the retired session after ChatDaemon.restart writes nothing ─


class _HeldRaceClient(_RestartRaceClient):
    """`_RestartRaceClient`, but the blocked prompt — released by close() —
    waits for the test before it returns: so the retired session's worker
    acts only AFTER the new session is busy, and any write it makes is
    visible as a clobber, not lost in the noise of identical content."""

    def __init__(self, session_result, hold: threading.Event):
        super().__init__(session_result)
        self._hold = hold

    def prompt(self, session_id, text, timeout=600.0):
        result = super().prompt(session_id, text, timeout)
        self._hold.wait(timeout=10)
        return result


def _hermes_module():
    sys.path.insert(0, str(REPO / "scripts"))
    import hermes_acp_chat  # noqa: PLC0415 — scripts/ is not on the path by default

    return hermes_acp_chat


def test_6_hermes_restart_card_and_the_retired_session_writes_nothing(tmp_path):
    hermes = _hermes_module()
    hold = threading.Event()
    next_gate = threading.Event()
    clients = [
        _HeldRaceClient(SESSION_RESULT, hold),
        FakeClient(session_result=SESSION_RESULT,
                   turns=[{"chunks": ["new"], "gate": next_gate}]),
    ]
    made: list = []
    workspace = tmp_path / "workspace"
    sessions_dir = workspace / "omp-sessions" / "--work--"
    sessions_dir.mkdir(parents=True)

    def factory():
        client = clients[len(made)]
        made.append(client)
        return acp_chat.ChatSession(
            client_factory=lambda: client, cwd=str(tmp_path / "work"),
            state_dir=workspace, sessions_dir=sessions_dir, driver="hermes",
            permission_policy="yolo",
        )

    daemon = hermes.ChatDaemon(factory)
    daemon.start()
    old = daemon.session
    try:
        status, answer = daemon.request("prompt", {"text": "hang me"})
        assert status == 200 and answer["ok"] is True
        assert wait_for(lambda: old.state()["busy"])
        assert wait_for(lambda: "hang me" in user_texts(read_lines(old.transcript_path)))
        transcript = old.transcript_path

        daemon.restart()  # stop() closes the old session, start() builds a new one
        new = daemon.session
        assert new is not old and len(made) == 2

        found = cards(transcript, "turn_interrupted")
        assert len(found) == 1 and found[0]["data"]["retryText"] == "hang me"
        assert prompts(made[1]) == [], "the restart sent the text again"

        # The new session is busy with the operator's next message ...
        status, answer = daemon.request("prompt", {"text": "next message"})
        assert status == 200 and answer["ok"] is True
        assert wait_for(lambda: (mirror(new)["current"] or {}).get("text") == "next message")
        assert wait_for(lambda: "next message" in user_texts(read_lines(transcript)))
        lines_before = read_lines(transcript)
        state_before = new.state_file.read_bytes()

        # ... and only now the retired turn returns.
        hold.set()
        assert old.wait_idle(timeout=5)
        time.sleep(0.3)

        assert new.state_file.read_bytes() == state_before, (
            "the retired session rewrote acp-chat-state.json over the new one")
        assert mirror(new)["busy"] is True
        assert read_lines(transcript) == lines_before, (
            "the retired session wrote into the shared transcript")
        assert len(made) == 2, "the retired session spawned another child"
        assert prompts(made[0]) == ["hang me"]
    finally:
        hold.set()
        next_gate.set()
        daemon.stop()


# ── 7. a text over the limit: never cut, no button ──────────────────────────


@pytest.mark.parametrize("length,whole", [(LIMIT, True), (LIMIT + 1, False)])
def test_7_a_text_over_8000_characters_gives_a_card_without_retry_text(
        tmp_path, length, whole):
    text = ("abcdefghij" * (length // 10 + 1))[:length]
    assert len(text) == length
    gate = threading.Event()
    client = FakeClient(session_result=SESSION_RESULT, turns=[{"gate": gate}])
    sess = make_session(tmp_path, client)
    sess.start()
    sess.prompt(text)
    assert wait_for(lambda: sess.state()["busy"])
    cur = mirror(sess)["current"]
    if whole:
        assert cur["text"] == text and cur["truncated"] is False
    else:
        assert cur["text"] is None and cur["truncated"] is True, (
            "a long text must be left out, never cut")
    transcript = sess.transcript_path
    sess.close()
    gate.set()
    assert sess.wait_idle(timeout=5)

    again, client2 = restart(tmp_path)
    again.close()
    found = cards(transcript, "turn_interrupted")
    assert len(found) == 1
    if whole:
        assert found[0]["data"]["retryText"] == text
    else:
        assert "retryText" not in found[0]["data"]
    assert prompts(client2) == []


def test_7b_process_exit_with_a_text_over_the_limit_has_no_retry_text(tmp_path):
    sess, _, respawned = child_dies(tmp_path, "y" * (LIMIT + 1))
    try:
        found = cards(sess.transcript_path, "process_exit")
        assert len(found) == 1 and "retryText" not in found[0]["data"]
        assert prompts(respawned) == []
    finally:
        sess.close()


@pytest.mark.parametrize("text", ["", "   \n"])
def test_7c_an_empty_text_offers_nothing_to_resend(tmp_path, text):
    sess, _, _ = child_dies(tmp_path, text)
    try:
        found = cards(sess.transcript_path, "process_exit")
        assert len(found) == 1 and "retryText" not in found[0]["data"]
    finally:
        sess.close()


# ── 8. map_chat_error: `data` is optional and changes nothing without it ───


GOLDEN = (
    '{"type": "custom_message", "customType": "chat_error", "content": "The ACP '
    'process died — reconnecting the session.", "display": true, "attribution": '
    '"agent", "id": "acp00001", "parentId": null, "timestamp": '
    '"2026-10-20T12:00:00Z", "data": {"code": "process_exit", "detail": "child '
    'exited during turn 3"}}'
)


def _fixed_mapper(monkeypatch) -> acp_chat_events.ACPEventMapper:
    monkeypatch.setattr(acp_chat_events, "_now_iso", lambda: "2026-10-20T12:00:00Z")
    mapper = acp_chat_events.ACPEventMapper()
    mapper.seq = 0
    return mapper


@pytest.mark.parametrize("extra", [{}, {"data": None}, {"data": {}}],
                         ids=["no-data", "data-none", "data-empty"])
def test_8_map_chat_error_without_data_is_byte_identical(monkeypatch, extra):
    mapper = _fixed_mapper(monkeypatch)
    lines = acp_chat_events.ACPEventMapper.dump(mapper.map_chat_error(
        "process_exit", "child exited during turn 3",
        "The ACP process died — reconnecting the session.", **extra))
    assert lines == [GOLDEN]


def test_8b_data_is_merged_but_never_replaces_code_or_detail(monkeypatch):
    mapper = _fixed_mapper(monkeypatch)
    long_text = "z" * 5000  # longer than the 4000 the content is cut to
    entries = mapper.map_chat_error("turn_interrupted", "turn 2", INTERRUPTED_TEXT, data={
        "retryText": long_text, "code": "other", "detail": "other", "model": None})
    assert entries[-1]["data"] == {"code": "turn_interrupted", "detail": "turn 2",
                                   "retryText": long_text}


def test_8c_an_open_thought_still_comes_before_a_card_with_data(monkeypatch):
    mapper = _fixed_mapper(monkeypatch)
    mapper.map_update({"update": {
        "sessionUpdate": "agent_thought_chunk", "messageId": "t1",
        "content": {"type": "text", "text": "thinking before the restart"}}}, stream=True)
    entries = mapper.map_chat_error("process_exit", "x", data={"retryText": "again"})
    assert entries[0]["message"]["content"][0]["type"] == "thinking"
    assert entries[-1]["data"]["retryText"] == "again"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
