#!/usr/bin/env python3
"""model_errors.py — "the model is not answering", one rule for task and chat.

omp (18.10.1) never ends a turn with the ACP stop reason ``error`` when the
model's endpoint is gone. It retries, gives up and sends its error line as
ordinary TEXT; the stop reason is ``refusal`` (omp's pattern
``refus(al|ed)`` matches "ECONNREFUSED") or ``end_turn``. A prompt can also
fail as a whole ("Internal error: Connection error.").

* The task path (``bridge.classify_acp``, #897) reads the END of the turn's
  text and the prompt error, guarded by the completion sentinel.
* The chat (``acp_chat``, S1-R6) has no sentinel. It asks omp's OWN session
  file instead: omp records every failed turn there as an assistant line
  with ``stopReason: "error"`` and ``errorMessage`` (live 20.10.2026), while
  MC's display copy shows that text as an ordinary answer. Only when that
  file gives no answer does the chat fall back to the text rule.

Both read ``MODEL_UNREACHABLE_RE`` and ``UNREACHABLE_TAIL_CHARS`` from here —
one source. Standard library only, Python 3.9+: the host's Hermes chat
daemon imports this through ``acp_chat.py`` with the system Python.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

#: The model's endpoint does not answer at all — omp's wording once it gave
#: up retrying (live 19.10.2026: ``ECONNREFUSED: Unable to connect. Is the
#: computer able to access the url?``, ACP: ``Internal error`` /
#: ``Connection error.``). Narrower than TRANSIENT_ERROR_RE: no "network",
#: no bare 5xx — an overloaded server DID answer.
MODEL_UNREACHABLE_RE = re.compile(
    r"econnrefused|connection refused|unable to connect|enotfound|ehostunreach|"
    r"enetunreach|etimedout|fetch failed|connection error|socket hang ?up|"
    r"bad gateway|service unavailable|gateway time-?out",
    re.IGNORECASE,
)

#: How much of the turn's END counts: omp appends its error as the turn's
#: last text; a model that merely talks about a refused connection earlier
#: in a long answer is not a lost model.
UNREACHABLE_TAIL_CHARS = 300

#: The card's ``detail``: omp's line, at most this long.
DETAIL_CHARS = 240

#: omp's words for a turn that was stopped, not failed (live 20.10.2026:
#: ``stopReason: "aborted"`` with one of these as ``errorMessage``).
ABORT_RE = re.compile(r"interrupted by user|operation aborted|request was aborted",
                      re.IGNORECASE)

#: ``ChatTurnVerdict.kind``: the chat card's code, and "the turn was aborted".
MODEL_UNREACHABLE = "model_unreachable"
ABORTED = "aborted"

#: ``retryRecovery.status`` of an attempt omp retried: not the turn's end.
_RETRIED = ("superseded", "recovered")


def unreachable_line(text: Any, *, keep_end: bool = False) -> Optional[str]:
    """``text`` on one line, at most DETAIL_CHARS, when it says the model's
    endpoint does not answer — else None. An error message keeps its START
    (where omp names the cause); the end of a turn's text keeps its END
    (``keep_end``: omp appended its line there)."""
    if not isinstance(text, str) or not MODEL_UNREACHABLE_RE.search(text):
        return None
    line = " ".join(text.split())
    return line[-DETAIL_CHARS:] if keep_end else line[:DETAIL_CHARS]


def _one_line(text: str) -> str:
    return " ".join(text.split())


def final_assistant_message(lines: Iterable[str]) -> Optional[dict]:
    """The ``message`` of the assistant line that ENDS the turn in omp's
    session file lines, or None when the turn has not ended there (yet).

    ``lines`` are the lines omp wrote since the turn started. Only the LAST
    message line counts, and only when it is an assistant line that ends a
    turn: not a user line (the turn's own lines are not there yet — an
    earlier turn's last line never counts), not a tool call or its result
    (``toolUse`` / ``toolResult`` — the next answer is still to come), not
    an attempt omp retried (``retryRecovery.status`` ``superseded`` /
    ``recovered``). Other line types and broken or half-written lines are
    skipped."""
    last: Optional[dict] = None
    for raw in lines:
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "message":
            continue
        message = entry.get("message")
        last = message if isinstance(message, dict) else None
    if last is None or last.get("role") != "assistant" or last.get("stopReason") == "toolUse":
        return None
    recovery = last.get("retryRecovery")
    if isinstance(recovery, dict) and recovery.get("status") in _RETRIED:
        return None
    return last


@dataclass(frozen=True)
class ChatTurnVerdict:
    """What the chat adds to a turn that ended.

    ``kind``: MODEL_UNREACHABLE (the card), ABORTED (no card at all, like
    Stop) or None (as before S1-R6). ``detail``: omp's line for the card.
    ``hide_reply``: the reply text is omp's error line, not the model's —
    no reply bubble."""

    kind: Optional[str] = None
    detail: str = ""
    hide_reply: bool = False


NOTHING = ChatTurnVerdict()


def needs_omp_line(stop_reason: str, final_text: str, error_text: str) -> bool:
    """Whether omp's session file has to be asked about this turn: only a
    turn whose text names a lost model somewhere (omp sends its error line
    as the turn's text) or that has no text at all — an ordinary answer
    never waits for the file. A prompt error decides on its own; a stopped
    turn is the operator's decision."""
    if error_text or stop_reason == "cancelled":
        return False
    text = final_text or ""
    return not text.strip() or bool(MODEL_UNREACHABLE_RE.search(text))


def classify_chat_turn(*, stop_reason: str, final_text: str, error_text: str = "",
                       usage: Any = None, omp_line: Optional[dict] = None) -> ChatTurnVerdict:
    """The chat's rule (S1-R6), in this order:

    1. The prompt failed: a matching error is MODEL_UNREACHABLE (never
       ``rpc_error``); any other failure is not decided here.
    2. Stop pressed (``cancelled``): nothing.
    3. omp's final line of the turn (``omp_line``, its ``message``) decides
       when it is there: ``aborted`` or the abort wording -> ABORTED;
       ``error`` with a matching ``errorMessage`` -> MODEL_UNREACHABLE, the
       reply hidden when its text IS that line; anything else (a real
       answer, 401, 429, ...) -> nothing.
    4. Fallback, only without omp's line (file missing, unreadable, not
       written in time): the END of the text matches AND (the stop reason is
       ``refusal`` — omp's error path — OR the reply has no ``usage`` at
       all — omp sends none for a turn without tokens). The reply is hidden
       only without ``usage``: tokens mean the model spoke.
    """
    if error_text:
        line = unreachable_line(error_text)
        return ChatTurnVerdict(MODEL_UNREACHABLE, line) if line is not None else NOTHING
    if stop_reason == "cancelled":
        return NOTHING
    text = final_text or ""
    if omp_line is not None:
        stop = omp_line.get("stopReason")
        message = omp_line.get("errorMessage")
        message = message if isinstance(message, str) else ""
        if stop == "aborted" or ABORT_RE.search(message):
            return ChatTurnVerdict(ABORTED)
        if stop != "error":
            return NOTHING
        line = unreachable_line(message)
        if line is None:
            return NOTHING
        return ChatTurnVerdict(MODEL_UNREACHABLE, line,
                               hide_reply=_one_line(text) == _one_line(message))
    line = unreachable_line(text[-UNREACHABLE_TAIL_CHARS:], keep_end=True)
    if line is None:
        return NOTHING
    no_usage = not (isinstance(usage, dict) and usage)
    if stop_reason == "refusal" or no_usage:
        return ChatTurnVerdict(MODEL_UNREACHABLE, line, hide_reply=no_usage)
    return NOTHING
