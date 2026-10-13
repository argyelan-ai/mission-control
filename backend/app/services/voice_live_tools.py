"""Live voice in the chat — the voice's two hands (voice PR 2, ADR-091 draft).

The realtime model runs in OpenAI's cloud and talks to the browser over a
WebRTC data channel. When it calls a tool, the browser posts the call here
and hands the answer back as ``function_call_output``. Nothing about an
agent (token, pane, transcript) ever travels to the browser for this; the
browser only carries the operator's own words and our short answers.

Two tools, both bound to ONE call (``call_ref`` from ``POST /session``):

``send_to_agent(text)``
    Types a chat message into THIS chat's agent through the chat's own input
    path (``agent_chat_input.send_text``) — the same path the composer uses,
    with all its guards. Headless (ACP) agents get it with ``mode="queue"``
    (``send_text`` always does that): a running turn keeps running and the
    message follows it. A TUI agent gets it typed into its input line, which
    the CLI holds as a steer. Never a cancel, never a key press.

    The message ALWAYS carries the operator's verbatim transcript next to the
    voice's wording (incident 12.09.2026: a voice invented the task text it
    passed on). No verbatim words -> nothing is sent.

``get_agent_status()``
    A short, redacted snapshot (state, current todo step, last tool title)
    read from MC's own state. Read-only: the agent is not asked and not
    disturbed. Tool OUTPUTS are never part of it.

Guards (pinned by ``tests/test_voice_live_tools.py``): call must be known,
unexpired and opened for this very agent; per-call rate limits; length caps;
an identical send within ``DUPLICATE_WINDOW_S`` is dropped (the model
sometimes repeats a call).
"""

from __future__ import annotations

import re
import secrets
import time
import unicodedata
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from app.log_redaction import redact_secrets

#: Marker the chat UI keys on (``frontend-v2/.../voiceMessage.ts``) to show the
#: small "per Stimme"/"by voice" label. Protocol, not UI copy: it stays the
#: same in both languages so the transcript is parseable either way.
VOICE_PREFIX = "[per Stimme]"
VERBATIM_LABEL = "Wortlaut:"
#: Last line of every voice message: tells the AGENT how to read it. The UI
#: hides this line (it is for the agent, not the operator).
VOICE_NOTE = (
    "(Voice transcript: the operator is speaking to you through MC's voice; it "
    "may contain mis-hearings. The Wortlaut line is what the operator actually said. "
    "Phrases like \"pass on to <you>\" / \"gib an <dich> weiter\" / \"sag <dir>\" mean "
    "this request is for you: do it yourself, never forward it to another agent "
    "unless the operator names a different one. Keep the reply short; report "
    "success only when it is real.)"
)

MAX_TEXT_CHARS = 1000
MAX_TRANSCRIPT_CHARS = 4000

#: A call lives at most 20 min (client cap, bauplan B6) — plus slack for a
#: slow last tool call. After that its ``call_ref`` is worthless.
CALL_TTL_S = 21 * 60
DUPLICATE_WINDOW_S = 10.0
RATE_WINDOW_S = 60.0
#: Per call and minute. A spoken conversation passes on a handful of
#: requests a minute at most; more is a loop in the model.
RATE_LIMITS = {"send_to_agent": 6, "get_agent_status": 20}

TOOL_NAMES = ("send_to_agent", "get_agent_status")


class VoiceToolError(Exception):
    def __init__(self, reason: str, status: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


@dataclass
class _Call:
    agent_id: uuid.UUID
    expires_at: float
    hits: dict[str, deque] = field(default_factory=dict)
    last_send: tuple[str, float] | None = None


# In-process: the backend runs one worker (see agent_chat_input._LAST_SENT).
# A backend restart ends every call's tools — the browser then says "start
# the call again", which is honest and cheap.
_CALLS: dict[str, _Call] = {}


def _now() -> float:
    return time.monotonic()


def open_call(agent_id: uuid.UUID) -> str:
    """Registers a new call for ``agent_id`` and returns its reference."""
    now = _now()
    for ref in [r for r, c in _CALLS.items() if c.expires_at <= now]:
        _CALLS.pop(ref, None)
    ref = secrets.token_urlsafe(24)
    _CALLS[ref] = _Call(agent_id=agent_id, expires_at=now + CALL_TTL_S)
    return ref


def check_call(ref: str, agent_id: uuid.UUID) -> _Call:
    call = _CALLS.get(ref or "")
    if call is None or call.expires_at <= _now():
        raise VoiceToolError("call_unknown", 403)
    if call.agent_id != agent_id:
        # A call opened for agent A must never reach agent B.
        raise VoiceToolError("wrong_agent", 403)
    return call


def take_rate(call: _Call, tool: str) -> None:
    now = _now()
    hits = call.hits.setdefault(tool, deque())
    while hits and now - hits[0] > RATE_WINDOW_S:
        hits.popleft()
    if len(hits) >= RATE_LIMITS[tool]:
        raise VoiceToolError("rate_limited", 429)
    hits.append(now)


def _norm(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def is_duplicate(call: _Call, text: str) -> bool:
    last = call.last_send
    return last is not None and last[0] == _norm(text) and _now() - last[1] < DUPLICATE_WINDOW_S


def remember_send(call: _Call, text: str) -> None:
    call.last_send = (_norm(text), _now())


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def clean_line(text: str) -> str:
    """One line, no control characters: the message format is line-based and
    a NUL would break delivery deep in tmux."""
    return " ".join(_CONTROL.sub(" ", text or "").split())


#: "Gib bitte an Sparky weiter: …", "Sag Boss, …", "Tell Sparky: …" — the
#: message already goes to this chat's agent, so a leading forwarding phrase
#: only confuses it (live 13.10.2026: a misheard name made the agent try to
#: forward to an unknown agent). Names are capitalised; "sag mir …" /
#: "tell me …" never match. Only line 1 is cleaned; the Wortlaut stays as said.
_FORWARD_PHRASES = (
    re.compile(
        r"^\s*(?i:bitte\s+)?(?i:gib|sag|richte|teile?|leite)\s+(?i:(?:es|das|bitte|doch)\s+)*"
        r"(?i:an\s+|dem\s+|der\s+)?[A-ZÄÖÜ][\w-]{1,20}(?i:\s+(?:bitte|doch))*"
        r"(?i:\s+(?:weiter|aus|mit))?\s*[:,]\s*"
    ),
    re.compile(
        r"^\s*(?i:please\s+)?(?i:tell|ask|pass\s+(?:this\s+|it\s+)?on\s+to)\s+"
        r"[A-Z][\w-]{1,20}\s*[:,]\s*"
    ),
)


def strip_forwarding(text: str) -> str:
    for pattern in _FORWARD_PHRASES:
        stripped = pattern.sub("", text, count=1)
        if stripped != text and stripped.strip():
            return stripped.strip()
    return text


def compose_message(text: str, verbatim: str) -> str:
    """The chat message the agent receives. Line 1 = the voice's wording with
    the marker (a leading "pass on to <agent>" phrase removed), line 2 = the
    operator's own words (left out only when they are the same text), line 3 =
    the note for the agent."""
    said = strip_forwarding(clean_line(text))
    words = clean_line(verbatim)
    lines = [f"{VOICE_PREFIX} {said}"]
    if _norm(said) != _norm(words):
        lines.append(f"{VERBATIM_LABEL} „{words}“")
    lines.append(VOICE_NOTE)
    return "\n".join(lines)


# ── status snapshot ──────────────────────────────────────────────────────

_EXTRA_SECRETS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(sk|ek|rk|pk)[-_][A-Za-z0-9_-]{8,}"), "[secret]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{12,}"), "[secret]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{8,}"), "[secret]"),
    (re.compile(r"\b[A-Z][A-Z0-9_]{2,}=\S+"), lambda m: m.group(0).split("=", 1)[0] + "=…"),  # type: ignore[list-item]
    (re.compile(r"\b[A-Fa-f0-9]{32,}\b"), "[secret]"),
    (re.compile(r"\b[A-Za-z0-9+_-]{40,}={0,2}"), "[secret]"),
)
# A path: at least one slash-separated segment. Only the last segment is kept
# (bauplan B4) — directory names can carry customer or private names.
_PATH = re.compile(r"(?:~|\.{1,2}|[\w.-]+)?(?:/[^\s/\"'`]+)+/?")

STATUS_FIELD_MAX = 60


def redact_short(text: str, limit: int = STATUS_FIELD_MAX) -> str:
    """Redacts a short status string: secrets out, paths cut to their last
    segment, one line, capped."""
    out = redact_secrets(clean_line(text))

    def _last_segment(m: re.Match[str]) -> str:
        parts = [p for p in m.group(0).split("/") if p and p not in ("~", ".", "..")]
        return parts[-1] if parts else m.group(0)

    # Paths first (their last segment survives), then the secret shapes, so
    # a token sitting in a file name is still caught.
    out = _PATH.sub(_last_segment, out)
    for pattern, repl in _EXTRA_SECRETS:
        out = pattern.sub(repl, out)  # type: ignore[arg-type]
    if len(out) > limit:
        out = out[: limit - 1].rstrip() + "…"
    return out


_STATE_WORD = {
    "working": "working",
    "idle": "idle",
    "permission_prompt": "waiting_for_approval",
    "waiting_input": "waiting_for_answer",
}


def build_status(
    state: dict[str, Any] | None,
    todo: dict[str, Any] | None,
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    """The snapshot the voice reads out. Pure: the router gathers the
    inputs, this decides what may leave MC."""
    raw = (state or {}).get("status")
    aliveness = (state or {}).get("aliveness")
    word = _STATE_WORD.get(raw or "", "unknown")
    if aliveness == "ended" and word in ("idle", "unknown"):
        word = "offline"

    step = None
    items = [i for i in (todo or {}).get("items") or [] if isinstance(i, dict)]
    if items:
        done = sum(1 for i in items if i.get("status") == "completed")
        current = next((i for i in items if i.get("status") == "in_progress"), None)
        if current is not None:
            pos = items.index(current) + 1
            step = f"{pos}/{len(items)}: {redact_short(str(current.get('content') or ''))}"
        else:
            step = f"{done}/{len(items)} done"

    last_tool = None
    for ev in reversed(events):
        if ev.get("kind") == "tool" and not ev.get("sidechain"):
            name = clean_line(str(ev.get("name") or ""))[:30]
            title = str(ev.get("title") or "")
            # title only, never ``result``/``detail`` (tool output, file content)
            last_tool = redact_short(f"{name} · {title}" if title and title != name else name)
            break

    out: dict[str, Any] = {"state": word, "step": step, "last_tool": last_tool}
    if word == "waiting_for_approval":
        out["note"] = "He waits for an approval. Ask the operator to tap it in the chat; a spoken yes does not count."
    elif word == "working":
        out["note"] = "Still working. Nothing is done until he reports it."
    else:
        out["note"] = "Snapshot only. Do not add anything that is not here."
    return out


def reset_for_tests() -> None:
    _CALLS.clear()
