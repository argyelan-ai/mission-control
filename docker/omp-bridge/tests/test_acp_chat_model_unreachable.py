#!/usr/bin/env python3
"""S1-R6 — "the model is not answering" is a card in the chat, not a reply.

Before S1-R6 a chat turn whose model endpoint was gone ended like this:
omp retried, gave up and sent its error line as ordinary TEXT
(``agent_message_chunk``) with the ACP stop reason ``refusal`` (omp's
pattern ``refus(al|ed)`` matches "ECONNREFUSED") or ``end_turn`` (for
"Unable to connect ..." without ECONNREFUSED). The chat daemon only looked
for ``stopReason == "error"``, which omp never sends here, so the operator
saw omp's error line as the agent's answer. A prompt that failed as a whole
("Internal error: Connection error.") became the card ``rpc_error``.

The reliable signal (plan review F2, live 20.10.2026): omp records every
failed turn in ITS OWN session file as an assistant line with
``stopReason: "error"`` and ``errorMessage``. Intermediate attempts carry
``retryRecovery.status`` ``superseded``/``recovered``; the final line has
none. An abort is ``stopReason: "aborted"`` ("Interrupted by user",
"Operation aborted").

Contract (docs/specs/chat-over-acp.md, "The model is not answering"):

1. omp's final line of the turn says ``error`` and its ``errorMessage``
   matches ``MODEL_UNREACHABLE_RE`` (the task path's pattern, #897, ONE
   source: ``model_errors.py``) -> ONE ``chat_error`` card
   ``model_unreachable``; ``detail`` = omp's line (at most 240 characters),
   ``data.retryText`` = the operator's whole text (S1-R5 rules: only the
   operator's own text, at most 8000 characters, never cut).
2. The reply text IS omp's error line -> no reply bubble. A real reply
   before the error -> the bubble stays and the card comes too.
3. A prompt that fails with a matching error -> ``model_unreachable``,
   not ``rpc_error``.
4. An abort (omp's line ``aborted``, or the abort wording) -> no card.
5. Any other error (401, 429, ...) -> as before S1-R6 (its own card later).
6. Only when omp's file gives no answer (missing, unreadable, no final
   line in time): the END of the text (300 characters) must match AND the
   ACP stop reason is ``refusal`` OR the reply has no ``usage`` at all
   (omp sends none for a turn without tokens — never "0 tokens").
7. omp's file is read from where it ended when the turn started: a line
   of an earlier turn never counts. omp's flush at the turn's end is not
   proven, so the read waits a moment for the final line.
"""
from __future__ import annotations

import ast
import json
import sys
import threading
import uuid
from pathlib import Path

import pytest

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
    assistant_texts,
    entries_of_type,
    make_session,
    read_lines,
)

#: omp's own wording, word for word as in #897's tests (test_model_wait.py)
#: and in omp's session files (live 20.10.2026).
ECONN_TEXT = "ECONNREFUSED: Unable to connect. Is the computer able to access the url?"
UNABLE_TEXT = "Unable to connect. Is the computer able to access the url?"
BUDGET_TEXT = "Retry budget exhausted after 10 retries: " + UNABLE_TEXT
AUTH_TEXT = "401 Unauthorized: the API key was not accepted."

CARD_TEXT = ("The model is not answering — check the Runtimes page. "
             "Your message is not lost.")
NEUTRAL_CARD_TEXT = "The model is not answering — check the Runtimes page."

OPERATOR = "operator"
PROMPT = "Run the test suite and tell me what fails."
USAGE = {"inputTokens": 1200, "outputTokens": 85}
OMP_NAME = "2026-10-20T10-00-00-000Z_sid-1.jsonl"


def model_errors():
    """Imported per test: before S1-R6 the module does not exist, and the
    daemon tests must fail on what the chat shows, not on a collection
    error."""
    import model_errors as module  # noqa: PLC0415

    return module


# ── omp's own session file ──────────────────────────────────────────────────


def _line(message: dict) -> dict:
    return {"type": "message", "id": uuid.uuid4().hex[:8], "parentId": None,
            "timestamp": "2026-10-20T10:00:01.000Z", "message": message}


def omp_user(text: str) -> dict:
    return _line({"role": "user", "content": [{"type": "text", "text": text}]})


def omp_assistant(stop: str, *, error: str = "", retry: str = "",
                  content=None) -> dict:
    """An assistant line as omp 18.10.1 writes it (shape live 20.10.2026)."""
    message = {
        "role": "assistant", "content": content or [], "api": "openai-completions",
        "provider": "local", "model": "test-model",
        "usage": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0,
                  "totalTokens": 0},
        "stopReason": stop, "timestamp": 1789504293692,
    }
    if error:
        message["errorMessage"] = error
    if retry:
        message["retryRecovery"] = {"kind": "auto-retry", "status": retry,
                                    "attempt": 1, "recovery": "plain"}
    return _line(message)


def omp_tool_use() -> dict:
    return omp_assistant("toolUse", content=[
        {"type": "toolCall", "id": "call-1", "name": "bash",
         "arguments": {"command": "make test"}}])


def omp_tool_result() -> dict:
    return _line({"role": "toolResult", "toolCallId": "call-1",
                  "content": [{"type": "text", "text": "3 passed"}]})


def omp_reply(text: str) -> dict:
    return omp_assistant("stop", content=[{"type": "text", "text": text}])


class OmpClient(FakeClient):
    """FakeClient plus omp's OWN session file next to MC's copy.

    Per turn (besides FakeClient's keys):
      "omp":      lines omp appends during the turn (after the user line)
      "omp_late": lines omp appends only AFTER the reply (the flush at the
                  turn's end is not proven), "late_s" seconds later
      "thoughts": agent_thought_chunk texts before the answer
      "silent":   omp writes nothing of this turn (not even its user line)
    """

    def __init__(self, sessions_dir: Path, **kw):
        super().__init__(session_result=SESSION_RESULT, **kw)
        sessions_dir.mkdir(parents=True, exist_ok=True)
        self.omp_file = sessions_dir / OMP_NAME
        self.omp_file.write_text(json.dumps(
            {"type": "session", "version": 3, "id": "sid-1",
             "timestamp": "2026-10-20T10:00:00.000Z", "cwd": "/work"}) + "\n")
        self.late_threads: list[threading.Thread] = []

    def append(self, lines) -> None:
        with open(self.omp_file, "a", encoding="utf-8") as fh:
            for line in lines:
                fh.write(json.dumps(line) + "\n")

    def prompt(self, session_id, text, timeout=600.0):
        turn = self.turns[0] if self.turns else {}
        if not turn.get("silent"):
            self.append([omp_user(text)] + list(turn.get("omp") or []))
        for thought in turn.get("thoughts") or []:
            self.fire({"sessionId": session_id, "update": {
                "sessionUpdate": "agent_thought_chunk", "messageId": "t1",
                "content": {"type": "text", "text": thought}}})
        late = turn.get("omp_late")
        try:
            return super().prompt(session_id, text, timeout=timeout)
        finally:
            if late:
                timer = threading.Timer(turn.get("late_s", 0.15), self.append, args=(late,))
                timer.start()
                self.late_threads.append(timer)


def run_turns(tmp_path: Path, turns: list, *, texts=None, origin=OPERATOR,
              omp_file=True) -> tuple[list[dict], "acp_chat.ChatSession"]:
    """One daemon, the given turns in order; the transcript MC wrote."""
    sessions = tmp_path / "sessions"
    client = OmpClient(sessions, turns=turns) if omp_file else FakeClient(
        session_result=SESSION_RESULT, turns=turns)
    sess = make_session(tmp_path, client)
    sess.start()
    for text in texts or [PROMPT] * len(turns):
        sess.prompt(text, origin=origin)
        assert sess.wait_idle(timeout=5)
    for timer in getattr(client, "late_threads", []):
        timer.join()
    lines = read_lines(sess.transcript_path)
    sess.close()
    return lines, sess


def run_turn(tmp_path: Path, turn: dict, **kw) -> tuple[list[dict], "acp_chat.ChatSession"]:
    return run_turns(tmp_path, [turn], **kw)


def cards(lines, code=None) -> list[dict]:
    out = entries_of_type(lines, "custom_message", acp_chat_events.CHAT_ERROR_CUSTOM_TYPE)
    if code is not None:
        out = [c for c in out if (c.get("data") or {}).get("code") == code]
    return out


def codes(lines) -> list[str]:
    return [(c.get("data") or {}).get("code") for c in cards(lines)]


def replies(lines) -> list[str]:
    """The reply bubbles (assistant text lines; thought lines have none)."""
    return [t for t in assistant_texts(lines) if t]


# ── 1–3. omp's error line is a card, not a reply ────────────────────────────


def test_1_econnrefused_line_is_a_card_not_a_reply(tmp_path):
    lines, sess = run_turn(tmp_path, {
        "chunks": [ECONN_TEXT], "stopReason": "refusal",  # no usage: 0 tokens
        "omp": [omp_assistant("error", error=ECONN_TEXT)],
    })
    assert replies(lines) == [], "omp's error line must not read as the agent's answer"
    assert codes(lines) == ["model_unreachable"]
    card = cards(lines)[0]
    assert card["content"] == CARD_TEXT
    assert card["data"]["detail"] == ECONN_TEXT
    assert card["data"]["retryText"] == PROMPT
    assert sess.state()["lastError"]["code"] == "model_unreachable"


def test_2_unable_to_connect_after_tools_is_a_card(tmp_path):
    """The case the token rule missed (F2, 64x live): no ECONNREFUSED, so
    omp ends ``end_turn``; the model worked first (tools, tokens)."""
    lines, _ = run_turn(tmp_path, {
        "chunks": [UNABLE_TEXT], "stopReason": "end_turn", "usage": USAGE,
        "omp": [omp_tool_use(), omp_tool_result(), omp_assistant("error", error=UNABLE_TEXT)],
    })
    assert replies(lines) == []
    assert codes(lines) == ["model_unreachable"]
    assert cards(lines)[0]["data"]["detail"] == UNABLE_TEXT


def test_2b_an_empty_reply_with_omps_error_line_is_the_card_not_empty_turn(tmp_path):
    lines, _ = run_turn(tmp_path, {
        "chunks": [], "stopReason": "end_turn",
        "omp": [omp_assistant("error", error=ECONN_TEXT)],
    })
    assert codes(lines) == ["model_unreachable"]


def test_3_retry_budget_line_is_a_card(tmp_path):
    attempts = [omp_assistant("error", error=UNABLE_TEXT, retry="superseded")
                for _ in range(3)]
    lines, _ = run_turn(tmp_path, {
        "chunks": [BUDGET_TEXT], "stopReason": "end_turn", "usage": USAGE,
        "omp": [omp_tool_use(), omp_tool_result(), *attempts,
                omp_assistant("error", error=BUDGET_TEXT)],
    })
    assert replies(lines) == []
    assert codes(lines) == ["model_unreachable"]
    assert cards(lines)[0]["data"]["detail"] == BUDGET_TEXT


def test_3b_the_detail_is_at_most_240_characters(tmp_path):
    long_error = BUDGET_TEXT + " " + ("x" * 400)
    lines, _ = run_turn(tmp_path, {
        "chunks": [long_error], "stopReason": "end_turn",
        "omp": [omp_assistant("error", error=long_error)],
    })
    detail = cards(lines, "model_unreachable")[0]["data"]["detail"]
    assert 0 < len(detail) <= 240
    assert detail.startswith("Retry budget exhausted"), "omp's line keeps its start"


# ── 4. a prompt that fails as a whole ───────────────────────────────────────


def test_4_prompt_connection_error_is_model_unreachable_not_rpc_error(tmp_path):
    lines, _ = run_turn(tmp_path, {
        "raise": acp_client.ACPError("session/prompt failed: Internal error (Connection error.)"),
    }, omp_file=False)
    assert codes(lines) == ["model_unreachable"]
    card = cards(lines)[0]
    assert "Connection error." in card["data"]["detail"]
    assert card["data"]["retryText"] == PROMPT


def test_4b_any_other_prompt_failure_stays_rpc_error(tmp_path):
    lines, _ = run_turn(tmp_path, {
        "raise": acp_client.ACPError("session/prompt failed: boom"),
    }, omp_file=False)
    assert codes(lines) == ["rpc_error"]


# ── 5–6. a real answer stays an answer ──────────────────────────────────────


LONG_ANSWER = ("The service log shows the cause in the last hour. " * 8
               + "The client got 'connection refused' because the port was "
                 "closed; I opened it again and the check passes.")
SHORT_ANSWER = "Connection refused on port 8080: the service was down, I restarted it."


@pytest.mark.parametrize("answer", [LONG_ANSWER, SHORT_ANSWER], ids=["long", "short"])
def test_5_a_real_answer_about_connection_refused_stays_a_reply(tmp_path, answer):
    lines, _ = run_turn(tmp_path, {
        "chunks": [answer], "stopReason": "end_turn", "usage": USAGE,
        "omp": [omp_tool_use(), omp_tool_result(), omp_reply(answer)],
    })
    assert replies(lines) == [answer]
    assert codes(lines) == []


def test_6_a_real_reply_then_the_error_keeps_the_reply_and_adds_the_card(tmp_path):
    before = "I started the test run and read the first results."
    text = before + "\n\n" + ECONN_TEXT
    lines, _ = run_turn(tmp_path, {
        "chunks": [before, "\n\n" + ECONN_TEXT], "stopReason": "refusal", "usage": USAGE,
        "omp": [omp_reply(before), omp_tool_use(), omp_tool_result(),
                omp_assistant("error", error=ECONN_TEXT)],
    })
    assert replies(lines) == [text], "the model's own words must stay"
    assert codes(lines) == ["model_unreachable"]


# ── 7–8. other errors and aborts ────────────────────────────────────────────


def test_7_an_auth_error_line_is_unchanged(tmp_path):
    """401/429 get their own card later (plan §14) — here: as before."""
    lines, _ = run_turn(tmp_path, {
        "chunks": [AUTH_TEXT], "stopReason": "end_turn",
        "omp": [omp_assistant("error", error=AUTH_TEXT)],
    })
    assert replies(lines) == [AUTH_TEXT]
    assert codes(lines) == []


@pytest.mark.parametrize("wording", ["Operation aborted", "Interrupted by user"])
def test_8_an_abort_is_no_card(tmp_path, wording):
    """The model failed a few times, then the turn was aborted: omp's FINAL
    line is the abort — no "model is not answering", and no empty-turn card
    either (an abort is a decision, like Stop)."""
    attempts = [omp_assistant("error", error=UNABLE_TEXT, retry="superseded")
                for _ in range(2)]
    lines, _ = run_turn(tmp_path, {
        "chunks": [], "stopReason": "end_turn",
        "omp": [*attempts, omp_assistant("aborted", error=wording)],
    })
    assert codes(lines) == []
    assert replies(lines) == []


def test_8b_stop_pressed_is_no_card(tmp_path):
    lines, _ = run_turn(tmp_path, {
        "chunks": ["Half an answer"], "stopReason": "cancelled",
        "omp": [omp_assistant("error", error=UNABLE_TEXT, retry="superseded"),
                omp_assistant("aborted", error="Interrupted by user")],
    })
    assert codes(lines) == []


# ── 9. without omp's file: the text rule, with "usage missing" ──────────────


def test_9_without_omps_file_a_turn_without_usage_is_the_card(tmp_path):
    lines, _ = run_turn(tmp_path, {
        "chunks": [UNABLE_TEXT], "stopReason": "end_turn",  # usage missing
    }, omp_file=False)
    assert replies(lines) == []
    assert codes(lines) == ["model_unreachable"]
    assert cards(lines)[0]["data"]["detail"] == UNABLE_TEXT


def test_9b_without_omps_file_a_turn_with_tokens_stays_a_reply(tmp_path):
    """Tokens = the model spoke; its words are never hidden on a guess."""
    lines, _ = run_turn(tmp_path, {
        "chunks": [UNABLE_TEXT], "stopReason": "end_turn", "usage": USAGE,
    }, omp_file=False)
    assert replies(lines) == [UNABLE_TEXT]
    assert codes(lines) == []


def test_9c_without_omps_file_refusal_after_work_keeps_the_reply_and_adds_the_card(tmp_path):
    text = "I read the logs.\n\n" + ECONN_TEXT
    lines, _ = run_turn(tmp_path, {
        "chunks": [text], "stopReason": "refusal", "usage": USAGE,
    }, omp_file=False)
    assert replies(lines) == [text]
    assert codes(lines) == ["model_unreachable"]


def test_9d_without_omps_file_the_word_early_in_a_long_text_is_no_card(tmp_path):
    text = "ECONNREFUSED was in yesterday's log.\n" + ("Analysis. " * 60)
    lines, _ = run_turn(tmp_path, {
        "chunks": [text], "stopReason": "refusal",
    }, omp_file=False)
    assert codes(lines) == []


# ── 10. one source for the pattern ──────────────────────────────────────────


def test_10_one_source_for_the_pattern():
    import bridge  # noqa: PLC0415

    errors = model_errors()
    assert bridge.MODEL_UNREACHABLE_RE is errors.MODEL_UNREACHABLE_RE
    assert errors.UNREACHABLE_TAIL_CHARS == 300
    assert bridge._UNREACHABLE_TAIL_CHARS == errors.UNREACHABLE_TAIL_CHARS
    assert acp_chat.model_errors is errors
    # No second copy anywhere in the bridge code: a word only this pattern
    # has appears in exactly one module.
    holders = sorted(p.name for p in ROOT.glob("*.py")
                     if "ehostunreach" in p.read_text(encoding="utf-8"))
    assert holders == ["model_errors.py"], holders


# ── 11. parity with the task path (#897) ────────────────────────────────────


def _task_outcome(text="", stop="refusal", worked=False, error=None):
    import bridge  # noqa: PLC0415

    o = bridge.RunOutcome()
    o.saw_session = o.saw_agent_start = o.saw_agent_end = error is None
    if error is not None:
        o.saw_session = True
    o.final_stop_reason = stop if error is None else None
    o.final_text = text
    o.model_worked = worked
    o.error_message = error
    return o


def _finish_mentioning_the_word() -> str:
    """#897's template (test_model_wait.py), word for word."""
    import test_ack_on_first_activity as base  # noqa: PLC0415

    return "Fertig." + base._REFLECTION.replace(
        "Das Modell antwortete sofort und sauber.",
        "Der Fehler 'connection refused' im Log war alt und ist behoben.",
    )


_TALKING_END = ("Analysis. " * 40) + "The last request failed with connection refused."

#: #897's templates (tests/test_model_wait.py) with omp's own final line.
#: (name, task text, stop, worked, prompt error, omp line, task verdict, chat verdict)
PARITY = [
    ("econnrefused, refusal", ECONN_TEXT, "refusal", False, None,
     omp_assistant("error", error=ECONN_TEXT), True, True),
    ("retry budget after work, end_turn",
     "Ich pruefe die Dateien.\n\nRetry budget exhausted after 10 retries: " + ECONN_TEXT,
     "end_turn", True, None,
     omp_assistant("error", error="Retry budget exhausted after 10 retries: " + ECONN_TEXT),
     True, True),
    ("model worked, then econnrefused", "Ich lese den Code.\n" + ECONN_TEXT, "refusal",
     True, None, omp_assistant("error", error=ECONN_TEXT), True, True),
    ("unable to connect, end_turn", UNABLE_TEXT, "end_turn", True, None,
     omp_assistant("error", error=UNABLE_TEXT), True, True),
    ("prompt fails: connection error", "", "", False, "Internal error: Connection error.",
     None, True, True),
    ("real finish mentioning the word", _finish_mentioning_the_word(), "end_turn", True,
     None, omp_reply(_finish_mentioning_the_word()), False, False),
    ("the word early in a long turn",
     "Im Log stand ECONNREFUSED von gestern.\n" + ("Analyse. " * 120), "end_turn", True,
     None, omp_reply("Im Log stand ECONNREFUSED von gestern."), False, False),
    ("plain missing sentinel", "Ich habe angefangen.", "end_turn", True, None,
     omp_reply("Ich habe angefangen."), False, False),
    ("an auth error", AUTH_TEXT, "end_turn", False, None,
     omp_assistant("error", error=AUTH_TEXT), False, False),
    # Deliberate difference: the model's OWN answer ends talking about a
    # refused connection. The task path has no omp line to ask and reads
    # the text's end; the chat asks omp's line, which says the model
    # answered — a model's answer is never turned into a card.
    ("DIFFERENT: the model's own answer ends on the word", _TALKING_END, "end_turn", True,
     None, omp_reply(_TALKING_END), True, False),
]


@pytest.mark.parametrize("row", PARITY, ids=[row[0] for row in PARITY])
def test_11_parity_with_the_task_path(row):
    import bridge  # noqa: PLC0415

    name, text, stop, worked, error, omp_line, task_says, chat_says = row
    task = bridge.classify_acp(_task_outcome(text, stop, worked, error)).reason == "model_unreachable"
    verdict = model_errors().classify_chat_turn(
        stop_reason=stop, final_text=text,
        error_text=error or "", usage=USAGE if worked else None,
        omp_line=omp_line["message"] if omp_line else None,
    )
    chat = verdict.kind == model_errors().MODEL_UNREACHABLE
    assert (task, chat) == (task_says, chat_says), name
    if not name.startswith("DIFFERENT"):
        assert task == chat, f"{name}: the chat and the task path disagree"


# ── 13–14. reading omp's file at the right place and time ───────────────────


def test_13_omps_line_written_just_after_the_reply_still_counts(tmp_path):
    """The flush at the turn's end is not proven: the read waits briefly."""
    lines, _ = run_turn(tmp_path, {
        "chunks": [UNABLE_TEXT], "stopReason": "end_turn", "usage": USAGE,
        "omp": [omp_tool_use(), omp_tool_result()],
        "omp_late": [omp_assistant("error", error=UNABLE_TEXT)], "late_s": 0.15,
    })
    assert codes(lines) == ["model_unreachable"]
    assert replies(lines) == []


def test_14_an_earlier_turns_error_line_never_counts(tmp_path):
    """Turn 1 lost the model; turn 2 is a real answer whose final line omp
    has not written (yet). Turn 1's line must not make a second card."""
    lines, _ = run_turns(tmp_path, [
        {"chunks": [ECONN_TEXT], "stopReason": "refusal",
         "omp": [omp_assistant("error", error=ECONN_TEXT)]},
        {"chunks": [SHORT_ANSWER], "stopReason": "end_turn", "usage": USAGE, "omp": []},
    ], texts=["First question.", "Second question."])
    assert codes(lines) == ["model_unreachable"], "one card, for turn 1 only"
    assert replies(lines) == [SHORT_ANSWER]


def test_14b_nothing_of_this_turn_in_omps_file_is_not_the_earlier_turns_end(tmp_path):
    """omp has written nothing of turn 2 yet: what was in the file before
    the turn started is not read at all (the mark), so the earlier error
    line cannot answer for this turn; the text rule decides (tokens: no
    card)."""
    lines, _ = run_turns(tmp_path, [
        {"chunks": [ECONN_TEXT], "stopReason": "refusal",
         "omp": [omp_assistant("error", error=ECONN_TEXT)]},
        {"chunks": [SHORT_ANSWER], "stopReason": "end_turn", "usage": USAGE, "silent": True},
    ], texts=["First question.", "Second question."])
    assert codes(lines) == ["model_unreachable"], "one card, for turn 1 only"
    assert replies(lines) == [SHORT_ANSWER]


# ── 15. who wrote the text (S1-R5 rules for "Send again") ───────────────────


@pytest.mark.parametrize("origin", [None, "task", "nudge", "someone"])
def test_15_a_text_not_from_the_operator_gets_the_card_without_the_text(tmp_path, origin):
    lines, _ = run_turn(tmp_path, {
        "chunks": [ECONN_TEXT], "stopReason": "refusal",
        "omp": [omp_assistant("error", error=ECONN_TEXT)],
    }, origin=origin)
    card = cards(lines, "model_unreachable")[0]
    assert "retryText" not in card["data"]
    assert card["content"] == NEUTRAL_CARD_TEXT


def test_15b_a_text_over_8000_characters_is_never_offered_cut(tmp_path):
    text = "y" * 8001
    lines, _ = run_turns(tmp_path, [{
        "chunks": [ECONN_TEXT], "stopReason": "refusal",
        "omp": [omp_assistant("error", error=ECONN_TEXT)],
    }], texts=[text])
    card = cards(lines, "model_unreachable")[0]
    assert "retryText" not in card["data"]


# ── 16. an open thought is closed before the card (#902) ────────────────────


def test_16_an_open_thought_is_written_before_the_card(tmp_path):
    lines, _ = run_turn(tmp_path, {
        "thoughts": ["Checking whether the server is up."], "chunks": [],
        "stopReason": "end_turn",
        "omp": [omp_assistant("error", error=UNABLE_TEXT)],
    })
    kinds = []
    for line in lines:
        msg = line.get("message") or {}
        blocks = msg.get("content") or []
        if any(b.get("type") == "thinking" for b in blocks if isinstance(b, dict)):
            kinds.append("thinking")
        elif line.get("customType") == acp_chat_events.CHAT_ERROR_CUSTOM_TYPE:
            kinds.append(line["data"]["code"])
    assert kinds == ["thinking", "model_unreachable"]


# ── the parser of omp's lines ───────────────────────────────────────────────


def _raw(*entries) -> list[str]:
    return [json.dumps(e) for e in entries]


def test_17_the_final_line_is_the_last_assistant_line_that_ends_the_turn():
    errors = model_errors()
    final = errors.final_assistant_message(_raw(
        omp_user("q"), omp_assistant("error", error=UNABLE_TEXT, retry="superseded"),
        omp_assistant("error", error=BUDGET_TEXT)))
    assert final is not None and final["errorMessage"] == BUDGET_TEXT


@pytest.mark.parametrize("last", [
    omp_tool_use(),
    omp_assistant("error", error=UNABLE_TEXT, retry="superseded"),
    omp_assistant("error", error=UNABLE_TEXT, retry="recovered"),
    None,
], ids=["tool-use", "superseded", "recovered", "no-assistant-line"])
def test_17b_a_turn_that_has_not_ended_in_the_file_has_no_final_line(last):
    entries = [omp_user("q"), omp_tool_result()] + ([last] if last else [])
    assert model_errors().final_assistant_message(_raw(*entries)) is None


def test_17d_an_earlier_turns_final_line_before_this_turns_user_line_never_counts():
    """The mark is taken when the job is claimed; a final line of the turn
    before that omp wrote late can sit after it — this turn's user line
    follows it, so it is not this turn's end."""
    raw = _raw(omp_assistant("error", error=ECONN_TEXT), omp_user("the next question"))
    assert model_errors().final_assistant_message(raw) is None


def test_17c_broken_and_half_written_lines_are_skipped():
    raw = _raw(omp_user("q"), omp_assistant("error", error=ECONN_TEXT))
    raw.insert(1, "{not json")
    raw.append('{"type": "message", "message": {"role": "assis')
    final = model_errors().final_assistant_message(raw)
    assert final is not None and final["errorMessage"] == ECONN_TEXT


def test_18_model_errors_is_standard_library_only():
    """The host's Python 3.9 loads it with acp_chat.py (Hermes)."""
    tree = ast.parse((ROOT / "model_errors.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "json", "re", "typing", "dataclasses"}, imported
