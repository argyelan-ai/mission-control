#!/usr/bin/env python3
"""The option list an agent sends in its REPLY to `session/set_config_option`.

ACP's `SetSessionConfigOptionResponse` carries `configOptions`: the full set of
options after the change. omp 18.10.1 builds it fresh for every reply
(`acp-agent.ts` setSessionConfigOption). A model switch changes more than the
model row: the new model brings its own thinking ladder.

The daemon used to throw that reply away and wait for a
`config_option_update` push instead. omp loses the push when a model switch
starts inside its 50 ms bootstrap guard (`ACP_BOOTSTRAP_RACE_GUARD_MS`) and
finishes after it. The chat daemon pins the model right after `session/new`,
which is exactly that window. The state file then kept the ladder of the model
the session opened on, and the effort chip offered levels the running model
does not have (and hid the ones it has).

Rules pinned here:
  * a full reply (it names the option just set) replaces the list;
  * a push that arrives while the request is out, or after it, wins over the
    reply — the agent's own push is never overwritten; the value written into
    it is the one the agent reports, not the one requested;
  * config calls go out one at a time, so an older reply never lands after a
    newer one;
  * a reply without a usable list (`{}`, or `[]` as `hermes acp` answers,
    live-checked) leaves the list as it was, as before.

Runs two ways:
  * pytest:      pytest test_acp_chat_config_options.py -v
  * standalone:  python3 test_acp_chat_config_options.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import acp_chat  # noqa: E402,F401 — the unit under test, via make_session
from test_acp_chat import FakeClient, make_session  # noqa: E402

START_MODEL = "prov/model-start"
NEXT_MODEL = "prov/model-next"
#: The ladder of the model a fresh session opens on.
START_LADDER = ["off", "auto", "low", "medium", "high", "xhigh"]
#: The ladder of the model the daemon pins — a different set, with `max`.
NEXT_LADDER = ["off", "auto", "low", "high", "max"]


def option_set(model: str, ladder: list, thinking: str = "high") -> list:
    """A full option set shaped like omp's `#buildConfigOptions`."""
    return [
        {"id": "mode", "category": "mode", "type": "select", "currentValue": "default",
         "options": [{"value": "default", "name": "Default"}]},
        {"id": "model", "category": "model", "type": "select", "currentValue": model,
         "options": [{"value": START_MODEL, "name": "Start"},
                     {"value": NEXT_MODEL, "name": "Next"}]},
        {"id": "thinking", "category": "thought_level", "type": "select",
         "currentValue": thinking,
         "options": [{"value": v, "name": v} for v in ladder]},
    ]


SESSION_RESULT = {"sessionId": "sid-1",
                  "configOptions": option_set(START_MODEL, START_LADDER)}


class ReplyClient(FakeClient):
    """Answers `set_config_option` the way an ACP agent does: with the option
    set after the change, in the REPLY only. No `config_option_update` is
    pushed — the case omp 18.10.1 produces in its bootstrap race.

    `reply(key, value)` builds the answer. `during(key, value)` runs inside
    the request, before the reply goes out (a push the agent sends first).
    `replied` is set right before the reply is handed back."""

    def __init__(self, *, reply, during=None, **kw):
        super().__init__(**kw)
        self._reply = reply
        self._during = during
        self.replied = threading.Event()

    def set_config_option(self, session_id, key, value, timeout=30.0):
        self.calls.append(("set_config_option", session_id, key, value))
        if self._during is not None:
            self._during(key, value)
        answer = self._reply(key, value)
        self.replied.set()
        return answer


def omp_like_reply(key, value):
    """The new set after the change: a model switch brings that model's
    ladder, a thinking switch moves only the thinking row."""
    if key == "model":
        return {"configOptions": option_set(value, NEXT_LADDER if value == NEXT_MODEL
                                            else START_LADDER)}
    if key == "thinking":
        return {"configOptions": option_set(NEXT_MODEL, NEXT_LADDER, thinking=value)}
    return {"configOptions": option_set(START_MODEL, START_LADDER)}


def push(client: FakeClient, options: list) -> None:
    client.fire({"sessionId": "sid-1",
                 "update": {"sessionUpdate": "config_option_update",
                            "configOptions": options}})


def state_file_option(sess, option_id: str) -> dict:
    """The row as the BACKEND reads it: from acp-chat-state.json on disk."""
    state = json.loads(Path(sess.state_file).read_text())
    rows = [o for o in state["configOptions"] if o.get("id") == option_id]
    assert len(rows) == 1, state["configOptions"]
    return rows[0]


def ladder(row: dict) -> list:
    return [o["value"] for o in row.get("options") or []]


# ── 1. the reply's list reaches the state file ──────────────────────────────


def test_model_pin_takes_the_new_ladder_from_the_reply(tmp_path):
    """The live bug: the daemon pins the model right after session/new, omp
    loses the push, and the ladder of the opening model stayed in the file."""
    client = ReplyClient(session_result=SESSION_RESULT, reply=omp_like_reply)
    sess = make_session(tmp_path, client, model=NEXT_MODEL)
    sess.start()
    try:
        assert ("set_config_option", "sid-1", "model", NEXT_MODEL) in client.calls
        thinking = state_file_option(sess, "thinking")
        assert ladder(thinking) == NEXT_LADDER
        assert state_file_option(sess, "model")["currentValue"] == NEXT_MODEL
    finally:
        sess.close()
    print("PASS test_model_pin_takes_the_new_ladder_from_the_reply")


def test_config_answer_and_state_carry_the_reply_list(tmp_path):
    """A switch from the chat (`config` op): the answer the backend gets and
    the file the chip reads both show the agent's new set."""
    client = ReplyClient(session_result=SESSION_RESULT, reply=omp_like_reply)
    sess = make_session(tmp_path, client)
    sess.start()
    try:
        answer = sess.config("model", NEXT_MODEL)
        assert answer["ok"] is True
        rows = {o["id"]: o for o in answer["configOptions"]}
        assert ladder(rows["thinking"]) == NEXT_LADDER
        assert ladder(state_file_option(sess, "thinking")) == NEXT_LADDER

        assert sess.config("thinking", "max")["ok"] is True
        thinking = state_file_option(sess, "thinking")
        assert thinking["currentValue"] == "max"
        assert ladder(thinking) == NEXT_LADDER
    finally:
        sess.close()
    print("PASS test_config_answer_and_state_carry_the_reply_list")


def test_the_reply_list_is_a_copy(tmp_path):
    """The daemon edits `currentValue` in place later — that must never write
    back into the client's reply object."""
    replies = []

    def keep(key, value):
        answer = omp_like_reply(key, value)
        replies.append(answer)
        return answer

    client = ReplyClient(session_result=SESSION_RESULT, reply=keep)
    sess = make_session(tmp_path, client)
    sess.start()
    try:
        sess.config("model", NEXT_MODEL)
        sess._on_event({"sessionId": "sid-1",
                        "update": {"sessionUpdate": "current_mode_update",
                                   "currentModeId": "plan"}})
        mode = [o for o in replies[0]["configOptions"] if o["id"] == "mode"][0]
        assert mode["currentValue"] == "default"
    finally:
        sess.close()
    print("PASS test_the_reply_list_is_a_copy")


# ── 2. the agent's own push still wins ──────────────────────────────────────


def test_a_later_push_still_wins(tmp_path):
    client = ReplyClient(session_result=SESSION_RESULT, reply=omp_like_reply)
    sess = make_session(tmp_path, client)
    sess.start()
    try:
        sess.config("model", NEXT_MODEL)
        assert ladder(state_file_option(sess, "thinking")) == NEXT_LADDER
        later = ["off", "low", "high"]
        push(client, option_set(NEXT_MODEL, later, thinking="low"))
        thinking = state_file_option(sess, "thinking")
        assert ladder(thinking) == later
        assert thinking["currentValue"] == "low"
    finally:
        sess.close()
    print("PASS test_a_later_push_still_wins")


def test_a_push_between_reply_and_apply_is_not_overwritten(tmp_path):
    """The ACP reader thread hands the reply over and reads on: a push can be
    applied before config() takes the lock again. That push is newer than the
    reply and must stay. Deterministic: while the request is out, the test
    takes the session lock (an RLock, so its own push still goes through),
    lets the reply go back, pushes, and only then lets config() continue."""
    in_request = threading.Event()
    lock_held = threading.Event()

    def handshake(key, value):
        in_request.set()
        assert lock_held.wait(timeout=5)

    client = ReplyClient(session_result=SESSION_RESULT, reply=omp_like_reply,
                         during=handshake)
    sess = make_session(tmp_path, client)
    sess.start()
    newer = ["off", "low", "high"]
    result = {}
    worker = threading.Thread(
        target=lambda: result.update(sess.config("model", NEXT_MODEL)), daemon=True)
    try:
        worker.start()
        assert in_request.wait(timeout=5)
        with sess._lock:
            lock_held.set()
            assert client.replied.wait(timeout=5)
            push(client, option_set(NEXT_MODEL, newer, thinking="low"))
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert result.get("ok") is True
        assert ladder(state_file_option(sess, "thinking")) == newer
        assert state_file_option(sess, "model")["currentValue"] == NEXT_MODEL
    finally:
        lock_held.set()
        worker.join(timeout=5)
        sess.close()
    print("PASS test_a_push_between_reply_and_apply_is_not_overwritten")


def test_a_push_during_the_request_is_kept(tmp_path):
    """A push the agent sent before its reply already carries the agent's
    list: it is kept, and only the value just set is written into it (the
    behaviour before this change)."""
    pushed = ["off", "low", "high"]
    client = ReplyClient(
        session_result=SESSION_RESULT, reply=omp_like_reply,
        during=lambda key, value: push(client, option_set(NEXT_MODEL, pushed)),
    )
    sess = make_session(tmp_path, client)
    sess.start()
    try:
        sess.config("thinking", "high")
        thinking = state_file_option(sess, "thinking")
        assert ladder(thinking) == pushed
        assert thinking["currentValue"] == "high"
    finally:
        sess.close()
    print("PASS test_a_push_during_the_request_is_kept")


def test_a_push_during_the_request_keeps_the_value_the_agent_reports(tmp_path):
    """An agent may run a different value than the one requested: omp clamps
    a level the model does not offer (live 20.10.2026, omp 18.10.1: `medium`
    on a low/high/max model is reported back as `low`, both in its push and
    in its reply). The state must name the level the agent runs — one the
    ladder contains — not the requested one, or the chip shows no level."""
    pushed = option_set(NEXT_MODEL, NEXT_LADDER, thinking="low")

    def clamped(key, value):
        return {"configOptions": option_set(NEXT_MODEL, NEXT_LADDER, thinking="low")}

    client = ReplyClient(
        session_result=SESSION_RESULT, reply=clamped,
        during=lambda key, value: push(client, pushed),
    )
    sess = make_session(tmp_path, client)
    sess.start()
    try:
        answer = sess.config("thinking", "medium")
        assert answer["ok"] is True
        rows = {o["id"]: o for o in answer["configOptions"]}
        assert rows["thinking"]["currentValue"] == "low"
        thinking = state_file_option(sess, "thinking")
        assert ladder(thinking) == NEXT_LADDER
        assert thinking["currentValue"] == "low"
    finally:
        sess.close()
    print("PASS test_a_push_during_the_request_keeps_the_value_the_agent_reports")


def test_a_reply_row_without_a_value_falls_back_to_the_request(tmp_path):
    """A reply that names the option but carries no `currentValue` for it
    says nothing about the value: the requested one is written, as before."""
    pushed = option_set(NEXT_MODEL, NEXT_LADDER, thinking="high")

    def no_value(key, value):
        rows = option_set(NEXT_MODEL, NEXT_LADDER)
        for row in rows:
            if row["id"] == "thinking":
                row.pop("currentValue")
        return {"configOptions": rows}

    client = ReplyClient(
        session_result=SESSION_RESULT, reply=no_value,
        during=lambda key, value: push(client, pushed),
    )
    sess = make_session(tmp_path, client)
    sess.start()
    try:
        assert sess.config("thinking", "max")["ok"] is True
        assert state_file_option(sess, "thinking")["currentValue"] == "max"
    finally:
        sess.close()
    print("PASS test_a_reply_row_without_a_value_falls_back_to_the_request")


# ── 3. overlapping config calls apply in the order the agent ran them ───────


def test_overlapping_configs_never_apply_an_older_reply_last(tmp_path):
    """Two config calls at once (the operator's chip while the daemon pins
    the model) and no push in between: the replies are not ordered against
    each other. A thinking reply built BEFORE a model switch must not land
    after the model reply — the state would name the old model and its
    ladder while the agent runs the new one. Config calls go out one at a
    time: the model request waits until the thinking reply is applied."""
    agent = {"model": START_MODEL}
    thinking_out = threading.Event()
    release_thinking = threading.Event()
    model_out = threading.Event()

    def ladder_of(model):
        return NEXT_LADDER if model == NEXT_MODEL else START_LADDER

    def reply(key, value):
        if key == "model":
            agent["model"] = value
            model_out.set()
            return {"configOptions": option_set(value, ladder_of(value))}
        # The set as the agent sees it when it handles the request —
        # handed back late, after the test lets it go.
        built = option_set(agent["model"], ladder_of(agent["model"]), thinking=value)
        thinking_out.set()
        release_thinking.wait(timeout=5)
        return {"configOptions": built}

    client = ReplyClient(session_result=SESSION_RESULT, reply=reply)
    sess = make_session(tmp_path, client)
    sess.start()
    results = {}
    # daemon: a config call stuck behind a broken lock fails the test
    # instead of keeping the test process alive.
    slow = threading.Thread(
        target=lambda: results.update(thinking=sess.config("thinking", "low")),
        daemon=True)
    fast = threading.Thread(
        target=lambda: results.update(model=sess.config("model", NEXT_MODEL)),
        daemon=True)
    try:
        slow.start()
        assert thinking_out.wait(timeout=5)
        fast.start()
        overlapped = model_out.wait(timeout=0.3)
        release_thinking.set()
        slow.join(timeout=5)
        fast.join(timeout=5)
        assert not slow.is_alive() and not fast.is_alive()
        assert results["thinking"]["ok"] is True
        assert results["model"]["ok"] is True
        assert state_file_option(sess, "model")["currentValue"] == NEXT_MODEL
        assert ladder(state_file_option(sess, "thinking")) == NEXT_LADDER
        assert not overlapped, "model request went out while the thinking one was open"
    finally:
        release_thinking.set()
        slow.join(timeout=5)
        fast.join(timeout=5)
        sess.close()
    print("PASS test_overlapping_configs_never_apply_an_older_reply_last")


def test_a_refused_config_does_not_block_the_next_one(tmp_path):
    """A failed request releases the queue: the next config goes out."""
    def flaky(key, value):
        if key == "model":
            raise RuntimeError("session/set_config_option failed: unknown model")
        return omp_like_reply(key, value)

    client = ReplyClient(session_result=SESSION_RESULT, reply=flaky)
    sess = make_session(tmp_path, client)
    sess.start()
    try:
        assert sess.config("model", "prov/nope")["ok"] is False
        done = {}
        worker = threading.Thread(
            target=lambda: done.update(sess.config("thinking", "max")), daemon=True)
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive(), "second config hung behind the refused one"
        assert done.get("ok") is True
    finally:
        sess.close()
    print("PASS test_a_refused_config_does_not_block_the_next_one")


# ── 4. a reply without a usable list changes nothing ────────────────────────


def _assert_list_kept(tmp_path, answer: dict) -> None:
    client = ReplyClient(session_result=SESSION_RESULT, reply=lambda key, value: answer)
    sess = make_session(tmp_path, client)
    sess.start()
    try:
        assert sess.config("thinking", "low")["ok"] is True
        thinking = state_file_option(sess, "thinking")
        assert ladder(thinking) == START_LADDER
        assert thinking["currentValue"] == "low"
        assert [o["id"] for o in json.loads(Path(sess.state_file).read_text())
                ["configOptions"]] == ["mode", "model", "thinking"]
    finally:
        sess.close()


def test_empty_reply_keeps_the_list(tmp_path):
    _assert_list_kept(tmp_path, {})
    print("PASS test_empty_reply_keeps_the_list")


def test_hermes_placeholder_reply_keeps_the_list(tmp_path):
    """`hermes acp` answers every set_config_option with `configOptions: []`
    (it has no typed config surface yet, live-checked 20.10.2026). An empty
    list is no option set — taking it would wipe the chip and the model row."""
    _assert_list_kept(tmp_path, {"configOptions": []})
    print("PASS test_hermes_placeholder_reply_keeps_the_list")


def test_reply_without_the_option_just_set_keeps_the_list(tmp_path):
    """A full set always names the option that was just set. A list without
    it is a partial answer, not the agent's whole state."""
    _assert_list_kept(tmp_path, {"configOptions": [
        {"id": "mode", "currentValue": "default", "options": []}]})
    print("PASS test_reply_without_the_option_just_set_keeps_the_list")


def test_garbage_reply_keeps_the_list(tmp_path):
    _assert_list_kept(tmp_path, {"configOptions": "thinking"})
    print("PASS test_garbage_reply_keeps_the_list")


if __name__ == "__main__":
    import inspect

    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            if "tmp_path" in inspect.signature(fn).parameters:
                with tempfile.TemporaryDirectory() as td:
                    fn(Path(td))
            else:
                fn()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {exc!r}")
    sys.exit(1 if failures else 0)
