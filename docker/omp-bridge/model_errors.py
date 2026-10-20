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
  After retries the file's final line is ``Retry budget exhausted after N
  retries: <line>``, while the ACP text carries only ``<line>`` — the
  retried attempts' message — sometimes glued onto the model's last
  sentence (live: 13 of 13 MC copies). The chat cuts exactly that line off
  the END of the reply; the model's own words stay.

Both read ``MODEL_UNREACHABLE_RE`` and ``UNREACHABLE_TAIL_CHARS`` from here —
one source. The chat alone adds ``MODEL_STOPPED_RE`` (the engine stopped
mid-request), and only on omp's own error line. Standard library only, Python 3.9+: the host's Hermes chat
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

#: The engine took the request but stopped answering: omp's words when the
#: stream never started, stalled, or its socket closed (live: 19 final
#: error lines, all with 0 output tokens). The CHAT's addition, read ONLY
#: from omp's own ``error`` line — in a model's text "timed out" is far too
#: common for the text rule, and the task path keeps #897's pattern and
#: its own handling of these (a transient error, the completion contract).
MODEL_STOPPED_RE = re.compile(
    r"stream timed out|stream stalled|socket connection was closed",
    re.IGNORECASE,
)

#: How much of the turn's END counts: omp appends its error as the turn's
#: last text; a model that merely talks about a refused connection earlier
#: in a long answer is not a lost model.
UNREACHABLE_TAIL_CHARS = 300

#: The card's ``detail``: omp's line, at most this long.
DETAIL_CHARS = 240

#: omp's wrapper around the attempt's line once it gave up retrying (live
#: 20.10.2026, omp 18.10.1).
_RETRY_BUDGET_RE = re.compile(r"^\s*retry budget exhausted after \d+ retr(?:y|ies)\s*:\s*",
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


def _cut_end(text: str, line: str) -> Optional[str]:
    """``text`` without ``line`` at its very END (whitespace does not count:
    omp's line may follow the model's words with or without a break), or
    None when the text does not end with it."""
    want = "".join(line.split())
    if not want:
        return None
    start, found = len(text), 0
    while start > 0 and found < len(want):
        start -= 1
        if not text[start].isspace():
            found += 1
    if found < len(want) or "".join(text[start:].split()) != want:
        return None
    return text[:start].rstrip()


def reply_without_omp_line(text: str, omp_lines: Iterable[str]) -> str:
    """The reply text with omp's error line cut off its END — "" when the
    text was nothing else. ``omp_lines``: the forms omp's line can take in
    the ACP text (``omp_line_forms``). Only the very end is cut, as often as
    a form is still there; the same words earlier in the model's answer
    stay."""
    forms = sorted({f for f in omp_lines if isinstance(f, str) and f.strip()},
                   key=len, reverse=True)
    out = text
    while out:
        for form in forms:
            cut = _cut_end(out, form)
            if cut is not None:
                out = cut
                break
        else:
            break
    return out


def omp_line_forms(error_message: str, retried: Iterable[str] = ()) -> tuple:
    """How omp's error line of a turn can appear in the ACP text: its final
    ``errorMessage``, the same without the "Retry budget exhausted after N
    retries:" wrapper (live: the ACP text carries the attempt's line), and
    the lines of the attempts it retried in this turn (any other wrapper)."""
    forms = [error_message, _RETRY_BUDGET_RE.sub("", error_message)]
    forms.extend(r for r in retried if isinstance(r, str))
    return tuple(forms)


@dataclass(frozen=True)
class OmpTurnEnd:
    """The end of a turn in omp's own session file: the ``message`` of its
    final line, and the ``errorMessage`` of each attempt omp retried in
    this turn (oldest first)."""

    message: dict
    retried: tuple = ()


def omp_turn_end(lines: Iterable[str]) -> Optional[OmpTurnEnd]:
    """The assistant line that ENDS the turn in omp's session file lines,
    or None when the turn has not ended there (yet).

    ``lines`` are the lines omp wrote since the turn started. Only the LAST
    message line counts, and only when it is an assistant line that ends a
    turn: not a user line (the turn's own lines are not there yet — an
    earlier turn's last line never counts), not a tool call or its result
    (``toolUse`` / ``toolResult`` — the next answer is still to come), not
    an attempt omp retried (``retryRecovery.status`` ``superseded`` /
    ``recovered``). The retried attempts' messages count from the turn's
    last user line on. Other line types and broken or half-written lines
    are skipped."""
    last: Optional[dict] = None
    retried: list = []
    for raw in lines:
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "message":
            continue
        message = entry.get("message")
        last = message if isinstance(message, dict) else None
        if last is None:
            continue
        if last.get("role") == "user":
            retried = []
        elif _was_retried(last) and isinstance(last.get("errorMessage"), str):
            retried.append(last["errorMessage"])
    if last is None or last.get("role") != "assistant" or last.get("stopReason") == "toolUse":
        return None
    if _was_retried(last):
        return None
    return OmpTurnEnd(last, tuple(retried))


def final_assistant_message(lines: Iterable[str]) -> Optional[dict]:
    """The ``message`` of ``omp_turn_end``, or None."""
    end = omp_turn_end(lines)
    return end.message if end is not None else None


def _was_retried(message: dict) -> bool:
    recovery = message.get("retryRecovery")
    return isinstance(recovery, dict) and recovery.get("status") in _RETRIED


@dataclass(frozen=True)
class ChatTurnVerdict:
    """What the chat adds to a turn that ended.

    ``kind``: MODEL_UNREACHABLE (the card), ABORTED (no card at all, like
    Stop) or None (as before S1-R6). ``detail``: omp's line for the card.
    ``reply``: the text the reply bubble shows instead of the turn's text —
    omp's error line cut off its end; "" = no bubble; None = unchanged."""

    kind: Optional[str] = None
    detail: str = ""
    reply: Optional[str] = None

    @property
    def hide_reply(self) -> bool:
        """No reply bubble: the text was omp's error line and nothing else."""
        return self.reply is not None and not self.reply.strip()


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
    return (not text.strip() or bool(MODEL_UNREACHABLE_RE.search(text))
            or bool(MODEL_STOPPED_RE.search(text)))


def _omp_error_line(message: str) -> Optional[str]:
    """omp's own ``errorMessage`` on one line, at most DETAIL_CHARS, when it
    says the model is not answering — unreachable, or the engine stopped
    mid-request (only here, on omp's own line) — else None."""
    if not MODEL_UNREACHABLE_RE.search(message) and not MODEL_STOPPED_RE.search(message):
        return None
    return _one_line(message)[:DETAIL_CHARS]


def classify_chat_turn(*, stop_reason: str, final_text: str, error_text: str = "",
                       usage: Any = None, omp_line: Optional[dict] = None,
                       omp_retried: Iterable[str] = ()) -> ChatTurnVerdict:
    """The chat's rule (S1-R6), in this order:

    1. The prompt failed: a matching error is MODEL_UNREACHABLE (never
       ``rpc_error``); any other failure is not decided here.
    2. Stop pressed (``cancelled``): nothing.
    3. omp's final line of the turn (``omp_line``, its ``message``) decides
       when it is there: ``aborted`` -> ABORTED; ``error`` whose
       ``errorMessage`` matches MODEL_UNREACHABLE_RE or MODEL_STOPPED_RE ->
       MODEL_UNREACHABLE, with omp's line cut off the END of the reply (in
       any of its forms, ``omp_line_forms`` with ``omp_retried``: the
       attempts' lines of this turn); anything else (a real answer, 401,
       429, an ``error`` line that merely says "aborted") -> nothing.
    4. Fallback, only without omp's line (file missing, unreadable, not
       written in time): the END of the text matches MODEL_UNREACHABLE_RE
       AND (the stop reason is ``refusal`` — omp's error path — OR the reply
       has no ``usage`` at all — omp sends none for a turn without tokens).
       The reply is hidden only without ``usage`` (tokens mean the model
       spoke) and only when the card's ``detail`` holds all of it.
    """
    if error_text:
        line = unreachable_line(error_text)
        return ChatTurnVerdict(MODEL_UNREACHABLE, line) if line is not None else NOTHING
    if stop_reason == "cancelled":
        return NOTHING
    text = final_text or ""
    if omp_line is not None:
        stop = omp_line.get("stopReason")
        if stop == "aborted":
            return ChatTurnVerdict(ABORTED)
        message = omp_line.get("errorMessage")
        message = message if isinstance(message, str) else ""
        line = _omp_error_line(message) if stop == "error" else None
        if line is None:
            return NOTHING
        reply = reply_without_omp_line(text, omp_line_forms(message, omp_retried))
        return ChatTurnVerdict(MODEL_UNREACHABLE, line,
                               reply=None if reply == text else reply)
    line = unreachable_line(text[-UNREACHABLE_TAIL_CHARS:], keep_end=True)
    if line is None:
        return NOTHING
    no_usage = not (isinstance(usage, dict) and usage)
    if stop_reason == "refusal" or no_usage:
        # Hidden only when the card's detail is the whole text: no word of
        # a reply the rule misread is ever lost.
        whole = no_usage and len(_one_line(text)) <= DETAIL_CHARS
        return ChatTurnVerdict(MODEL_UNREACHABLE, line, reply="" if whole else None)
    return NOTHING
