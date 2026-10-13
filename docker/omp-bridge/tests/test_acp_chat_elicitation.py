#!/usr/bin/env python3
"""Chat package P3 — question cards (ACP form elicitation) and plan mode.

The request shapes below are copied from omp 18.10.1's ACP agent
(`elicitFromAcpClient` / `askDialog` / `#requestAcpPlanApprovalChoice`):
omp only asks when the client advertises `clientCapabilities.elicitation.form`,
then sends `elicitation/create` and waits for `{action, content}`.

Three layers:
  * ACPClient — capability advertised only with a handler; the request is
    held (not answered on the reader thread) and answered via respond().
  * ChatSession — card line in the transcript, `answer` op, expiry when the
    turn ends or the daemon restarts, plan-mode state.
  * the task path never opts in (bridge.py builds no elicitation handler).
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import acp_chat  # noqa: E402
import acp_chat_events  # noqa: E402
import acp_client  # noqa: E402
from test_acp_chat import (  # noqa: E402
    SESSION_RESULT,
    FakeClient,
    entries_of_type,
    make_session,
    read_lines,
)

# ── omp's request shapes ─────────────────────────────────────────────────────

SELECT_PARAMS = {
    "mode": "form",
    "sessionId": "sid-1",
    "message": "Which database?",
    "requestedSchema": {
        "type": "object",
        "properties": {"value": {"type": "string", "enum": ["Postgres", "SQLite"]}},
        "required": ["value"],
    },
}

CONFIRM_PARAMS = {
    "mode": "form",
    "sessionId": "sid-1",
    "message": "Delete the branch?\n\nIt has no PR.",
    "requestedSchema": {
        "type": "object",
        "properties": {"value": {"type": "boolean"}},
        "required": ["value"],
    },
}

ASK_PARAMS = {
    "mode": "form",
    "sessionId": "sid-1",
    "message": "Answer 2 questions",
    "requestedSchema": {
        "type": "object",
        "properties": {
            "q0": {"type": "string", "title": "Which framework?", "description": "Frontend",
                   "oneOf": [{"const": "React", "title": "React", "description": "the usual"},
                             {"const": "Vue", "title": "Vue"}],
                   "default": "React"},
            "q0__other": {"type": "string", "title": "Other (type your own)"},
            "q1": {"type": "array", "title": "Which checks?",
                   "items": {"anyOf": [{"const": "lint", "title": "lint"},
                                       {"const": "test", "title": "test"}]}},
            "q1__other": {"type": "string", "title": "Other (type your own)"},
        },
    },
}

PLAN_PARAMS = {
    "mode": "form",
    "sessionId": "sid-1",
    "message": 'Approve plan "Add login" and start implementation?\n\n# Plan\n1. form',
    "requestedSchema": {
        "type": "object",
        "properties": {"value": {"type": "string",
                                 "enum": ["Approve and execute", "Refine plan"]}},
        "required": ["value"],
    },
}


class ElicitingClient(FakeClient):
    """FakeClient plus the elicitation surface of ACPClient."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self._elicit = None
        self.replies: list[tuple] = []

    def on_elicitation(self, cb, on_complete=None):
        self._elicit = cb

    def respond(self, request_id, result):
        self.replies.append((request_id, result))

    def ask(self, rpc_id, params):
        assert self._elicit is not None, "chat daemon registered no elicitation handler"
        self._elicit(rpc_id, params)


def _cards(lines):
    return entries_of_type(lines, "custom_message", acp_chat_events.ELICITATION_CUSTOM_TYPE)


def _results(lines):
    return [l["data"] for l in
            entries_of_type(lines, "custom", acp_chat_events.ELICITATION_RESULT_CUSTOM_TYPE)]


def _started(tmp_path, **kw):
    client = ElicitingClient(session_result=SESSION_RESULT, **kw)
    sess = make_session(tmp_path, client, elicitation=True)
    sess.start()
    return client, sess


def _wait_busy(sess):
    deadline = time.time() + 3
    while not sess.state()["busy"] and time.time() < deadline:
        time.sleep(0.01)


# ── ACPClient ────────────────────────────────────────────────────────────────


class _WireClient(acp_client.ACPClient):
    """ACPClient with the pipe replaced by a list — no process."""

    def __init__(self):
        super().__init__()
        self.sent: list[dict] = []

    def _send_raw(self, obj):
        self.sent.append(obj)

    def _request(self, method, params, timeout=300.0):
        self.sent.append({"method": method, "params": params})
        return {}


def test_capability_is_advertised_only_with_a_handler():
    plain = _WireClient()
    plain.initialize()
    caps = plain.sent[-1]["params"]["clientCapabilities"]
    assert "elicitation" not in caps, "task runs must not advertise elicitation"

    asking = _WireClient()
    asking.on_elicitation(lambda rid, params: None)
    asking.initialize()
    caps = asking.sent[-1]["params"]["clientCapabilities"]
    assert caps["elicitation"] == {"form": {}}
    assert caps["fs"] == {"readTextFile": True, "writeTextFile": True}


def test_request_is_held_then_answered_with_the_same_id():
    client = _WireClient()
    seen = []
    client.on_elicitation(lambda rid, params: seen.append((rid, params)))
    client._dispatch({"jsonrpc": "2.0", "id": 7, "method": "elicitation/create",
                      "params": SELECT_PARAMS})
    assert seen == [(7, SELECT_PARAMS)]
    assert client.sent == [], "the reader thread must not answer for the operator"
    client.respond(7, {"action": "accept", "content": {"value": "SQLite"}})
    assert client.sent == [{"jsonrpc": "2.0", "id": 7,
                            "result": {"action": "accept", "content": {"value": "SQLite"}}}]


def test_without_handler_the_request_is_refused_as_before():
    client = _WireClient()
    client._dispatch({"jsonrpc": "2.0", "id": 8, "method": "elicitation/create",
                      "params": SELECT_PARAMS})
    assert client.sent[0]["error"]["code"] == -32601


def test_broken_handler_declines_instead_of_hanging_omp():
    client = _WireClient()

    def boom(rid, params):
        raise RuntimeError("bug")

    client.on_elicitation(boom)
    client._dispatch({"jsonrpc": "2.0", "id": 9, "method": "elicitation/create",
                      "params": SELECT_PARAMS})
    assert client.sent == [{"jsonrpc": "2.0", "id": 9, "result": {"action": "decline"}}]


# ── ChatSession: the card ────────────────────────────────────────────────────


def test_question_becomes_a_card_and_an_accepted_answer_reaches_omp(tmp_path):
    gate = threading.Event()
    client, sess = _started(tmp_path, turns=[{"gate": gate}])
    sess.prompt("pick a db")
    _wait_busy(sess)
    client.ask(41, SELECT_PARAMS)

    cards = _cards(read_lines(sess.transcript_path))
    assert len(cards) == 1
    data = cards[0]["data"]
    assert data["message"] == "Which database?"
    assert data["schema"]["properties"]["value"]["enum"] == ["Postgres", "SQLite"]
    assert cards[0]["content"] == "Which database?"
    eid = data["elicitationId"]
    assert [e["id"] for e in sess.state()["elicitations"]] == [eid]

    assert acp_chat.dispatch(sess, {"op": "answer", "id": eid, "action": "accept",
                                    "content": {"value": "SQLite"}}) == \
        {"ok": True, "status": "answered"}
    assert client.replies == [(41, {"action": "accept", "content": {"value": "SQLite"}})]
    assert sess.state()["elicitations"] == []
    assert _results(read_lines(sess.transcript_path)) == [
        {"elicitationId": eid, "status": "answered", "content": {"value": "SQLite"}}
    ]

    # a second click (other tab, double tap) is a clear refusal
    again = sess.answer(eid, "accept", {"value": "SQLite"})
    assert again == {"ok": False, "error": "not_pending"}
    assert len(client.replies) == 1

    gate.set()
    assert sess.wait_idle(timeout=5)
    sess.close()


def test_cancel_and_decline_send_no_content(tmp_path):
    gate = threading.Event()
    client, sess = _started(tmp_path, turns=[{"gate": gate}])
    sess.prompt("x")
    _wait_busy(sess)
    client.ask(1, CONFIRM_PARAMS)
    client.ask(2, PLAN_PARAMS)
    first, second = [c["data"]["elicitationId"] for c in _cards(read_lines(sess.transcript_path))]
    assert sess.answer(first, "cancel")["status"] == "cancelled"
    assert sess.answer(second, "decline")["status"] == "declined"
    assert client.replies == [(1, {"action": "cancel"}), (2, {"action": "decline"})]
    statuses = [r["status"] for r in _results(read_lines(sess.transcript_path))]
    assert statuses == ["cancelled", "declined"]
    gate.set()
    sess.wait_idle(timeout=5)
    sess.close()


def test_invalid_answers_are_refused_and_the_card_stays_open(tmp_path):
    gate = threading.Event()
    client, sess = _started(tmp_path, turns=[{"gate": gate}])
    sess.prompt("x")
    _wait_busy(sess)
    client.ask(1, SELECT_PARAMS)
    client.ask(2, CONFIRM_PARAMS)
    client.ask(3, ASK_PARAMS)
    sel, conf, ask = [c["data"]["elicitationId"] for c in _cards(read_lines(sess.transcript_path))]

    assert sess.answer(sel, "accept", {"value": "MySQL"})["error"] == "bad_content"
    assert sess.answer(sel, "accept", {})["error"] == "bad_content"  # required
    assert sess.answer(conf, "accept", {"value": "yes"})["error"] == "bad_content"
    assert sess.answer(ask, "accept", {"q9": "x"})["error"] == "bad_content"
    assert sess.answer(ask, "accept", {"q1": ["deploy"]})["error"] == "bad_content"
    assert sess.answer(sel, "maybe")["error"] == "bad_action"
    assert client.replies == []
    assert len(sess.state()["elicitations"]) == 3

    # the askDialog shape: a choice, a free-text "other", a multi-select
    ok = sess.answer(ask, "accept", {"q0": "Vue", "q1": ["lint", "test"],
                                     "q1__other": "e2e"})
    assert ok["ok"] is True
    assert client.replies[-1] == (3, {"action": "accept", "content": {
        "q0": "Vue", "q1": ["lint", "test"], "q1__other": "e2e"}})
    assert sess.answer(conf, "accept", {"value": False})["ok"] is True
    gate.set()
    sess.wait_idle(timeout=5)
    sess.close()


def test_ask_schema_survives_cleaning_with_default_and_other(tmp_path):
    schema = acp_chat.clean_schema(ASK_PARAMS["requestedSchema"])
    q0 = schema["properties"]["q0"]
    assert q0["default"] == "React"
    assert q0["oneOf"][0] == {"const": "React", "title": "React", "description": "the usual"}
    assert schema["properties"]["q0__other"] == {"type": "string",
                                                 "title": "Other (type your own)"}
    assert [c["const"] for c in schema["properties"]["q1"]["items"]["anyOf"]] == ["lint", "test"]
    # unknown fields never reach the transcript
    dirty = {"type": "object", "properties": {"value": {"type": "string", "x-secret": "s",
                                                        "enum": ["a"]}}}
    assert acp_chat.clean_schema(dirty)["properties"]["value"] == {"type": "string",
                                                                  "enum": ["a"]}
    assert acp_chat.clean_schema({"type": "object", "properties": {}}) is None


def test_url_mode_and_garbage_are_declined_at_once(tmp_path):
    client, sess = _started(tmp_path)
    client.ask(5, {"mode": "url", "sessionId": "sid-1", "message": "open",
                   "url": "https://example.invalid", "elicitationId": "e"})
    client.ask(6, {"mode": "form", "sessionId": "sid-1", "message": "?",
                   "requestedSchema": {"type": "object"}})
    assert client.replies == [(5, {"action": "decline"}), (6, {"action": "decline"})]
    assert _cards(read_lines(sess.transcript_path)) == []
    sess.close()


def test_open_card_expires_when_the_turn_ends(tmp_path):
    """omp's own timeout (form mode has no elicitation/complete) or Stop end
    the turn — the card must then say "no longer open"."""
    gate = threading.Event()
    client, sess = _started(tmp_path, turns=[{"gate": gate}])
    sess.prompt("x")
    _wait_busy(sess)
    client.ask(11, SELECT_PARAMS)
    eid = _cards(read_lines(sess.transcript_path))[0]["data"]["elicitationId"]
    gate.set()
    assert sess.wait_idle(timeout=5)
    assert _results(read_lines(sess.transcript_path)) == [
        {"elicitationId": eid, "status": "expired"}
    ]
    assert client.replies == [(11, {"action": "cancel"})]
    assert sess.state()["elicitations"] == []
    assert sess.answer(eid, "accept", {"value": "SQLite"}) == {"ok": False,
                                                              "error": "not_pending"}
    sess.close()


def test_restart_expires_what_the_previous_daemon_held(tmp_path):
    gate = threading.Event()
    client, sess = _started(tmp_path, turns=[{"gate": gate}])
    sess.prompt("x")
    _wait_busy(sess)
    client.ask(21, PLAN_PARAMS)
    eid = _cards(read_lines(sess.transcript_path))[0]["data"]["elicitationId"]
    # the daemon dies with the card open (state mirror still lists it)
    persisted = json.loads(sess.state_file.read_text())
    assert [e["id"] for e in persisted["elicitations"]] == [eid]
    sess._closed = True
    gate.set()
    sess.wait_idle(timeout=5)

    client2 = ElicitingClient(session_result=SESSION_RESULT)
    sess2 = make_session(tmp_path, client2, elicitation=True)
    sess2.start()
    assert {"elicitationId": eid, "status": "expired"} in _results(
        read_lines(sess2.transcript_path))
    assert sess2.answer(eid, "accept", {"value": "Refine plan"})["error"] == "not_pending"
    sess2.close()


def test_daemon_without_opt_in_never_asks(tmp_path):
    client = ElicitingClient(session_result=SESSION_RESULT)
    sess = make_session(tmp_path, client)  # default: elicitation off (host chat)
    sess.start()
    assert client._elicit is None
    sess.close()


def test_task_path_never_registers_an_elicitation_handler():
    """bridge.run_acp_once drives unattended task runs — a question there
    would stall the task forever. Source check: it never opts in."""
    source = (ROOT / "bridge.py").read_text(encoding="utf-8")
    assert "on_elicitation" not in source
    assert "elicitation=True" not in source


# ── plan mode ────────────────────────────────────────────────────────────────

MODE_RESULT = {
    **SESSION_RESULT,
    "configOptions": SESSION_RESULT["configOptions"] + [
        {"id": "mode", "name": "Mode", "category": "mode", "type": "select",
         "currentValue": "default",
         "options": [{"value": "default", "name": "Default"},
                     {"value": "plan", "name": "Plan"}]},
    ],
}


def _mode_of(state):
    return next(o["currentValue"] for o in state["configOptions"] if o["id"] == "mode")


def test_current_mode_update_moves_state_and_writes_a_quiet_line(tmp_path):
    client = ElicitingClient(session_result=MODE_RESULT)
    sess = make_session(tmp_path, client, elicitation=True)
    sess.start()
    assert sess.config("mode", "plan")["ok"] is True
    assert ("set_config_option", "sid-1", "mode", "plan") in client.calls
    client.fire({"sessionId": "sid-1",
                 "update": {"sessionUpdate": "current_mode_update", "currentModeId": "plan"}})
    assert _mode_of(sess.state()) == "plan"
    assert _mode_of(json.loads(sess.state_file.read_text())) == "plan"
    modes = entries_of_type(read_lines(sess.transcript_path), "custom",
                            acp_chat_events.MODE_CUSTOM_TYPE)
    assert [m["data"] for m in modes] == [{"modeId": "plan"}]

    # omp leaves plan mode on approval -> back to default
    client.fire({"sessionId": "sid-1",
                 "update": {"sessionUpdate": "current_mode_update", "currentModeId": "default"}})
    assert _mode_of(sess.state()) == "default"
    sess.close()


def test_session_info_update_is_mirrored_not_shown(tmp_path):
    client, sess = _started(tmp_path)
    client.fire({"sessionId": "sid-1",
                 "update": {"sessionUpdate": "session_info_update", "title": "Login form"}})
    assert sess.state()["sessionInfo"] == {"title": "Login form"}
    lines = read_lines(sess.transcript_path)
    assert not [l for l in lines if "Login form" in json.dumps(l)]
    sess.close()


def test_ctl_shim_knows_the_answer_op():
    import acp_chat_ctl

    assert "answer" in acp_chat_ctl.OPS


if __name__ == "__main__":
    import tempfile

    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            if fn.__code__.co_argcount:
                with tempfile.TemporaryDirectory() as d:
                    fn(Path(d))
            else:
                fn()
            print("PASS", name)
