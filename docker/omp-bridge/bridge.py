#!/usr/bin/env python3
"""
omp-bridge — omp event-stream → MC agent-lifecycle reducer (PROTOTYPE).

This module reads an ``omp -p --mode json`` NDJSON lifecycle stream (from a file
or stdin), reduces it to a single *run outcome*, classifies that outcome, and
maps it to an MC agent-lifecycle action (ack / finish / review / blocker).

It CLOSES the silent-abort gap that sank the tmux screen-scrape harness:
a turn (or the whole run) can end without the task actually being complete, and
today nobody PATCHes a terminal status -> the task hangs `in_progress` forever.
Here, EVERY run resolves into exactly one of {finish, blocker} — there is no
code path that ends a run and leaves the task `in_progress`.

Ground truth for the parser (verified against captured streams in ../rpc/):
  - The outcome of a run is legible from the STREAM, never from the exit code:
    clean finish, model error, and --max-time cutoff all exit 0.
  - Terminal marker of a normal run = exactly one `{"type":"agent_end"}` line.
  - The completion oracle = the FINAL `turn_end` message.stopReason.
  - `message_update` is 88% of lines (pure token deltas) and is DROPPED.

PROTOTYPE SCOPE / STUBS (see README):
  - The MCLifecycle hooks (ack/finish/set_blocker/comment) only LOG the intended
    MC call. They do NOT hit the real backend. Swap `LoggingLifecycle` for a
    real `mc`-CLI-backed implementation in Phase 2.
  - The wall-clock / no-progress watchdog is implemented for the live subprocess
    path (`run_omp_subprocess`) but replay tests exercise the pure reducer.
  - Live Claude sentinel reliability is UNVERIFIED (Qwen-only fixtures) — a hard
    Phase-2 gate (see docs/omp-bridge-design.md §7 #8).
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable, Iterator, Optional, TextIO

# ---------------------------------------------------------------------------
# Completion contract (mirrors docs/omp-bridge-design.md §3.4)
# ---------------------------------------------------------------------------

SENTINEL = "TASK_COMPLETE"

# The exact 4 German headers `mc finish` hard-requires (mc_cli _validate_reflection).
REFLECTION_HEADERS = (
    "## Was wurde gemacht",
    "## Was hat funktioniert",
    "## Was war unklar",
    "## Lesson fuer Agent-Memory",
)
MIN_REFLECTION_CHARS = 80

# ===========================================================================
# MIRRORED NORMALIZER — keep in LOCKSTEP with scripts/mc-cli/mc_cli/reflection.py
# ===========================================================================
# The forgiving close-contract logic (sentinel + header tolerance +
# normalization) has ONE canonical source: mc_cli/reflection.py. bridge.py runs
# from /opt/omp-bridge while the mc CLI lives under /home/agent/.mc-cli in the
# omp image — they are NOT on the same sys.path, so bridge.py cannot cleanly
# `import mc_cli`. We therefore DUPLICATE the small normalizer here, and a
# repo-level parity test (tests/test_close_parity.py) feeds a shared case matrix
# through BOTH copies and asserts they agree byte-for-byte. If you touch either
# copy, update the other and keep that test green — any drift fails loudly.
#
# INVARIANT: every tolerance is ADDITIVE — strict canonical input (exact 4
# German `## ` headers, `TASK_COMPLETE` alone as the last line, >=80 chars)
# parses byte-identical to the pre-tolerance behaviour. The backend gate only
# checks reflection existence + length (NOT headers), so nothing accepted here
# could be rejected downstream.

# Canonical German field labels (without the `## ` prefix).
_CANON_FIELDS = tuple(h[len("## "):] for h in REFLECTION_HEADERS)

_ENGLISH_ALIASES = {
    "what was done": "Was wurde gemacht",
    "what worked": "Was hat funktioniert",
    "what was unclear": "Was war unklar",
    "lesson for agent memory": "Lesson fuer Agent-Memory",
}

_HEADER_RE = re.compile(r"^\s{0,3}#{1,3}\s+(.+?)\s*$")
_TRAILER_RE = re.compile(r"^[-=*_`]{3,}$")


def _fold(s: str) -> str:
    """Normalize a header label for tolerant matching (see reflection.py)."""
    s = s.strip().rstrip(":").strip().lower()
    for a, b in (("ü", "ue"), ("ö", "oe"), ("ä", "ae"), ("ß", "ss")):
        s = s.replace(a, b)
    s = s.replace("-", " ")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


_CANON_BY_FOLD = {}
for _f in _CANON_FIELDS:
    _CANON_BY_FOLD[_fold(_f)] = _f
for _k, _v in _ENGLISH_ALIASES.items():
    _CANON_BY_FOLD[_fold(_k)] = _v


def _match_header(line: str) -> Optional[str]:
    """Canonical German field if `line` is a recognised header, else None."""
    m = _HEADER_RE.match(line)
    if not m:
        return None
    return _CANON_BY_FOLD.get(_fold(m.group(1)))


def _is_sentinel_line(line: str) -> bool:
    """True if `line` is a TASK_COMPLETE sentinel, tolerating case, wrapping
    markdown (`**`/backticks/`__`) and trailing punctuation."""
    s = line.strip()
    if not s:
        return False
    s = s.strip("*`_~ ")
    s = re.sub(r"[.\!:;,\s]+$", "", s)
    return s.upper() == SENTINEL

# Heuristic — the ORIGINAL openclaude failure (transient mid-run network abort).
# The exact shape omp+Claude produces is UNVERIFIED on disk (design §2.1); we
# detect it as a retryable sub-class of the abort family, never assert it as the
# only shape.
TRANSIENT_ERROR_RE = re.compile(
    r"fetch failed|connection error|econnreset|socket hang ?up|network|"
    r"etimedout|timed out|\b5\d\d\b|overloaded|upstream|"
    # omp's own wording when the OpenAI-compatible endpoint is unreachable
    # (verified: a dead/cold Qwen vLLM yields exactly this) — the prime
    # transient case (Qwen briefly down / still warming), so retry it.
    r"unable to connect|econnrefused|connection refused",
    re.IGNORECASE,
)

# Streaming-delta wrapper — 509/578 lines in the real sample. Never lifecycle.
_DROP_TYPES = frozenset({"message_update"})

# Default bounded-retry budget for the abort class (design §3.3, "OMP_MAX_RETRIES
# (e.g. 2)"). The driver decrements this per re-spawn and, once exhausted, ALWAYS
# falls through to a terminal `mc blocked` — never a dangling non-terminal state.
OMP_MAX_RETRIES = 2

# Continue-nudge budget (Fix B, design §4). SEPARATE from OMP_MAX_RETRIES: the
# harmless self-completion aborts (turn ended without / with a malformed sentinel,
# or a trailing tool error) are nudged forward IN THE SAME live session instead of
# escalated. Own counter so a run can burn its crash-retries AND its continues
# independently. Exhausted -> terminal blocker (routed via Fix A to the Lead first).
OMP_MAX_CONTINUES = 2


# ---------------------------------------------------------------------------
# Run outcome + classification
# ---------------------------------------------------------------------------


class Kind(str, Enum):
    """Terminal classification of a single omp run."""

    FINISH = "finish"                          # genuine, contract-satisfying finish
    SILENT_ABORT_NO_SENTINEL = "silent_abort_no_sentinel"  # stop, but no sentinel
    MALFORMED_REFLECTION = "malformed_reflection"          # sentinel, bad reflection
    TRAILING_TOOL_ERROR = "trailing_tool_error"            # finish text but tool failed
    ABORT_TRANSIENT_API = "abort_transient_api"            # the openclaude bug (retryable)
    ABORT_ERROR = "abort_error"                # stopReason==error, non-transient
    ABORT_MAXTIME = "abort_maxtime"            # --max-time cut a tool mid-flight
    ABORT_CRASH = "abort_crash"                # no agent_end at process exit
    ABORT_HANG = "abort_hang"                  # watchdog no-progress / wall-clock kill
    ABORT_UNKNOWN = "abort_unknown"            # any other non-stop terminal stopReason
    LAUNCH_PREFLIGHT = "launch_preflight"      # exit 1/2, no json session emitted


# Which kinds are safe to re-run (omp -p is one-shot + idempotent, design §3.2).
RETRYABLE_KINDS = frozenset(
    {
        Kind.ABORT_TRANSIENT_API,
        Kind.ABORT_ERROR,
        Kind.ABORT_MAXTIME,
        Kind.ABORT_CRASH,
        Kind.ABORT_HANG,
        Kind.ABORT_UNKNOWN,
    }
)

# Which kinds heal via a Continue-Nudge (Fix B): the model stopped mid-work or
# just forgot the sentinel/reflection — the least harmful aborts, so instead of
# a Blocker they get a follow-up prompt in the SAME session. These are NOT in
# RETRYABLE_KINDS (a full re-run would relaunch fresh and drop the model's
# context — a continue keeps it).
CONTINUEABLE_KINDS = frozenset(
    {
        Kind.SILENT_ABORT_NO_SENTINEL,
        Kind.MALFORMED_REFLECTION,
        Kind.TRAILING_TOOL_ERROR,
    }
)

# The follow-up prompt per continueable kind (design §4). Kept as the ALLERLETZTE
# instruction the model reads before resuming.
CONTINUE_NUDGE_PROMPTS: dict[Kind, str] = {
    Kind.SILENT_ABORT_NO_SENTINEL: (
        "Dein letzter Turn endete ohne Abschluss-Sentinel. Setze die Arbeit fort "
        "und beende mit der 4-Feld-Reflexion + TASK_COMPLETE als allerletzter Zeile."
    ),
    Kind.MALFORMED_REFLECTION: (
        "Sentinel erhalten, aber die 4-Feld-Reflexion fehlt oder ist zu kurz. "
        "Liefere die vollstaendige Reflexion und beende erneut mit TASK_COMPLETE."
    ),
    Kind.TRAILING_TOOL_ERROR: (
        "Dein letzter Tool-Aufruf endete mit einem Fehler, obwohl du abgeschlossen "
        "hast. Pruefe das Ergebnis, korrigiere falls noetig, und schliesse sauber "
        "ab (Reflexion + TASK_COMPLETE)."
    ),
}

# Kinds whose repeat nudge (2nd+ within the SAME run, for the SAME Kind) drops
# the explanatory prose for a maximally minimal, copy-paste template. A weak
# model that misread the paragraph once is unlikely to parse it better the
# second time; a fill-in-the-blanks block gives it far less to get wrong.
# TRAILING_TOOL_ERROR is excluded — that nudge asks for a judgement call
# (check/fix the tool result), not just format compliance.
_ESCALATING_NUDGE_KINDS = frozenset({Kind.MALFORMED_REFLECTION, Kind.SILENT_ABORT_NO_SENTINEL})


def _minimal_nudge_template() -> str:
    """Maximally minimal, copy-paste completion template used from the 2nd
    continue-nudge onward for a Kind in _ESCALATING_NUDGE_KINDS. Header names
    are sourced from the canonical REFLECTION_HEADERS list (kept in parity
    with mc_cli/reflection.py), never re-typed as fresh literals."""
    body_lines: list[str] = []
    for header in REFLECTION_HEADERS:
        body_lines.append(header)
        body_lines.append("<...>")
    body = "\n".join(body_lines)
    return (
        f"Format falsch. Kopiere EXAKT diesen Block, fuelle die <...> aus, "
        f"{SENTINEL} als letzte Zeile:\n\n{body}\n{SENTINEL}"
    )


def _nudge_prompt_for(kind: Kind, attempt_index: int) -> str:
    """The continue-nudge text for the `attempt_index`-th (0-based) nudge fired
    for this Kind within the current run. attempt_index==0 is always the
    normal, explanatory prompt (often enough on its own — escalating on the
    FIRST attempt would be premature). attempt_index>=1 for a Kind in
    _ESCALATING_NUDGE_KINDS switches to the minimal copy-paste template."""
    if attempt_index >= 1 and kind in _ESCALATING_NUDGE_KINDS:
        return _minimal_nudge_template()
    return CONTINUE_NUDGE_PROMPTS[kind]


@dataclass
class RunOutcome:
    """Everything the reducer distilled from one NDJSON stream."""

    saw_session: bool = False
    saw_agent_start: bool = False
    saw_agent_end: bool = False
    final_stop_reason: Optional[str] = None
    final_text: str = ""
    error_message: Optional[str] = None
    tool_cancelled: bool = False          # "[Command cancelled]" (--max-time)
    last_turn_had_tool_error: bool = False
    turns: int = 0
    tool_calls: int = 0
    parse_failures: int = 0
    session_id: Optional[str] = None
    watchdog_killed: bool = False         # set by the live supervisor, not the file

    # Derived completion-contract signals (filled by classify()).
    sentinel_ok: bool = False
    reflection_block: Optional[str] = None
    reflection_valid: bool = False


@dataclass
class Classification:
    kind: Kind
    retryable: bool
    reason: str          # short machine tag
    detail: str          # human-readable, becomes the blocker question


@dataclass
class LifecycleAction:
    """What the driver decided to do about the run."""

    action: str          # "finish" | "blocker" | "retry" | "continue"
    review: bool = False
    blocker_type: Optional[str] = None
    question: Optional[str] = None
    reflection: Optional[str] = None
    nudge_prompt: Optional[str] = None   # set for action=="continue" (Fix B)
    classification: Optional[Classification] = None


# ---------------------------------------------------------------------------
# Stream parsing
# ---------------------------------------------------------------------------


def iter_events(fileobj: TextIO, outcome: Optional[RunOutcome] = None) -> Iterator[dict]:
    """Yield top-level lifecycle events, dropping message_update + parse failures.

    Malformed lines are counted (outcome.parse_failures) but never raise — a
    partial/truncated stream (crash) must still reduce cleanly.
    """
    for line in fileobj:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            if outcome is not None:
                outcome.parse_failures += 1
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") in _DROP_TYPES:
            continue
        yield obj


def _assistant_text(message: dict) -> str:
    """Concatenate only content[].text of type=='text' (never 'thinking')."""
    parts = []
    for c in message.get("content", []) or []:
        if isinstance(c, dict) and c.get("type") == "text":
            parts.append(c.get("text", ""))
    return "".join(parts)


def reduce_stream(events: Iterable[dict], outcome: Optional[RunOutcome] = None) -> RunOutcome:
    """Fold the event stream into a RunOutcome (the streaming NDJSON reducer)."""
    o = outcome or RunOutcome()
    last_turn_end: Optional[dict] = None

    for ev in events:
        t = ev.get("type")
        if t == "session":
            o.saw_session = True
            o.session_id = ev.get("id")
        elif t == "agent_start":
            o.saw_agent_start = True
        elif t == "turn_start":
            o.turns += 1
            o.last_turn_had_tool_error = False   # reset per turn
        elif t == "tool_execution_start":
            o.tool_calls += 1
        elif t == "tool_execution_end":
            if ev.get("isError"):
                o.last_turn_had_tool_error = True
                text = _tool_result_text(ev)
                if "[command cancelled]" in text.lower():
                    o.tool_cancelled = True
        elif t == "message_end":
            msg = ev.get("message", {}) or {}
            if msg.get("role") == "assistant" and msg.get("stopReason") == "error":
                o.error_message = msg.get("errorMessage") or o.error_message
        elif t == "turn_end":
            last_turn_end = ev
        elif t == "agent_end":
            o.saw_agent_end = True

    if last_turn_end is not None:
        msg = last_turn_end.get("message", {}) or {}
        o.final_stop_reason = msg.get("stopReason")
        o.final_text = _assistant_text(msg)
        if msg.get("errorMessage"):
            o.error_message = msg.get("errorMessage")
    return o


def _tool_result_text(ev: dict) -> str:
    result = ev.get("result") or {}
    parts = []
    for c in result.get("content", []) or []:
        if isinstance(c, dict) and c.get("type") == "text":
            parts.append(c.get("text", ""))
    return "".join(parts)


# ---------------------------------------------------------------------------
# Completion contract (§3.4)
# ---------------------------------------------------------------------------


def last_nonempty_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return ""


def extract_reflection(text: str) -> Optional[str]:
    """Slice the reflection block from the FIRST recognised header (any level
    1-3, case-insensitive, EN alias, ü/ue, trailing colon) to the line before
    the sentinel, with every recognised header normalised to canonical German
    `## ` form. Returns None if no recognised header is present.

    MIRROR of mc_cli.reflection.extract_reflection — keep in lockstep.
    """
    lines = text.splitlines()
    start = -1
    for i, line in enumerate(lines):
        if _match_header(line) is not None:
            start = i
            break
    if start == -1:
        return None
    kept = []
    for line in lines[start:]:
        if _is_sentinel_line(line):
            break
        canon = _match_header(line)
        kept.append(f"## {canon}" if canon is not None else line)
    return "\n".join(kept).strip()


def validate_reflection(block: Optional[str]) -> bool:
    """4 canonical German headers present + >=80 chars. Expects a block already
    run through extract_reflection (headers canonicalised).

    MIRROR of mc_cli.reflection.validate_reflection — keep in lockstep.
    """
    if not block:
        return False
    if len(block) < MIN_REFLECTION_CHARS:
        return False
    return all(h in block for h in REFLECTION_HEADERS)


def _count_present_headers(block: str) -> int:
    """How many of the 4 canonical headers appear in an (already-normalised)
    reflection block. Used for partial-reflection salvage (Fix 2) — a block
    that fails validate_reflection() can still be mostly there."""
    return sum(1 for h in REFLECTION_HEADERS if h in block)


def sentinel_present(text: str) -> bool:
    """Anti-echo: the sentinel counts only as the LAST meaningful line (alone,
    modulo markdown/punctuation), OR the second-to-last when the final line is a
    harmless trailer (`---`/`***`).

    MIRROR of mc_cli.reflection.sentinel_present — keep in lockstep.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False
    if _is_sentinel_line(lines[-1]):
        return True
    if len(lines) >= 2 and _TRAILER_RE.match(lines[-1].strip()) and _is_sentinel_line(lines[-2]):
        return True
    return False


# ---------------------------------------------------------------------------
# Classification (§3.3 core table)
# ---------------------------------------------------------------------------


def classify(outcome: RunOutcome) -> Classification:
    """Map a reduced RunOutcome to exactly one terminal Kind."""
    o = outcome

    # (0) Watchdog killed the subprocess (hang) — set by the live supervisor.
    #     Checked FIRST: a wall-clock/no-progress SIGKILL is a hang regardless of
    #     stream state (design §3.3 "from the outside, regardless of stream state"),
    #     including a wedge before the `session` line ever lands — that is a hung
    #     omp, not a deterministic launch/preflight failure.
    if o.watchdog_killed:
        return Classification(
            Kind.ABORT_HANG,
            retryable=True,
            reason="hang",
            detail="omp reagierte nicht mehr (kein Stream-Fortschritt) — "
                   "vom Wall-Clock-Watchdog beendet.",
        )

    # (1) Launch / preflight failure: no json session ever emitted.
    if not o.saw_session:
        return Classification(
            Kind.LAUNCH_PREFLIGHT,
            retryable=False,
            reason="launch_preflight",
            detail="omp Launch/Preflight-Fehler (keine json-session emittiert) — "
                   "Konfiguration/Credential pruefen (deterministisch, retry hilft nicht).",
        )

    # (2) Crash: normal run must terminate with agent_end. Its absence = abnormal.
    if not o.saw_agent_end:
        return Classification(
            Kind.ABORT_CRASH,
            retryable=True,
            reason="crash",
            detail="Stream endete ohne agent_end (Crash/SIGKILL/abgeschnitten).",
        )

    sr = o.final_stop_reason

    # (3) Model/provider error surfaces ONLY as stopReason==error (design §2).
    if sr == "error":
        blob = f"{o.error_message or ''} {o.final_text or ''}"
        if TRANSIENT_ERROR_RE.search(blob):
            return Classification(
                Kind.ABORT_TRANSIENT_API,
                retryable=True,
                reason="transient_api_error",
                detail=f"Transienter API/Netzwerk-Fehler (die openclaude-Originalstoerung): "
                       f"{(o.error_message or '').strip()[:200]}",
            )
        return Classification(
            Kind.ABORT_ERROR,
            retryable=True,
            reason="model_error",
            detail=f"Modell/Provider-Fehler (stopReason=error): "
                   f"{(o.error_message or '').strip()[:200]}",
        )

    # (4) --max-time cut a tool mid-flight -> toolUse / [Command cancelled].
    if sr == "toolUse" or o.tool_cancelled:
        return Classification(
            Kind.ABORT_MAXTIME,
            retryable=True,
            reason="maxtime_cutoff",
            detail="Lauf wurde mitten im Tool abgeschnitten (--max-time / [Command cancelled]) "
                   "— Aufgabe unvollstaendig.",
        )

    # (5) stopReason==stop -> apply the completion contract (§3.4).
    if sr == "stop":
        o.sentinel_ok = sentinel_present(o.final_text)
        o.reflection_block = extract_reflection(o.final_text)
        o.reflection_valid = validate_reflection(o.reflection_block)

        if not o.sentinel_ok:
            return Classification(
                Kind.SILENT_ABORT_NO_SENTINEL,
                retryable=False,
                reason="silent_abort_no_sentinel",
                detail="omp-Turn endete (stopReason=stop) ohne gueltige TASK_COMPLETE-Sentinel "
                       "als letzte Zeile — Aufgabe evtl. nicht abgeschlossen; bitte pruefen/fortsetzen.",
            )
        if not o.reflection_valid:
            return Classification(
                Kind.MALFORMED_REFLECTION,
                retryable=False,
                reason="malformed_reflection",
                detail="Sentinel vorhanden, aber 4-Feld-Reflexion fehlt/ist <80 Zeichen — "
                       "mc finish wuerde lokal abgelehnt; bitte Reflexion nachziehen.",
            )
        if o.last_turn_had_tool_error:
            return Classification(
                Kind.TRAILING_TOOL_ERROR,
                retryable=False,
                reason="trailing_tool_error",
                detail="Finale Runde meldete einen Tool-Fehler (isError) trotz stop+Sentinel — "
                       "Ergebnis fraglich; bitte pruefen.",
            )
        return Classification(
            Kind.FINISH,
            retryable=False,
            reason="finish",
            detail="Genuiner Abschluss: agent_end + stopReason=stop + Sentinel + gueltige Reflexion.",
        )

    # (6) Any other terminal stopReason.
    return Classification(
        Kind.ABORT_UNKNOWN,
        retryable=True,
        reason="unknown_stop_reason",
        detail=f"Unerwarteter finaler stopReason={sr!r}.",
    )


def classify_stream(fileobj: TextIO) -> tuple[RunOutcome, Classification]:
    """Convenience: reduce + classify a stream in one call."""
    outcome = RunOutcome()
    reduce_stream(iter_events(fileobj, outcome), outcome)
    return outcome, classify(outcome)


# ---------------------------------------------------------------------------
# Lifecycle mapping (retry-then-blocked policy, §3.3)
# ---------------------------------------------------------------------------


def decide_lifecycle(
    classification: Classification,
    *,
    board_requires_review: bool,
    retries_left: int,
    continues_left: int = 0,
) -> LifecycleAction:
    """Map a classification to a single MC lifecycle action.

    Policy (in order): FINISH -> finish/review. Continueable self-completion
    abort with continues left -> continue (Fix B: a follow-up nudge in the SAME
    session). Retryable abort with retries left -> retry (fresh re-run).
    Everything else (both budgets exhausted, non-continueable/non-retryable)
    -> blocker.
    NEVER `mc failed` (FAILED -> {INBOX} only, auto-unassigns, no auto-redispatch).

    `continues_left` defaults to 0 so callers that don't opt in keep the exact
    old behavior (a continueable kind falls straight through to blocker).
    """
    c = classification
    if c.kind is Kind.FINISH:
        return LifecycleAction(
            action="finish",
            review=board_requires_review,
            classification=c,
        )

    if c.kind in CONTINUEABLE_KINDS and continues_left > 0:
        return LifecycleAction(
            action="continue",
            nudge_prompt=CONTINUE_NUDGE_PROMPTS[c.kind],
            classification=c,
        )

    if c.retryable and retries_left > 0:
        return LifecycleAction(action="retry", classification=c)

    return LifecycleAction(
        action="blocker",
        blocker_type="technical_problem",
        question=c.detail,
        classification=c,
    )


# ---------------------------------------------------------------------------
# Pluggable MC lifecycle hooks (PROTOTYPE STUB — logs only, no backend call)
# ---------------------------------------------------------------------------


class MCLifecycle:
    """Interface the driver calls. Real impl shells out to the `mc` CLI."""

    def ack(self, task_id: str) -> None: ...
    def finish(self, task_id: str, reflection: str, *, review: bool) -> None: ...
    def set_blocker(self, task_id: str, *, blocker_type: str, question: str) -> None: ...
    def comment(self, task_id: str, text: str) -> None: ...

    def task_is_active(self, task_id: str) -> Optional[bool]:
        """Is `task_id` still the agent's live current task?

        True/False when determinable, None when it cannot be determined
        (best-effort, fail-open — callers must treat None exactly like True,
        i.e. keep nudging/retrying as before). Used by `drive_live_run` to
        stop nudging/retrying a task that was already completed or reassigned
        by the operator out-of-band (e.g. a human closed the task manually
        while a long live run was still going — see the 2026-07-12 Bench-
        Studio incident where the bridge kept posting continue-nudges on an
        already-done task for 40 minutes).
        """
        return None


class LoggingLifecycle(MCLifecycle):
    """Prototype hook set — logs the INTENDED mc-CLI call, never hits backend."""

    def __init__(self, sink: Optional[Callable[[str], None]] = None) -> None:
        self._sink = sink or (lambda s: print(s, file=sys.stderr))
        self.calls: list[tuple] = []

    def _log(self, kind: str, msg: str) -> None:
        self.calls.append((kind,))
        self._sink(f"[mc-stub] {msg}")

    def ack(self, task_id: str) -> None:
        self.calls.append(("ack", task_id))
        self._sink(f"[mc-stub] mc ack            (task={task_id})  # inbox -> in_progress, stamp ack_at")

    def finish(self, task_id: str, reflection: str, *, review: bool) -> None:
        target = "review" if review else "done"
        self.calls.append(("finish", task_id, review))
        self._sink(
            f"[mc-stub] mc finish{' --review' if review else ''}  (task={task_id})  "
            f"# in_progress -> {target}, {len(reflection)}-char reflection"
        )

    def set_blocker(self, task_id: str, *, blocker_type: str, question: str) -> None:
        self.calls.append(("blocker", task_id, blocker_type))
        self._sink(
            f"[mc-stub] mc blocked --blocker-type {blocker_type}  (task={task_id})\n"
            f"          --question {question!r}"
        )

    def comment(self, task_id: str, text: str) -> None:
        self.calls.append(("comment", task_id))
        self._sink(f"[mc-stub] mc comment          (task={task_id})  # {text}")

    def task_is_active(self, task_id: str) -> Optional[bool]:
        self.calls.append(("task_is_active", task_id))
        self._sink(f"[mc-stub] mc me               (task={task_id})  # undeterminable in stub, fail-open")
        return None


# ---------------------------------------------------------------------------
# Driver — replay a stream through the lifecycle
# ---------------------------------------------------------------------------


def drive_run(
    fileobj: TextIO,
    lifecycle: MCLifecycle,
    *,
    task_id: str = "TASK",
    board_requires_review: bool = True,
    retries_left: int = 0,
    spawn: Optional[Callable[[], TextIO]] = None,
) -> LifecycleAction:
    """Drive an omp run to a TERMINAL decision: ack on session, then finish or
    blocker on resolution — owning the bounded-retry loop end to end.

    `drive_run` NEVER returns a non-terminal `retry`. On a retryable abort class
    with budget left AND a `spawn` executor (a callable returning a fresh omp
    stream), it re-spawns and re-drives up to `retries_left` times (design §3.3
    "re-run omp -p up to OMP_MAX_RETRIES ... If still in the abort class after all
    retries -> mc blocked"). When the budget is exhausted — or no `spawn`
    executor is wired (pure replay) — a retryable class collapses to a terminal
    `mc blocked`. This closes the retry-path silent-abort gap: there is no path
    where the run ends and the task is left `in_progress`.

    The ack fires exactly once (the first stream that emits `session`); retries
    re-run the same, already-claimed task, so they never re-ack.
    """
    attempts_left = retries_left
    current: TextIO = fileobj
    acked = False

    while True:
        outcome = RunOutcome()

        # Stream and ack as soon as the session line lands (claim the work, once).
        for _ev in reduce_events_streaming(iter_events(current, outcome), outcome):
            if not acked and outcome.saw_session:
                lifecycle.ack(task_id)
                acked = True
        if not acked and outcome.saw_session:
            lifecycle.ack(task_id)
            acked = True

        cls = classify(outcome)
        action = decide_lifecycle(
            cls, board_requires_review=board_requires_review, retries_left=attempts_left
        )

        # Retryable abort WITH a spawn executor and budget -> re-run omp, loop.
        if action.action == "retry" and spawn is not None and attempts_left > 0:
            lifecycle.comment(
                task_id, f"omp abort ({cls.reason}); retrying, {attempts_left} left"
            )
            attempts_left -= 1
            current = spawn()
            continue

        # Terminal from here. A stranded `retry` (budget exhausted or no executor)
        # collapses to a blocker so the run ALWAYS resolves terminally.
        if action.action == "finish":
            lifecycle.finish(task_id, outcome.reflection_block or "", review=action.review)
        else:
            if action.action == "retry":
                action = LifecycleAction(
                    action="blocker",
                    blocker_type="technical_problem",
                    question=cls.detail,
                    classification=cls,
                )
            lifecycle.set_blocker(
                task_id,
                blocker_type=action.blocker_type or "technical_problem",
                question=action.question or cls.detail,
            )

        action.reflection = outcome.reflection_block
        return action


def reduce_events_streaming(events: Iterable[dict], outcome: RunOutcome) -> Iterator[dict]:
    """Same fold as reduce_stream, but yields each event so the driver can act
    on `session` mid-stream (for the ack). Populates `outcome` in place."""
    last_turn_end = None
    for ev in events:
        t = ev.get("type")
        if t == "session":
            outcome.saw_session = True
            outcome.session_id = ev.get("id")
        elif t == "agent_start":
            outcome.saw_agent_start = True
        elif t == "turn_start":
            outcome.turns += 1
            outcome.last_turn_had_tool_error = False
        elif t == "tool_execution_start":
            outcome.tool_calls += 1
        elif t == "tool_execution_end":
            if ev.get("isError"):
                outcome.last_turn_had_tool_error = True
                if "[command cancelled]" in _tool_result_text(ev).lower():
                    outcome.tool_cancelled = True
        elif t == "message_end":
            msg = ev.get("message", {}) or {}
            if msg.get("role") == "assistant" and msg.get("stopReason") == "error":
                outcome.error_message = msg.get("errorMessage") or outcome.error_message
        elif t == "turn_end":
            last_turn_end = ev
        elif t == "agent_end":
            outcome.saw_agent_end = True
        yield ev
    if last_turn_end is not None:
        msg = last_turn_end.get("message", {}) or {}
        outcome.final_stop_reason = msg.get("stopReason")
        outcome.final_text = _assistant_text(msg)
        if msg.get("errorMessage"):
            outcome.error_message = msg.get("errorMessage")


# ---------------------------------------------------------------------------
# Live subprocess path + out-of-band wall-clock / no-progress watchdog (§3.3)
# ---------------------------------------------------------------------------


def supervise_stream(
    stream: TextIO,
    outcome: RunOutcome,
    *,
    kill: Callable[[], None],
    deadline: float,
    stream_idle_timeout: float,
    tee: Optional[TextIO] = None,
    now: Callable[[], float] = time.monotonic,
    poll_interval: float = 1.0,
) -> RunOutcome:
    """Reduce `stream` while an INDEPENDENT wall-clock + no-progress watchdog runs
    OUT OF BAND (design §3.3 "closes the hang case").

    Why out of band: a genuine hang (deadlocked provider read / TLS stall) emits
    no NDJSON, so `readline()` blocks forever. If the deadline check lived inside
    the read loop it would never run. So the blocking read happens on a daemon
    reader thread that stamps a REAL last-progress timestamp on every actual line
    read; the main thread drains parsed events on a `poll_interval` timer and
    evaluates BOTH deadlines even while the reader is wedged. On either deadline
    it flips `watchdog_killed`, calls `kill()` (SIGKILLs omp -> EOF unblocks the
    reader), and stops — so a hung omp can never leave the run non-terminal.

    `kill` is injected so this is unit-testable against a fake blocking pipe
    without spawning a real subprocess.
    """
    import threading
    import queue as _queue

    q: "_queue.Queue[object]" = _queue.Queue()
    _EOF = object()
    last_progress = now()
    lock = threading.Lock()

    def _reader() -> None:
        nonlocal last_progress
        try:
            # readline() (not `for line in stream`) — file iteration does read-ahead
            # buffering that can block on a pipe even when whole lines are ready,
            # which would defeat both the progress stamp and the idle watchdog.
            while True:
                raw = stream.readline()
                if raw == "":
                    break  # EOF (process exited / pipe closed by kill()).
                # Progress = ANY byte from the process, tracked before message_update
                # deltas are dropped, so a chatty-but-not-hung stream never trips idle.
                with lock:
                    last_progress = now()
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    outcome.parse_failures += 1
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("type") in _DROP_TYPES:
                    continue
                q.put(obj)
        finally:
            q.put(_EOF)

    reader = threading.Thread(target=_reader, name="omp-reader", daemon=True)
    reader.start()

    def _drain() -> Iterator[dict]:
        while True:
            try:
                item = q.get(timeout=poll_interval)
            except _queue.Empty:
                item = None
            t_now = now()
            with lock:
                idle = t_now - last_progress
            if t_now > deadline or idle > stream_idle_timeout:
                outcome.watchdog_killed = True
                kill()
                return
            if item is _EOF:
                return
            if item is not None:
                ev: dict = item  # type: ignore[assignment]
                if tee is not None:
                    tee.write(json.dumps(ev) + "\n")
                    tee.flush()
                yield ev

    reduce_stream(_drain(), outcome)
    reader.join(timeout=5)
    return outcome


def run_omp_subprocess(
    prompt: str,
    *,
    cwd: str,
    model: str = "claude-opus-4-8",
    max_time: int = 900,
    stream_idle_timeout: int = 90,
    wall_clock_margin: int = 120,
    tee: Optional[TextIO] = None,
) -> RunOutcome:
    """Spawn `omp -p --mode json ...` and reduce its stdout under the independent
    wall-clock + no-progress watchdog (`supervise_stream`, design §3.3).

    This is the LIVE path. The supervisor is unit-tested via `supervise_stream`
    against a fake blocking pipe; this wrapper only wires the real subprocess.
    """
    import subprocess

    cmd = [
        "omp", "-p",
        "--cwd", cwd,
        "--mode", "json",
        "--model", model,
        "--approval-mode", "yolo",
        "--no-session",
        "--hide-thinking",
        "--max-time", str(max_time),
        prompt,
    ]
    outcome = RunOutcome()
    deadline = time.monotonic() + max_time + wall_clock_margin

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, cwd=cwd,
    )
    assert proc.stdout is not None
    try:
        supervise_stream(
            proc.stdout, outcome,
            kill=proc.kill,
            deadline=deadline,
            stream_idle_timeout=stream_idle_timeout,
            tee=tee,
        )
    finally:
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    if proc.returncode not in (0, None) and not outcome.saw_session:
        # exit 1/2 with no session -> launch/preflight (handled by classify()).
        outcome.saw_session = False
    return outcome


# ---------------------------------------------------------------------------
# Phase 2 — real mc-CLI lifecycle + persistent --serve poll loop (ADR-045 §4.3/§4.4)
# ---------------------------------------------------------------------------

import os
import re as _re
import shlex
import subprocess
import urllib.parse

# The task-active lock the forked recycler (omp-recycler.sh) gates on: while it
# is present an idle omp gap is NEVER read as a crash / idle-kill candidate.
TASK_LOCK_FILE = os.environ.get("OMP_TASK_LOCK_FILE", "/home/agent/.task-active.lock")

# Prompt-wrapping contract (§3.4). Without this the model ends a run with
# `Done.` and no sentinel -> classify() => SILENT_ABORT_NO_SENTINEL => blocker.
COMPLETION_INSTRUCTIONS = (
    "\n\n---\n"
    "WICHTIG — Abschluss-Protokoll (verbindlich):\n"
    "Wenn die Aufgabe vollstaendig erledigt ist, gib als ALLERLETZTE Ausgabe\n"
    "GENAU diesen Block aus (nichts danach), mit allen 4 Ueberschriften und\n"
    "mindestens 80 Zeichen Inhalt insgesamt:\n\n"
    "## Was wurde gemacht\n<kurz>\n"
    "## Was hat funktioniert\n<kurz>\n"
    "## Was war unklar\n<kurz>\n"
    "## Lesson fuer Agent-Memory\n<kurz>\n"
    "TASK_COMPLETE\n\n"
    "Die Zeile `TASK_COMPLETE` MUSS die allerletzte nicht-leere Zeile sein.\n"
    "Ohne diesen Block gilt die Aufgabe als NICHT abgeschlossen.\n"
)


def _identity_block(home_dir: Optional[str] = None) -> str:
    """Reads CARD.md — the context-economy Stage 2 opt-in (agent.
    use_operating_card, rendered by docker_agent_sync into the claude-config
    bind mount, i.e. $HOME/.claude/ inside this container) — if present.

    Deliberately CARD-ONLY, no SOUL.md fallback: omp is opt-IN. An omp agent
    without the flag gets nothing here, exactly like before this feature
    existed — bridge.py must never start injecting the ~29KB SOUL.md into
    every dispatched prompt just because SOUL.md happens to sit on disk
    (docker_agent_sync writes it for every agent regardless of runtime). That
    would blow up context for every non-piloted omp agent (Qwen/DeepSeek
    degrade badly on oversized prompts) and would make flipping the flag back
    OFF on the pilot (Sparky) a regression instead of a clean rollback to the
    prior (empty) behaviour.

    Unlike the claude/openclaude harnesses (SOUL/CARD injected once via
    --append-system-prompt at process start, see
    docker/mc-agent-base/start-claude.sh — those DO keep a SOUL fallback,
    they always had one), the omp native TUI has no such flag — bridge.py
    relaunches Window 0 per task and drives it purely through injected
    prompts, so the card is prepended to the dispatch prompt instead.
    """
    home = home_dir or os.environ.get("HOME", "/home/agent")
    card_path = os.path.join(home, ".claude", "CARD.md")
    try:
        with open(card_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def wrap_prompt(prompt: str, *, home_dir: Optional[str] = None, include_identity: bool = True) -> str:
    """Appends the §3.4 completion contract to the MC-built dispatch prompt,
    prepending the CARD.md identity block (Stage 2 opt-in only) when
    `include_identity` is set.

    `include_identity=False` for continue-nudges (see the `continue_once`
    callback in `run_omp_serve_loop`): a nudge resumes the SAME live omp
    session that already received the card on its first-dispatch prompt —
    re-prepending it on every nudge (up to OMP_MAX_CONTINUES times) would
    just burn context re-stating what the session already has.
    """
    body = (prompt or "").rstrip()
    if include_identity:
        identity = _identity_block(home_dir)
        if identity:
            body = f"{identity}\n\n---\n\n{body}"
    return body + COMPLETION_INSTRUCTIONS


def container_workspace_path(host_path: Optional[str]) -> Optional[str]:
    """Translate a host-side workspace path to the container `/workspace` view.

    Mirrors backend dispatch._container_workspace_path: the agent container
    mounts `~/.mc/workspaces/<slug>` at `/workspace`, so the host path in the
    poll response (`task.workspace_path`) must be rewritten. Returns None for a
    null path (ad-hoc tasks) so the caller can fall back to the mount root.
    """
    if not host_path:
        return None
    m = _re.match(r"^(?:/[^/]+)+?/\.mc/workspaces/([^/]+)(/.*)?$", host_path)
    if m:
        cand = os.path.normpath("/workspace" + (m.group(2) or ""))
        return cand if cand.startswith("/workspace") else "/workspace"
    m = _re.match(r"^(?:/[^/]+)+?/\.openclaw/workspace-([^/]+)(/.*)?$", host_path)
    if m:
        cand = os.path.normpath("/workspace" + (m.group(2) or ""))
        return cand if cand.startswith("/workspace") else "/workspace"
    return host_path


class McCliLifecycle(MCLifecycle):
    """Real lifecycle — shells out to the copied `mc` CLI (`mc ack|finish|blocked`).

    Task/board/attempt context is injected via env (the exact contract
    `mc_cli/config.py:from_env` reads). No new backend endpoint — the same
    lifecycle the whole fleet uses.
    """

    def __init__(
        self,
        *,
        api_url: str,
        token: str,
        task_id: str,
        board_id: Optional[str],
        attempt_id: Optional[str],
        mc_bin: str = "mc",
    ) -> None:
        self.api_url = api_url
        self.token = token
        self.board_id = board_id or ""
        self.attempt_id = attempt_id or ""
        self.mc_bin = mc_bin

    def _env(self, task_id: str) -> dict:
        env = dict(os.environ)
        env["MC_API_URL"] = self.api_url
        env["MC_AGENT_TOKEN"] = self.token
        env["TASK_ID"] = task_id
        env["BOARD_ID"] = self.board_id
        env["X_DISPATCH_ATTEMPT_ID"] = self.attempt_id
        return env

    # Marker `_preflight_finish` in mc-cli prefixes onto its UsageError message
    # when `mc finish` is rejected purely because checklist items are still
    # open (see mc_cli/commands.py:_preflight_finish). Used to distinguish
    # "work is done except an out-of-role checklist item" from a genuine
    # technical failure (5xx, network, unparseable) — the two used to be
    # indistinguishable to this bridge and both collapsed into `blocked`.
    _CHECKLIST_OPEN_MARKER = "Checklist-Item(s) noch offen"

    def _run(
        self, task_id: str, args: list[str], *, best_effort: bool = False,
    ) -> tuple[int, str]:
        """Run an mc-cli subcommand. Returns ``(exit_code, stderr_text)`` —
        ``exit_code`` is ``-1`` if the command could not be launched and
        ``best_effort`` swallowed the error."""
        cmd = [self.mc_bin, *args]
        try:
            proc = subprocess.run(
                cmd, env=self._env(task_id), capture_output=True, text=True, timeout=60,
            )
            stderr_text = (proc.stderr or proc.stdout or "").strip()
            if proc.returncode != 0:
                sys.stderr.write(
                    f"[mc-cli] {args[0]} exit={proc.returncode}: {stderr_text[:400]}\n"
                )
            return proc.returncode, stderr_text
        except Exception as e:  # noqa: BLE001 — lifecycle must never crash the loop
            sys.stderr.write(f"[mc-cli] {args[0]} raised {type(e).__name__}: {e}\n")
            if not best_effort:
                raise
            return -1, str(e)

    def ack(self, task_id: str) -> None:
        self._run(task_id, ["ack", task_id])

    def finish(self, task_id: str, reflection: str, *, review: bool) -> None:
        args = ["finish", task_id, reflection]
        if review:
            args.append("--review")
        # Terminal guarantee: a non-zero `mc finish` (backend rejected the
        # reflection, transient 5xx, ...) must NOT leave the task silently
        # in_progress — that is the exact silent-hang this runtime exists to
        # close. best_effort=True so a hard launch failure returns rc=-1 here
        # instead of raising past the fallback.
        rc, stderr_text = self._run(task_id, args, best_effort=True)
        if rc == 0:
            return

        if self._CHECKLIST_OPEN_MARKER in (stderr_text or ""):
            # The agent's work is otherwise done — some checklist items are
            # still open, possibly because they are out of this agent's role
            # (e.g. a live Vercel deploy needing npm/node, which is a
            # Deployer's job). Blanket `blocked`/technical_problem would lie
            # about *why* the task stalled. Route to review instead, with a
            # handoff comment carrying the exact pending items, so the Board
            # Lead/Mark can see what's outstanding and reassign the rest.
            sys.stderr.write(
                f"[mc-cli] finish failed (rc={rc}) due to open checklist items "
                f"-> routing to review with handoff instead of blocked (task {task_id})\n"
            )
            self._run(
                task_id,
                [
                    "comment", "handoff",
                    "omp-bridge: `mc finish` konnte den Task nicht abschliessen, "
                    "weil Checklist-Items offen sind (evtl. ausserhalb der Rolle "
                    f"dieses Agents). Details:\n{stderr_text}",
                ],
                best_effort=True,
            )
            review_rc, review_stderr = self._run(
                task_id, ["review", task_id], best_effort=True,
            )
            if review_rc == 0:
                return
            # The review rescue itself failed (network/5xx/concurrent status
            # change). We must NOT return here — that would leave the task
            # silently in_progress, the exact hang this runtime exists to
            # close. Fall through to the `blocked` fallback below, with a
            # question that makes clear both finish AND review failed.
            sys.stderr.write(
                f"[mc-cli] review rescue failed (rc={review_rc}) after checklist-open "
                f"finish -> falling back to blocked (task {task_id})\n"
            )
            self.set_blocker(
                task_id,
                blocker_type="technical_problem",
                question=(
                    "omp-bridge: `mc finish` scheiterte an offenen Checklist-Items "
                    f"und der Rettungsversuch `mc review` schlug ebenfalls fehl "
                    f"(exit={review_rc}). Automatisch blockiert statt still "
                    "in_progress haengen zu lassen — bitte Ergebnis pruefen, "
                    f"offene Items klaeren und Task erneut zuweisen. "
                    f"Details:\n{stderr_text}"
                ),
            )
            return

        # Genuinely unexpected failure (5xx, network, unparseable output,
        # missing `mc` binary, ...) — fall back to `blocked` (reversible,
        # notifies Mark) so every run still reaches a terminal state.
        sys.stderr.write(
            f"[mc-cli] finish failed (rc={rc}) -> falling back to blocked "
            f"to avoid a silent in_progress hang (task {task_id})\n"
        )
        self.set_blocker(
            task_id,
            blocker_type="technical_problem",
            question=(
                "omp-bridge konnte den Task nicht auf review setzen "
                f"(mc finish exit={rc}). Automatisch blockiert statt still "
                "in_progress haengen zu lassen — bitte Ergebnis pruefen und "
                "Task erneut zuweisen."
            ),
        )

    def set_blocker(self, task_id: str, *, blocker_type: str, question: str) -> None:
        self._run(
            task_id,
            ["blocked", task_id, "--blocker-type", blocker_type, "--question", question],
        )

    def comment(self, task_id: str, text: str) -> None:
        # Audit-only progress note (best-effort — a failed comment must not
        # abort the retry loop).
        self._run(task_id, ["comment", "progress", text], best_effort=True)

    def task_is_active(self, task_id: str) -> Optional[bool]:
        """`mc me` -> compare its `current_task.id` against `task_id`.

        Best-effort by design (fail-open, never raises): a garbled/absent
        `current_task` or any parse/subprocess failure returns None, which
        `drive_live_run` treats exactly like True (keep nudging/retrying —
        the pre-existing behavior). Only a CONFIRMED mismatch (or a null
        `current_task`, i.e. the agent has no live task at all) returns
        False, which stops the nudge/retry loop.
        """
        rc, stdout_text = self._run(task_id, ["me"], best_effort=True)
        if rc != 0:
            return None
        try:
            idx = stdout_text.index("{")
        except ValueError:
            return None
        try:
            data = json.loads(stdout_text[idx:])
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        current_task = data.get("current_task")
        if not current_task:
            return False
        if not isinstance(current_task, dict):
            return None
        return str(current_task.get("id")) == str(task_id)


def _set_task_lock(active: bool) -> None:
    path = os.environ.get("OMP_TASK_LOCK_FILE", TASK_LOCK_FILE)
    try:
        if active:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(str(int(time.time())))
        elif os.path.exists(path):
            os.remove(path)
    except OSError as e:  # pragma: no cover — defensive (best-effort recycler gate)
        sys.stderr.write(f"[serve] task-lock {'set' if active else 'clear'} failed: {e}\n")


def drive_live_run(
    lifecycle: MCLifecycle,
    run_once: Callable[[], RunOutcome],
    *,
    task_id: str,
    board_requires_review: bool,
    retries_left: int,
    pre_acked: bool = False,
    continues_left: int = 0,
    continue_once: Optional[Callable[[str], RunOutcome]] = None,
) -> LifecycleAction:
    """Live-path analogue of drive_run for the omp SUBPROCESS / native-TUI model.

    drive_run consumes a TextIO stream and re-derives the outcome; the live path
    already has a fully-reduced `RunOutcome` from `run_omp_subprocess` (which ran
    the out-of-band wall-clock/no-progress watchdog), so re-serialising it would
    DROP the `watchdog_killed` verdict. This reuses the genuinely reusable cores
    — `classify` + `decide_lifecycle` — with the same policy:
    ack once, then per outcome: FINISH -> finish; a continueable self-completion
    abort with continue-budget -> continue-nudge (`continue_once`, SAME session);
    a retryable abort with retry-budget -> retry (`run_once`, fresh re-run);
    both budgets spent -> terminal blocker. ALWAYS terminal (finish|blocker),
    never `mc failed`, never left `in_progress`.

    `pre_acked=True` when the caller already stamped `mc ack` (serve_loop acks up
    front so a long omp run cannot trip the 10-min ACK-timeout re-dispatch and so
    `mc finish` sees the required `in_progress` precondition).

    `continues_left` / `continue_once` opt into Fix B. `continue_once(nudge)`
    returns the RunOutcome of the nudged follow-up turn. Both default off so the
    subprocess path (no live session to continue) keeps the old behavior.
    """
    attempts_left = retries_left
    continues = continues_left
    acked = pre_acked
    # Fix 1: per-Kind nudge count within this run — the 2nd+ nudge for the SAME
    # Kind escalates to the minimal copy-paste template (see _nudge_prompt_for).
    nudge_counts: dict[Kind, int] = {}
    outcome = run_once()
    while True:
        if not acked and outcome.saw_session:
            lifecycle.ack(task_id)
            acked = True
        cls = classify(outcome)
        action = decide_lifecycle(
            cls, board_requires_review=board_requires_review,
            retries_left=attempts_left, continues_left=continues,
        )
        # Continue-Nudge: resume the SAME session with a follow-up prompt (Fix B).
        if action.action == "continue" and continue_once is not None and continues > 0:
            # Stop nudging a task the operator already closed/reassigned out-
            # of-band (2026-07-12 Bench-Studio incident: 40min of continue-
            # nudges + comments on an already-done task). Only a CONFIRMED
            # False stops the loop — True/None (undeterminable) keep the
            # exact old fail-open behavior.
            if lifecycle.task_is_active(task_id) is False:
                sys.stderr.write(
                    "[drive_live_run] task no longer active (done/review/reassigned) "
                    "-> stopping without nudge\n"
                )
                return LifecycleAction(
                    action="halted_external", classification=cls,
                    reflection=outcome.reflection_block,
                )
            lifecycle.comment(
                task_id, f"omp {cls.reason}; continue-nudge, {continues} left"
            )
            continues -= 1
            attempt_index = nudge_counts.get(cls.kind, 0)
            nudge_counts[cls.kind] = attempt_index + 1
            nudge_prompt = _nudge_prompt_for(cls.kind, attempt_index)
            outcome = continue_once(nudge_prompt)
            continue
        if action.action == "retry" and attempts_left > 0:
            if lifecycle.task_is_active(task_id) is False:
                sys.stderr.write(
                    "[drive_live_run] task no longer active (done/review/reassigned) "
                    "-> stopping without nudge\n"
                )
                return LifecycleAction(
                    action="halted_external", classification=cls,
                    reflection=outcome.reflection_block,
                )
            lifecycle.comment(task_id, f"omp abort ({cls.reason}); retrying, {attempts_left} left")
            attempts_left -= 1
            # Fresh session = fresh nudge history: a retry relaunches a BRAND-NEW
            # session that never saw the explanatory first nudge, so escalation
            # (Fix 1) must start over — otherwise the minimal template would fire
            # on a session that never got the normal prompt.
            nudge_counts = {}
            outcome = run_once()
            continue
        if action.action == "finish":
            lifecycle.finish(task_id, outcome.reflection_block or "", review=action.review)
        else:
            # Budget exhausted / no executor wired -> collapse to a terminal blocker.
            if action.action in ("retry", "continue"):
                action = LifecycleAction(
                    action="blocker", blocker_type="technical_problem",
                    question=cls.detail, classification=cls,
                )
            # Fix 2: partial-reflection salvage. A MALFORMED_REFLECTION collapse
            # often hides an almost-complete summary the model actually wrote —
            # today it survives only as a fragment quoted inside the blocker
            # question. If the last output cleared at least half the canonical
            # headers and the length floor, post it as its OWN progress comment
            # BEFORE the blocker lands, so Lead/operator see the agent's real
            # summary. Best-effort: a failed salvage must never break the
            # terminal blocker guarantee.
            if cls.kind is Kind.MALFORMED_REFLECTION:
                block = outcome.reflection_block or ""
                if _count_present_headers(block) >= 2 and len(block) >= MIN_REFLECTION_CHARS:
                    try:
                        lifecycle.comment(
                            task_id,
                            "Partielle Reflexion (auto-gerettet vor Blocker): " + block,
                        )
                    except Exception as e:  # noqa: BLE001 — best-effort, never breaks the fallback
                        sys.stderr.write(
                            f"[drive_live_run] partial-reflection salvage comment failed "
                            f"(task {task_id}): {e}\n"
                        )
            # Blocker-Qualität (Fix B §): post the bridge's OWN classification as a
            # fresh comment BEFORE blocking, so Lead/Operator (via Fix A triage)
            # see the real cause — not a stale reflection quoted from the run-up.
            lifecycle.comment(
                task_id, f"omp-bridge Klassifikation: {cls.reason} — {cls.detail}"
            )
            lifecycle.set_blocker(
                task_id,
                blocker_type=action.blocker_type or "technical_problem",
                question=action.question or cls.detail,
            )
        action.reflection = outcome.reflection_block
        return action


def start_heartbeater(
    api_url: str,
    token: str,
    *,
    interval: float = 30.0,
    _task_active: Optional[Callable[[], bool]] = None,
    _send: Optional[Callable[[str], None]] = None,
    _stop_event: Optional["threading.Event"] = None,
) -> "threading.Event":
    """Daemon-Thread: POST /me/heartbeat wie poll.sh es tut (working/idle).

    Der serve_loop blockt waehrend drive_live_run — ohne eigenen Thread
    friert last_task_activity_at/status waehrend eines Runs ein: die UI
    zeigt den Agent faelschlich offline und der Lifecycle-Watchdog
    (ADR-046) verliert seine Liveness-Basis. Status-Quelle ist der
    Task-Lock (gleiche Semantik wie der omp-recycler). Fehler werden
    geschluckt — Heartbeat darf nie den Driver reissen.
    Returns das Stop-Event (fuer Tests/Shutdown).
    """
    import threading
    import urllib.request

    stop = _stop_event or threading.Event()
    task_active = _task_active or (lambda: os.path.exists(
        os.environ.get("OMP_TASK_LOCK_FILE", "/home/agent/.task-active.lock")
    ))

    def _default_send(status: str) -> None:
        req = urllib.request.Request(
            f"{api_url}/api/v1/agent/me/heartbeat",
            data=json.dumps({"status": status}).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10).read()

    send = _send or _default_send

    def _loop() -> None:
        while not stop.wait(interval):
            try:
                send("working" if task_active() else "idle")
            except Exception:  # noqa: BLE001 — heartbeat is best-effort
                pass

    threading.Thread(target=_loop, name="omp-heartbeater", daemon=True).start()
    return stop


# Path the `mc` CLI reads task context from (mc_cli/config.py:from_env, file
# wins over stale process env). poll.sh writes this for claude agents; the omp
# bridge replaced poll.sh, so we write it here — see write_task_context_env.
MC_CONTEXT_ENV_PATH = "/tmp/mc-context.env"


def write_task_context_env(task: dict, path: str = MC_CONTEXT_ENV_PATH) -> bool:
    """Write the per-dispatch task context the `mc` CLI needs.

    The model's own `mc ack|deliverable|done` calls read TASK_ID / BOARD_ID /
    X_DISPATCH_ATTEMPT_ID via mc_cli/config.py:from_env, which resolves this
    file FIRST (it wins over the process env that still carries the previous
    dispatch's ids). For claude agents poll.sh writes it on every new_task; the
    native-TUI omp bridge dropped poll.sh, so without this the model has no task
    context — `mc ack` fails "TASK_ID … müssen gesetzt sein" and status calls
    are rejected "Missing X-Dispatch-Attempt-Id". Mirrors the exact 3-key
    contract of docker/shared/poll.sh and mc_cli/commands.py:_cmd_recover.

    Best-effort: an unwritable file must never crash the serve loop.
    """
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"TASK_ID={task.get('id') or ''}\n")
            f.write(f"BOARD_ID={task.get('board_id') or ''}\n")
            f.write(f"X_DISPATCH_ATTEMPT_ID={task.get('dispatch_attempt_id') or ''}\n")
        return True
    except OSError as e:  # noqa: BLE001 — context file is best-effort
        sys.stderr.write(f"[serve] mc-context.env write failed: {e}\n")
        return False


# ── Interaction Model 2.0: thread-message consumption (comm_v2) ─────────────
# Python port of the docker/shared/poll.sh message path for the native omp TUI.
# The backend only sends `new_messages` (and only understands `?acked_seq=`) for
# agents with agent.comm_v2=true — so for every other agent this key is ABSENT,
# no queue/ack file is ever written, no acked_seq param is appended, and the
# poll URL + delivery behaviour stay byte-identical to the pre-comm_v2 bridge.
#
# Contract (mirrors poll.sh build_acked_seq_param / _record_ack /
# queue_or_deliver / flush_msg_queue / deliver_messages):
#   1. Queue every incoming message crash-safe as <seq>__<thread_id>.msg BEFORE
#      any delivery, so a crash never drops a message and the ack is only ever
#      set for something actually delivered.
#   2. Flush ONLY at a turn boundary with no dispatch/inject in flight.
#   3. Per message: deliver (native inject) → verify (composer submit) → on
#      success write the per-thread ack high-water + delete the queue file. A
#      failed verify stops the flush with NO ack (at-least-once; the backend
#      redelivers seq > last_acked_seq).
#   4. Ack = only what was truly delivered. Never ack at queue time.

MSG_QUEUE_DIR = os.environ.get("OMP_MSG_QUEUE_DIR", "/home/agent/.msg-queue")
MSG_ACK_DIR = os.environ.get("OMP_MSG_ACK_DIR", "/home/agent/.msg-acked")

# W2.1 nudge+pull (ADR-071, port of poll.sh's deliver_messages_nudge / see
# scripts/grok-bridge.py for the reference impl): in nudge mode the bridge
# injects only a short 📬 wake-up line at the turn gate; the agent pulls the
# actual content itself via `mc inbox`, and the SERVER-side ack from that call
# advances the cursor — the bridge itself never acks locally in this mode.
# Default stays "paste" (byte-identical behaviour) until the per-agent live
# gate has passed. NOTE: the omp container is fed MSG_DELIVERY_MODE=nudge by
# the compose renderer as of PR #148 — this path goes live the moment
# comm_v2 is flipped on for the agent, so it must be correct from day one.
MSG_DELIVERY_MODE = (os.environ.get("MSG_DELIVERY_MODE", "paste").strip() or "paste")
NUDGE_REMIND_SECONDS = float(os.environ.get("NUDGE_REMIND_SECONDS", "600"))
MSG_NUDGE_STATE_FILE = os.environ.get("OMP_MSG_NUDGE_STATE_FILE", "/home/agent/.msg-nudge-state")
MSG_NUDGE_MSG_FILE = os.environ.get("OMP_MSG_NUDGE_MSG_FILE", "/home/agent/.msg-nudge.msg")


def build_acked_seq_param(ack_dir: str = MSG_ACK_DIR) -> str:
    """URL-encoded JSON ``{thread_id: high_water_seq}`` for the poll's
    ``?acked_seq=``. Returns "" when nothing has been delivered yet (no ack
    files) — the caller then appends no query param, so the poll URL is
    byte-identical to the pre-comm_v2 one. Mirrors poll.sh build_acked_seq_param.
    """
    if not os.path.isdir(ack_dir):
        return ""
    try:
        names = os.listdir(ack_dir)
    except OSError:
        return ""
    acked: dict[str, int] = {}
    for tid in names:
        f = os.path.join(ack_dir, tid)
        if not os.path.isfile(f):
            continue
        try:
            with open(f, encoding="utf-8") as fh:
                acked[tid] = int(fh.read().strip())
        except (OSError, ValueError):
            continue
    if not acked:
        return ""
    return urllib.parse.quote(
        json.dumps(acked, separators=(",", ":"), sort_keys=True)
    )


def _record_ack(ack_dir: str, tid: str, seq: int) -> None:
    """Advance the per-thread high-water ack (one file per thread). Only ever
    advances, never regresses (mirrors poll.sh _record_ack)."""
    os.makedirs(ack_dir, exist_ok=True)
    f = os.path.join(ack_dir, tid)
    cur = 0
    try:
        with open(f, encoding="utf-8") as fh:
            cur = int(fh.read().strip() or 0)
    except (OSError, ValueError):
        cur = 0
    if seq > cur:
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(str(seq))


def _format_message(m: dict) -> str:
    """Delivery text for one thread-message — same shape as poll.sh
    queue_or_deliver: a header, the raw body, and the ``[thread … seq …]``
    footer anchor the verify/reply flow keys on."""
    tid = str(m.get("thread_id"))
    seq = int(m["seq"])
    body = m.get("body") or ""
    return "\n".join([
        "# Neue Nachricht (Interaction 2.0)",
        "",
        body,
        "",
        f"[thread {tid} · seq {seq} · von {m.get('sender', '?')} "
        f"· typ {m.get('message_type', '?')}]",
    ])


def queue_messages(payload: Optional[dict], queue_dir: str = MSG_QUEUE_DIR) -> int:
    """Persist each ``new_messages`` entry as ``<seq08d>__<thread_id>.msg`` BEFORE
    any delivery (crash-safe). Idempotent: a redelivered message overwrites the
    same file with identical content. Returns the number written.

    The seq is zero-padded to 8 digits so a plain lexical filename sort equals
    poll.sh's ``sort -n`` numeric order (thread_id is a UUID with no ``_`` so the
    ``__`` split is unambiguous). No-op — and no directory created — when the
    payload carries no ``new_messages`` (the comm_v2=false case)."""
    if not payload or not isinstance(payload, dict):
        return 0
    msgs = payload.get("new_messages") or []
    if not msgs:
        return 0
    os.makedirs(queue_dir, exist_ok=True)
    n = 0
    for m in msgs:
        try:
            seq = int(m["seq"])
            tid = str(m["thread_id"])
        except (KeyError, ValueError, TypeError):
            continue
        fname = f"{seq:08d}__{tid}.msg"
        try:
            with open(os.path.join(queue_dir, fname), "w", encoding="utf-8") as fh:
                fh.write(_format_message(m))
            n += 1
        except OSError as e:  # noqa: BLE001 — one bad write must not abort the rest
            sys.stderr.write(f"[serve] queue_messages write failed: {e}\n")
    return n


def msg_queue_files(queue_dir: str = MSG_QUEUE_DIR) -> list[str]:
    """Queued message basenames, seq-ascending (zero-padded prefix → lexical ==
    numeric)."""
    try:
        names = [n for n in os.listdir(queue_dir) if n.endswith(".msg")]
    except OSError:
        return []
    return sorted(names)


# ── nudge+pull delivery (MSG_DELIVERY_MODE=nudge) ────────────────────────────
# Python port of scripts/grok-bridge.py's deliver_messages_nudge for the native
# omp TUI: same per-thread high-water dedup statefile, same immediate-on-
# higher-seq / remind-after-NUDGE_REMIND_SECONDS semantics, same "never ack
# locally" contract. The one omp-specific difference: omp's inject_file already
# has its own verified composer-submit + retry loop (see NativeTuiController.
# inject_file below), so its bool return IS the verify — no extra pane-token
# scraping is layered on top here.


def _nudge_thread_seqs(messages: list) -> dict[str, int]:
    """Per-thread max seq from a new_messages payload (seq is only unique
    WITHIN a thread — a global max would dedup across threads incorrectly)."""
    per: dict[str, int] = {}
    for m in messages or []:
        try:
            tid = str(m["thread_id"])
            seq = int(m["seq"])
        except (KeyError, TypeError, ValueError):
            continue
        if seq > per.get(tid, 0):
            per[tid] = seq
    return per


def _nudge_state_read(path: str) -> dict[str, tuple[int, float]]:
    """NUDGE_STATE_FILE → {thread_id: (last_nudged_seq, epoch)}. Malformed
    lines are skipped (a corrupt state file must degrade to re-nudging, never
    to crashing the poll loop)."""
    state: dict[str, tuple[int, float]] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return state
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            state[parts[0]] = (int(parts[1]), float(parts[2]))
        except ValueError:
            continue
    return state


def _nudge_state_write(path: str, seqs: dict[str, int], now: float) -> None:
    """Overwrite the state file with the high-water for every currently
    pending thread — one nudge covers ALL of them (the agent reads everything
    via `mc inbox`)."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("".join(f"{tid} {seq} {int(now)}\n" for tid, seq in seqs.items()))


def build_nudge_text(global_max: int, epoch: int) -> str:
    """Single-line wake-up, identical wording to poll.sh / grok-bridge.py."""
    return (
        f"📬 Neue Nachrichten (bis seq {global_max}, {epoch}) — "
        f"lies sie jetzt mit: mc inbox"
    )


def _clear_stale_queue_files(queue_dir: str, log: Callable[[str], None]) -> None:
    """Paste-mode leftovers are dead weight in nudge mode — their bodies are
    never injected, and the server keeps redelivering anything unacked until
    the agent pulls it via `mc inbox`. Deleting them loses nothing."""
    stale = msg_queue_files(queue_dir)
    if not stale:
        return
    for name in stale:
        try:
            os.remove(os.path.join(queue_dir, name))
        except OSError:
            pass
    log(
        f"nudge: {len(stale)} stale paste-mode queue file(s) entfernt "
        f"(Inhalt kommt via mc inbox)."
    )


class _MsgDelivery:
    """comm_v2 thread-message consumer for the native omp TUI.

    Flushes ONE queued message per idle turn boundary through the SAME
    ``inject_file`` + composer-submit verify the task path uses, and writes the
    per-thread ack high-water only after a verified submit. One-at-a-time is
    deliberate: unlike poll.sh (which re-checks a clean-prompt gate per paste),
    the native TUI has no cheap re-checkable idle gate mid-flush — after a
    message is injected the model opens a turn to process it, so the next message
    must wait for THAT turn's terminal ``turn_end`` in the hook signal. We track
    the signal-file byte offset at injection time and hold the gate closed until
    a terminal ``turn_end`` is appended beyond it.

    While a message is being processed we HOLD the recycler's task lock
    (omp-recycler.sh gates every Window-0 relaunch / bridge-respawn on it, see
    ``task_active``): otherwise the recycler would read the idle gap as a dead
    TUI and respawn Window 0 mid-turn, killing the model's response despite the
    ack. The lock is released the moment the processing turn ends.

    The awaiting offset is a byte position into the signal file, so it is
    invalidated whenever a task dispatch truncates the signal to 0 (each
    ``run_native_turn`` calls ``truncate_signal``). Two guards keep that from
    dead-locking the gate: serve_loop calls :meth:`reset_awaiting` when it
    commits to a dispatch, AND :meth:`gate_open` self-heals if it ever sees the
    signal file shorter than the stored offset (it was truncated underneath us)."""

    def __init__(
        self, controller, *, signal_file: str, queue_dir: str, ack_dir: str,
        task_lock_path: str, nudge_state_file: Optional[str] = None,
        nudge_msg_file: Optional[str] = None,
        remind_seconds: float = NUDGE_REMIND_SECONDS,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.ctrl = controller
        self.signal_file = signal_file
        self.queue_dir = queue_dir
        self.ack_dir = ack_dir
        self.task_lock_path = task_lock_path
        self.nudge_state_file = nudge_state_file or MSG_NUDGE_STATE_FILE
        self.nudge_msg_file = nudge_msg_file or MSG_NUDGE_MSG_FILE
        self.remind_seconds = remind_seconds
        self.log = log or (lambda m: sys.stderr.write("[serve] " + m + "\n"))
        # Byte offset of the signal file's EOF at the moment of the last
        # injection; None means "no message-processing turn is in flight".
        self._awaiting_offset: Optional[int] = None
        # True only while WE hold the recycler task lock for a message turn, so
        # we never remove a lock a real task dispatch is holding.
        self._holds_lock: bool = False

    def _signal_size(self) -> int:
        try:
            return os.path.getsize(self.signal_file)
        except OSError:
            return 0

    def _terminal_turn_end_after(self, offset: int) -> bool:
        try:
            with open(self.signal_file, "rb") as fh:
                fh.seek(offset)
                data = fh.read()
        except OSError:
            return False
        for raw in data.split(b"\n"):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw.decode("utf-8", "replace"))
            except (json.JSONDecodeError, ValueError):
                continue
            if (isinstance(obj, dict) and obj.get("kind") == "turn_end"
                    and obj.get("stopReason") in ("stop", "error", "aborted")):
                return True
        return False

    def _acquire_msg_lock(self) -> None:
        """Hold the recycler task lock for the duration of a message turn."""
        try:
            with open(self.task_lock_path, "w", encoding="utf-8") as fh:
                fh.write(str(int(time.time())))
            self._holds_lock = True
        except OSError as e:  # noqa: BLE001 — best-effort recycler gate
            sys.stderr.write(f"[serve] msg task-lock set failed: {e}\n")

    def _release_msg_lock(self) -> None:
        """Release only a lock WE took (never one held by a task dispatch)."""
        if not self._holds_lock:
            return
        try:
            if os.path.exists(self.task_lock_path):
                os.remove(self.task_lock_path)
        except OSError:
            pass
        self._holds_lock = False

    def reset_awaiting(self) -> None:
        """Drop the message-in-flight window (serve_loop calls this when it
        commits to a task dispatch — the dispatch truncates the signal and takes
        over Window 0, so the awaited offset is moot and its lock must not linger
        into the dispatch's own lock management)."""
        self._awaiting_offset = None
        self._release_msg_lock()

    def gate_open(self) -> bool:
        # (1) resolve a message-in-flight window FIRST so a completed (or
        #     truncated-away) processing turn releases our lock before the
        #     dispatch-in-flight check below can trip on it.
        if self._awaiting_offset is not None:
            if self._signal_size() < self._awaiting_offset:
                # Signal truncated underneath us (a task dispatch) — the awaited
                # turn is gone; stop waiting instead of dead-locking forever.
                self.reset_awaiting()
            elif self._terminal_turn_end_after(self._awaiting_offset):
                self._awaiting_offset = None
                self._release_msg_lock()
            else:
                return False  # model still processing our last message
        # (2) no dispatch / task-run in flight (the recycler's task lock).
        if os.path.exists(self.task_lock_path):
            return False
        return True

    def flush(self) -> None:
        """Never raises — a queue/tmux hiccup on a real message must not crash
        the poll loop (which would take the whole agent down)."""
        try:
            self._flush()
        except Exception as e:  # noqa: BLE001 — swallow, log, keep polling
            self.log(f"deliver_messages: flush error (swallowed): {type(e).__name__}: {e}")

    def _flush(self) -> None:
        pending = msg_queue_files(self.queue_dir)
        if not pending:
            return
        if not self.gate_open():
            self.log(
                f"deliver_messages: Gate zu (omp arbeitet / Dispatch in flight) — "
                f"{len(pending)} Message(s) bleiben gequeued, kein Ack."
            )
            return
        fname = pending[0]
        path = os.path.join(self.queue_dir, fname)
        seq_str, _, rest = fname.partition("__")
        tid = rest[:-4] if rest.endswith(".msg") else rest
        try:
            seq = int(seq_str)
        except ValueError:
            self._safe_remove(path)  # malformed name — never let it wedge the queue
            return
        offset_before = self._signal_size()
        # Take the recycler lock BEFORE injecting so the model's processing turn
        # is protected from a mid-turn respawn even if inject is slow.
        self._acquire_msg_lock()
        if self.ctrl.inject_file(path):
            _record_ack(self.ack_dir, tid, seq)
            self._safe_remove(path)
            # Hold the gate closed (and the lock held) until the turn ends.
            self._awaiting_offset = offset_before
            remaining = len(pending) - 1
            self.log(
                f"deliver_messages: 1 Message zugestellt (thread {tid}, seq {seq}), "
                f"ack bis seq {seq}; {remaining} bleiben gequeued."
            )
        else:
            # Nothing was submitted → no processing turn → release the lock now.
            self._release_msg_lock()
            self.log(
                f"deliver_messages: Zustellung fuer seq {seq} (thread {tid}) "
                f"FEHLGESCHLAGEN (Verify) — Flush gestoppt, bleibt gequeued, kein Ack."
            )

    def _safe_remove(self, path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass

    def nudge(self, messages: list) -> None:
        """Never raises — a queue/tmux hiccup on a real message must not
        crash the poll loop (which would take the whole agent down)."""
        try:
            self._nudge(messages)
        except Exception as e:  # noqa: BLE001 — swallow, log, keep polling
            self.log(f"nudge: error (swallowed): {type(e).__name__}: {e}")

    def _nudge(self, messages: list) -> None:
        """Nudge-mode delivery (port of poll.sh's / grok-bridge.py's
        deliver_messages_nudge): decide per thread whether a wake-up is due
        (new higher seq → immediately; no progress after remind_seconds →
        remind), inject ONE short line at the turn gate via the same
        inject_file() the task path uses, then record the nudged high-water
        for every pending thread. NEVER acks — the server-side cursor only
        advances through the agent's own `mc inbox` call, and the backend
        redelivers `new_messages` until then (at-least-once)."""
        _clear_stale_queue_files(self.queue_dir, self.log)
        seqs = _nudge_thread_seqs(messages)
        if not seqs:
            # Everything fetched+acked by the agent — reset so the next
            # message nudges immediately again.
            try:
                os.remove(self.nudge_state_file)
            except OSError:
                pass
            return

        now = time.time()
        state = _nudge_state_read(self.nudge_state_file)
        do_nudge = False
        for tid, mseq in seqs.items():
            last_seq, last_ts = state.get(tid, (0, 0.0))
            if mseq > last_seq:
                do_nudge = True  # new, higher seq in THIS thread → wake up now
            elif (now - last_ts) >= self.remind_seconds:
                do_nudge = True  # remind: still unacked after the grace window
        if not do_nudge:
            return

        global_max = max(seqs.values())
        if not self.gate_open():
            self.log(
                f"nudge: Gate zu (omp arbeitet / Dispatch in flight) — "
                f"Nudge aufgeschoben (bis seq={global_max})."
            )
            return

        epoch = int(now)
        text = build_nudge_text(global_max, epoch)
        try:
            parent = os.path.dirname(self.nudge_msg_file)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(self.nudge_msg_file, "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError as e:
            self.log(f"nudge: Schreiben der Nudge-Datei fehlgeschlagen (swallowed): {e}")
            return

        offset_before = self._signal_size()
        self._acquire_msg_lock()
        if self.ctrl.inject_file(self.nudge_msg_file):
            _nudge_state_write(self.nudge_state_file, seqs, now)
            # Hold the gate closed (and the lock held) until the turn ends —
            # same as a normal queued message: the agent's mc inbox call opens
            # a real processing turn.
            self._awaiting_offset = offset_before
            self.log(
                f"nudge: injiziert (bis seq {global_max}) — "
                f"Agent holt Inhalt via 'mc inbox'."
            )
        else:
            # Nothing was submitted → no processing turn → release the lock
            # now, and leave the state file untouched so the next poll retries.
            self._release_msg_lock()
            self.log(
                f"nudge: Zustellung fehlgeschlagen (Verify, bis seq {global_max}) — "
                f"Retry beim naechsten Poll (State unveraendert)."
            )


def serve_loop(
    *,
    poll_interval: float = 5.0,
    max_iterations: Optional[int] = None,
    _poll_fn: Optional[Callable[[], Optional[dict]]] = None,
    _lifecycle_factory: Optional[Callable[[dict], MCLifecycle]] = None,
    _run_factory: Optional[Callable[[dict, str], Callable[[], RunOutcome]]] = None,
    _continue_factory: Optional[Callable[[dict, str], Callable[[str], RunOutcome]]] = None,
    _sleep: Callable[[float], None] = time.sleep,
    _context_env_path: str = MC_CONTEXT_ENV_PATH,
    _msg_queue_dir: Optional[str] = None,
    _msg_ack_dir: Optional[str] = None,
    _task_lock_path: Optional[str] = None,
    _nudge_state_file: Optional[str] = None,
    _nudge_msg_file: Optional[str] = None,
) -> int:
    """Persistent poll→native-TUI→lifecycle driver (ADR-049, supersedes the
    ADR-045 headless one-shot serve path).

    Ported skeleton of scripts/hermes-bridge.py:dispatch_poll_loop — same
    `GET /me/poll` contract + dispatch-dedup cache — but the delivery step no
    longer spawns `omp -p`. It injects the task into the persistent native omp
    TUI (tmux Window 0) and reads the turn-end hook signal (see
    run_native_turn). The reduced RunOutcome flows through the SAME
    classify()/decide_lifecycle()/drive_live_run() as before, so ack/finish/
    blocked + the finish→blocked fallback are unchanged.

    Window 1 (this loop) prints OMP_BRIDGE_READY once after the first poll — a
    Window-1 liveness log. NOTE: the health-gate now scrapes Window 0 (the TUI),
    whose readiness anchor is the TUI chat glyph, not this sentinel (ADR-049 §5).
    The `_*` params are injection seams for unit tests.
    """
    api_url = os.environ.get("MC_API_URL", "http://backend:8000").rstrip("/")
    token = os.environ.get("MC_AGENT_TOKEN", "")
    require_review = os.environ.get("OMP_REQUIRE_REVIEW", "1") not in ("0", "false", "")
    retries = int(os.environ.get("OMP_MAX_RETRIES", str(OMP_MAX_RETRIES)))
    continues = int(os.environ.get("OMP_MAX_CONTINUES", str(OMP_MAX_CONTINUES)))

    # Native-TUI knobs (all overridable via env).
    session = os.environ.get("AGENT_NAME", "omp-agent")
    tui_window = os.environ.get("OMP_TUI_WINDOW", "0")
    signal_file = os.environ.get(
        "OMP_TURN_SIGNAL_FILE",
        os.path.join(os.environ.get("OMP_HOME", "/home/agent/.omp"), "turn-signal.ndjson"),
    )
    launcher = os.environ.get("OMP_LAUNCHER", "/opt/omp-bridge/launch-omp.sh")
    ready_timeout = float(os.environ.get("OMP_READY_TIMEOUT", "45"))
    turn_deadline = float(os.environ.get("OMP_TASK_DEADLINE", os.environ.get("OMP_MAX_TIME", "1200")))
    idle_timeout = float(os.environ.get("OMP_TURN_IDLE_TIMEOUT", "300"))
    isolation = os.environ.get("OMP_ISOLATION", "relaunch")  # relaunch | slash

    # comm_v2 thread-message state dirs (env-overridable, per-container).
    msg_queue_dir = _msg_queue_dir or MSG_QUEUE_DIR
    msg_ack_dir = _msg_ack_dir or MSG_ACK_DIR
    task_lock_path = _task_lock_path or os.environ.get("OMP_TASK_LOCK_FILE", TASK_LOCK_FILE)
    nudge_state_file = _nudge_state_file or MSG_NUDGE_STATE_FILE
    nudge_msg_file = _nudge_msg_file or MSG_NUDGE_MSG_FILE

    # One controller for the container's lifetime; run_native_turn relaunches +
    # truncates the signal per task, so state never bleeds between tasks.
    tui = NativeTuiController(session=session, signal_file=signal_file, window=tui_window,
                              launcher=launcher)

    delivery = _MsgDelivery(
        tui, signal_file=signal_file, queue_dir=msg_queue_dir,
        ack_dir=msg_ack_dir, task_lock_path=task_lock_path,
        nudge_state_file=nudge_state_file, nudge_msg_file=nudge_msg_file,
    )

    poll_fn = _poll_fn or _make_http_poll(api_url, token, ack_dir=msg_ack_dir)
    if _poll_fn is None:
        # Nur im echten Betrieb (Tests injizieren poll_fn und brauchen
        # keinen Netzwerk-Thread).
        start_heartbeater(api_url, token)
    last_attempt_id: Optional[str] = None
    ready_printed = False
    iterations = 0

    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        try:
            payload = poll_fn()
        except Exception as e:  # noqa: BLE001 — never crash the loop on a poll error
            sys.stderr.write(f"[serve] poll error: {type(e).__name__}: {e}\n")
            payload = None

        if not ready_printed:
            # Anchor readiness on the FIRST completed poll round-trip.
            print("OMP_BRIDGE_READY", flush=True)
            ready_printed = True

        state = (payload or {}).get("state")
        task = (payload or {}).get("task") if state == "new_task" else None

        if state in ("idle", "cancelled", "stopped"):
            last_attempt_id = None  # clear dedup so a re-opened task dispatches

        # comm_v2 delivery: nudge mode short-circuits the queue entirely — it
        # never persists message bodies, only per-thread high-water seqs (the
        # content comes back to the agent via its own `mc inbox` call). paste
        # mode (default) persists any thread-messages crash-safe BEFORE
        # dispatch (so a crash mid-task can't lose them). Both are no-ops — no
        # dir/state file created — when the payload has no `new_messages`
        # (comm_v2=false → byte-identical). Wrapped so a hiccup on a real
        # message can never crash the poll loop (which would take the whole
        # agent down).
        new_messages = (payload or {}).get("new_messages") if isinstance(payload, dict) else None
        if MSG_DELIVERY_MODE != "nudge":
            try:
                queue_messages(payload, msg_queue_dir)
            except Exception as e:  # noqa: BLE001 — log + keep polling
                sys.stderr.write(
                    f"[serve] queue_messages error (swallowed): {type(e).__name__}: {e}\n"
                )

        def _deliver_at_boundary() -> None:
            # Review fix (2026-07-23): the nudge must fire at the SAME points
            # where paste-mode flushes — post-dispatch / idle turn boundary —
            # never BEFORE the dispatch branch. A payload carrying new_task
            # AND new_messages would otherwise nudge first, mark the state
            # file as nudged, and then have run_native_turn relaunch Window 0
            # and kill the nudge turn before the agent could run `mc inbox`
            # (no re-nudge until NUDGE_REMIND_SECONDS). Also keeps stale
            # paste-mode queue files from ever being flushed in nudge mode.
            if MSG_DELIVERY_MODE == "nudge":
                if new_messages is not None:
                    delivery.nudge(new_messages)
            else:
                delivery.flush()

        if task and task.get("id"):
            attempt_id = task.get("dispatch_attempt_id") or task["id"]
            if attempt_id == last_attempt_id:
                # Dedup: same dispatch already handled — this is an idle turn
                # boundary, so deliver thread-messages (nudge or flush), then
                # sleep. Do NOT re-run the task.
                _deliver_at_boundary()
                _sleep(poll_interval)
                continue
            last_attempt_id = attempt_id
            # A task is taking over Window 0 (it will truncate the turn signal):
            # drop any pending message-in-flight window so its now-stale byte
            # offset can't dead-lock the gate afterwards.
            delivery.reset_awaiting()

            # Hydrate the task context the model's own `mc` calls read. Must
            # happen BEFORE the run (and its ack) so the very first `mc ack`
            # the model issues already has TASK_ID/BOARD_ID/attempt-id.
            write_task_context_env(task, _context_env_path)

            cwd = container_workspace_path(task.get("workspace_path")) or "/workspace"
            prompt = wrap_prompt(task.get("prompt") or task.get("title") or "")

            if _lifecycle_factory is not None:
                lifecycle: MCLifecycle = _lifecycle_factory(task)
            else:
                lifecycle = McCliLifecycle(
                    api_url=api_url, token=token, task_id=str(task["id"]),
                    board_id=task.get("board_id"), attempt_id=task.get("dispatch_attempt_id"),
                )

            continue_once: Optional[Callable[[str], RunOutcome]] = _continue_factory(task, cwd) \
                if _continue_factory is not None else None
            if _run_factory is not None:
                run_once = _run_factory(task, cwd)
            else:
                task_file = _task_file_for(str(task["id"]))
                _isolate = isolation != "slash"

                def run_once(_cwd=cwd, _p=prompt, _tf=task_file, _iso=_isolate) -> RunOutcome:
                    return run_native_turn(
                        tui, cwd=_cwd, prompt=_p, task_file_path=_tf, isolate=_iso,
                        ready_timeout=ready_timeout, turn_deadline=turn_deadline,
                        idle_timeout=idle_timeout,
                    )

                # Continue-Nudge (Fix B): resume the SAME TUI session (no relaunch,
                # so context survives) with the wrapped nudge follow-up.
                # include_identity=False — the session already has the CARD.md
                # identity block from the first-dispatch prompt above; re-
                # prepending it on every nudge (up to OMP_MAX_CONTINUES times)
                # would just re-burn context restating what the live session
                # already carries.
                def continue_once(nudge: str, _cwd=cwd, _tf=task_file) -> RunOutcome:
                    return run_native_continue(
                        tui, cwd=_cwd,
                        nudge_prompt=wrap_prompt(nudge, include_identity=False),
                        task_file_path=_tf,
                        turn_deadline=turn_deadline, idle_timeout=idle_timeout,
                    )

            _set_task_lock(True)
            try:
                # Claim the task up front: stamp ack_at before the (possibly long)
                # omp run so the 10-min ACK-timeout re-dispatch can't fire and so
                # `mc finish` sees the in_progress precondition.
                lifecycle.ack(str(task["id"]))
                drive_live_run(
                    lifecycle, run_once,
                    task_id=str(task["id"]),
                    board_requires_review=require_review,
                    retries_left=retries,
                    pre_acked=True,
                    continues_left=continues,
                    continue_once=continue_once,
                )
            except Exception as e:  # noqa: BLE001 — resolve terminally, never hang
                sys.stderr.write(f"[serve] run error: {type(e).__name__}: {e}\n")
                try:
                    lifecycle.set_blocker(
                        str(task["id"]),
                        blocker_type="technical_problem",
                        question=f"omp-bridge Laufzeitfehler: {type(e).__name__}: {e}",
                    )
                except Exception:  # pragma: no cover
                    pass
            finally:
                _set_task_lock(False)

        # comm_v2: post-dispatch OR no-task idle turn boundary — deliver at
        # most one queued thread-message (paste) or a single nudge (gate
        # re-checked inside). No-op when there is nothing pending
        # (comm_v2=false → byte-identical).
        _deliver_at_boundary()
        _sleep(poll_interval)

    return 0


def _default_model_selector(openai_model: Optional[str]) -> str:
    """Build omp's fully-qualified `<provider>/<model>` selector.

    entrypoint.sh renders the `mc-openai` provider from OPENAI_MODEL, so the
    selector is `mc-openai/<OPENAI_MODEL>`. Never a bare token — that
    mis-resolves against omp's built-in provider catalog. No baked-in
    default: a missing model is a boot error, not a silent fallback to a
    stale model (ADR-054).
    """
    m = (openai_model or "").strip()
    if not m:
        raise RuntimeError(
            "OPENAI_MODEL not set — entrypoint must render models.yml first"
        )
    if "/" in m and m.split("/", 1)[0] in ("mc-openai", "lm-studio", "openai"):
        return m  # already provider-qualified
    return f"mc-openai/{m}"


def _make_http_poll(
    api_url: str, token: str, ack_dir: str = MSG_ACK_DIR,
) -> Callable[[], Optional[dict]]:
    import urllib.request

    url = f"{api_url}/api/v1/agent/me/poll"
    headers = {"Authorization": f"Bearer {token}"}

    def _poll() -> Optional[dict]:
        # comm_v2: report the per-thread delivered high-water so the backend
        # only redelivers seq > last_acked_seq. Empty (no acks yet / comm_v2=off)
        # → no query param → byte-identical URL. Recomputed every poll because
        # the ack-store advances as messages are delivered.
        enc = build_acked_seq_param(ack_dir)
        full = f"{url}?acked_seq={enc}" if enc else url
        req = urllib.request.Request(full, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8")
        return json.loads(body) if body.strip() else None

    return _poll


# ---------------------------------------------------------------------------
# Native-TUI driver (ADR-049) — inject into the persistent omp TUI + read the
# turn-end hook signal instead of spawning a headless `omp -p` per task.
# ---------------------------------------------------------------------------
#
# The reducer/classifier/lifecycle above stay UNCHANGED — they are the proven
# taxonomy. What changes is HOW a run is produced: rather than spawning
# `omp -p --mode json` and reducing its NDJSON stdout, we drive the long-lived
# native TUI in tmux Window 0:
#   1. relaunch Window 0 with the task's --cwd (per-task isolation + fresh ctx),
#   2. inject the dispatch as an `@/abs/file` mention via `tmux send-keys`
#      (no bracketed-paste of the multi-line body),
#   3. tail the turn-end-hook signal file for THIS task's terminal turn,
#   4. fold the hook events into a RunOutcome and hand it to the SAME
#      classify()/decide_lifecycle()/drive_live_run() as the headless path.
#
# turn_end.stopReason mapping (verified against omp v16.2.13):
#   stop            -> terminal; apply completion contract (finish|silent-abort)
#   error | aborted -> terminal; error family (retry-then-blocker)
#   toolUse | length-> agent continues (tools / auto-compaction) — keep waiting
# Watchdog (non-negotiable): no terminal turn within the per-task deadline, a
# no-progress idle timeout, OR the TUI child dying -> watchdog_killed -> the
# supervisor SIGKILLs+relaunches the TUI and the task ends blocked (ABORT_HANG),
# never left in_progress.


class NativeTuiController:
    """Drive + observe the persistent native omp TUI (tmux Window 0).

    All tmux calls funnel through ``_run`` and all hook events through the
    signal file, both injectable so the driver is unit-testable with neither a
    real tmux server nor a real omp process.
    """

    def __init__(
        self,
        *,
        session: str,
        signal_file: str,
        window: str = "0",
        launcher: str = "/opt/omp-bridge/launch-omp.sh",
        _run: Optional[Callable[[list[str]], tuple[int, str]]] = None,
        _pid_alive: Optional[Callable[[int], bool]] = None,
        _sleep: Optional[Callable[[float], None]] = None,
        key_delay: float = 0.35,
    ) -> None:
        self.session = session
        self.window = window
        self.target = f"{session}:{window}"
        self.signal_file = signal_file
        self.launcher = launcher
        self._run = _run or self._default_run
        self._pid_alive = _pid_alive or _os_pid_alive
        self._sleep = _sleep or time.sleep
        self.key_delay = key_delay
        self._offset = 0

    @staticmethod
    def _default_run(args: list[str]) -> tuple[int, str]:
        try:
            proc = subprocess.run(
                ["tmux", *args], capture_output=True, text=True, timeout=15
            )
            return proc.returncode, (proc.stdout or "")
        except Exception as e:  # noqa: BLE001 — a tmux hiccup must not crash the loop
            sys.stderr.write(f"[native] tmux {args[:2]} failed: {type(e).__name__}: {e}\n")
            return 1, ""

    # -- drive ---------------------------------------------------------------

    def relaunch(self, cwd: str) -> None:
        """Kill + respawn Window 0 with the task cwd (isolation + fresh ctx)."""
        cmd = f"exec {self.launcher} {shlex.quote(cwd)}"
        self._run(["respawn-window", "-k", "-t", self.target, cmd])

    def inject_file(
        self, path: str, *, max_paste_attempts: int = 2,
        verify_attempts: int = 3, verify_wait: float = 1.0,
        retry_backoff: float = 5.0,
    ) -> bool:
        """Inject the dispatch as an `@/abs/path` mention (no body paste).

        Verified in-container (omp v16.2.13 TUI, manual repro 2026-07-12
        12:23 on Sparky): typing `@<existing-path>` opens a file-mention
        AUTOCOMPLETE POPUP that swallows a bare Enter, so a single Enter
        never submits. The robust sequence is:
          1. type `@path`      -> popup appears, text in the input box,
          2. `Escape`          -> dismisses the popup, KEEPS the `@path` text,
          3. `Enter`           -> submits; omp resolves `@path` -> Read(file).
        Escape is a no-op when no popup is showing, so this is safe either
        way. Also empirically confirmed on that same repro: the composer's
        bottom border line (`╰─...─╯`) shows the `@path` fragment BEFORE
        submit and is blank (only border/space) AFTER — see
        `_composer_state()`. Small key delays let the TUI process each event
        in order.

        Bug B (2026-07-12, live incident): the original version fired these
        three `send-keys` calls and returned unconditionally — no return-code
        check, no verification the text actually left the composer. A
        transient `subprocess.run(..., timeout=15)` TimeoutExpired inside
        `_run` (swallowed there, returns rc=1) then silently no-op'd: the
        task stayed `in_progress`, the agent "working", the TUI sitting at
        an empty Welcome screen forever.

        Bug B follow-up (review, same day): the first verification attempt
        scanned the last 400 chars of the WHOLE pane for the literal
        `@path` string — two failure modes: (1) FALSE NEGATIVE — omp echoes
        the submitted user message back into the transcript above the
        composer, so a successful submit could still show `@path` in the
        tail, causing up to `max_paste_attempts` redundant re-submissions of
        the SAME task (duplicate dispatch, worse than the original bug);
        (2) FALSE POSITIVE — a long path wraps across composer lines, so a
        literal full-string match can miss a still-pending mention. Fixed by
        checking ONLY the composer's own bottom-border line via
        `_composer_state()`, matched against a short tail FRAGMENT of the
        path (robust to line-wrapping — the filename tail is what lands on
        the last visual line), and by hard-capping `@path` re-sends at
        `max_paste_attempts` (default 2): the swallowed-Enter fallback
        (bare Enter, no retyping) is cheap and idempotent — Enter on an
        already-empty composer is a no-op — but a re-paste is the actual
        duplicate-dispatch risk, so it only fires on POSITIVE evidence the
        previous paste is still sitting there unsubmitted, never on an
        "unclear" (failed/blank) capture-pane read.

        Returns True once verified submitted, False if it definitively
        failed — the caller MUST treat False as a hard failure (see
        `_native_watchdog_kill` in run_native_turn / run_native_continue,
        which escalates to the existing blocked-task fallback instead of
        waiting out the multi-minute idle watchdog).
        """
        fragment = ("@" + path)[-24:]
        paste_count = 0

        while paste_count < max_paste_attempts:
            self._run(["send-keys", "-t", self.target, "--", f"@{path}"])
            self._sleep(self.key_delay)
            self._run(["send-keys", "-t", self.target, "Escape"])
            self._sleep(self.key_delay)
            self._run(["send-keys", "-t", self.target, "Enter"])
            self._sleep(self.key_delay)
            paste_count += 1

            state = self._composer_state(fragment, verify_attempts, verify_wait)
            if state == "submitted":
                return True

            if state == "pending":
                # Swallowed-Enter fallback: cheap + idempotent (Enter on an
                # empty composer is a no-op), does NOT count against the
                # paste cap, no retyping.
                self._run(["send-keys", "-t", self.target, "Enter"])
                self._sleep(self.key_delay)
                state = self._composer_state(fragment, verify_attempts, verify_wait)
                if state == "submitted":
                    return True

            if state == "unclear":
                # capture-pane never produced a clean read (TUI redraw) —
                # do NOT re-paste on ambiguity (duplicate-dispatch risk).
                # One more bounded wait-and-check; only a POSITIVE "pending"
                # read earns a re-paste.
                self._sleep(retry_backoff)
                state = self._composer_state(fragment, verify_attempts, verify_wait)
                if state == "submitted":
                    return True
                if state != "pending":
                    break  # still unclear -> give up, do not guess

            if paste_count < max_paste_attempts:
                sys.stderr.write(
                    f"[native] inject_file: composer still shows the pending "
                    f"mention after paste {paste_count}/{max_paste_attempts} "
                    f"(target={self.target}) — re-pasting in {retry_backoff}s\n"
                )
                self._sleep(retry_backoff)

        sys.stderr.write(
            f"[native] inject_file: FAILED after {paste_count} paste attempt(s) "
            f"(target={self.target}) — giving up, caller must escalate\n"
        )
        return False

    _COMPOSER_BOTTOM_PREFIX = "╰─"  # '╰─' — composer bottom-border edge
    _COMPOSER_BORDER_CHARS = "╰╯─│╭╮ "  # ╰╯─│╭╮ + space

    def _composer_state(
        self, fragment: str, verify_attempts: int, verify_wait: float,
    ) -> str:
        """Read the composer's bottom-border line and classify it.

        What is EMPIRICALLY verified (manual repro 2026-07-12 12:23, Sparky
        pane, live repair of a stuck task): the Escape-then-Enter sequence
        submits an `@path` mention out of the file-mention autocomplete
        popup, and the composer's bottom-border line (`╰─...─╯`) visibly
        contains the `@path` text before that submit and is blank
        (border/space only) immediately after. That direct observation is
        exactly the two-way "pending" vs "submitted" split below.

        What is a DEFENSIVE DESIGN CHOICE, not something separately
        observed: the `verify_attempts` capture-retry loop and the
        "unclear" state. A capture-pane call returning empty output DID
        happen live during that same repro (a TUI-redraw timing gap) — but
        whether every blank capture always means "mid-redraw" specifically
        (vs. some other transient tmux hiccup) isn't verified; treating it
        as "unclear" and retrying the capture rather than the paste is the
        conservative choice given that ambiguity, not a confirmed root
        cause.

        Returns "submitted" (border line is blank besides frame chars),
        "pending" (the `fragment` tail of our `@path` is still sitting in
        it, OR the border line holds unrecognized non-blank content — never
        guess "submitted" from an ambiguous read), or "unclear" (capture-pane
        failed/blank for `verify_attempts` tries in a row, or no
        composer-border line was found at all in an otherwise valid
        capture). Retries the CAPTURE (not the injection) up to
        `verify_attempts` times, only while it keeps coming back
        blank/failed.
        """
        out = None
        for _ in range(max(1, verify_attempts)):
            rc, captured = self._run(["capture-pane", "-t", self.target, "-p"])
            if rc == 0 and captured and captured.strip():
                out = captured
                break
            self._sleep(verify_wait)
        if out is None:
            return "unclear"

        composer_line = None
        for line in out.splitlines():
            if line.strip().startswith(self._COMPOSER_BOTTOM_PREFIX):
                composer_line = line
        if composer_line is None:
            return "unclear"

        content = composer_line.strip(self._COMPOSER_BORDER_CHARS).strip()
        if not content:
            return "submitted"
        if fragment in composer_line:
            return "pending"
        # Non-blank border line that doesn't match our fragment — some other
        # state we don't recognize. Never guess "submitted" from this.
        return "pending"

    def reset_conversation(self) -> None:
        """`/new` slash-reset (same process, same cwd). Kept as an alternative
        to relaunch() for same-cwd reuse; relaunch is the default because it
        also rebinds --cwd, which /new cannot."""
        self._run(["send-keys", "-t", self.target, "--", "/new"])
        self._run(["send-keys", "-t", self.target, "Enter"])

    def child_pid(self) -> Optional[int]:
        rc, out = self._run(
            ["list-panes", "-t", self.target, "-F", "#{pane_pid}"]
        )
        if rc == 0:
            for line in (out or "").splitlines():
                line = line.strip()
                if line.isdigit():
                    return int(line)
        return None

    def child_alive(self) -> bool:
        pid = self.child_pid()
        return pid is not None and self._pid_alive(pid)

    # -- observe -------------------------------------------------------------

    def truncate_signal(self) -> None:
        """Clear the hook signal file at the start of a task so the tail only
        sees THIS task's events; reset the read offset."""
        try:
            open(self.signal_file, "w", encoding="utf-8").close()
        except OSError as e:  # pragma: no cover — defensive
            sys.stderr.write(f"[native] signal truncate failed: {e}\n")
        self._offset = 0

    def drain(self) -> list[dict]:
        """Return hook records appended since the last drain (complete lines
        only — a half-written trailing line is left for the next poll)."""
        try:
            with open(self.signal_file, "rb") as fh:
                fh.seek(self._offset)
                data = fh.read()
        except FileNotFoundError:
            return []
        nl = data.rfind(b"\n")
        if nl == -1:
            return []
        chunk, self._offset = data[: nl + 1], self._offset + nl + 1
        recs: list[dict] = []
        for raw in chunk.split(b"\n"):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw.decode("utf-8", "replace"))
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(obj, dict):
                recs.append(obj)
        return recs

    def wait_for(
        self,
        kinds: Iterable[str],
        *,
        timeout: float,
        poll_interval: float,
        now: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> Optional[dict]:
        kinds = set(kinds)
        deadline = now() + timeout
        while now() < deadline:
            for rec in self.drain():
                if rec.get("kind") in kinds:
                    return rec
            sleep(poll_interval)
        return None


def _os_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _write_task_file(path: str, body: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)


def _native_watchdog_kill(
    controller: NativeTuiController, outcome: RunOutcome, cwd: str
) -> RunOutcome:
    """SIGKILL + relaunch the TUI (respawn-window -k) so a wedged / dead omp is
    never left in Window 0, and flag the run so classify()->ABORT_HANG
    (retryable; exhausted -> terminal blocker). Never left in_progress."""
    outcome.watchdog_killed = True
    try:
        controller.relaunch(cwd)
    except Exception as e:  # noqa: BLE001 — recovery must not raise past here
        sys.stderr.write(f"[native] watchdog relaunch failed: {e}\n")
    return outcome


def _observe_native_turn(
    controller: NativeTuiController,
    outcome: RunOutcome,
    *,
    cwd: str,
    turn_deadline: float,
    idle_timeout: float,
    poll_interval: float,
    now: Callable[[], float],
    sleep: Callable[[float], None],
) -> RunOutcome:
    """Tail the hook signal for THIS turn's terminal turn_end, folding it into
    `outcome`. Shared by the initial turn (run_native_turn) and a continue-nudge
    (run_native_continue). Owns the out-of-band watchdog: no terminal turn within
    the deadline, a no-progress idle, OR the TUI child dying -> watchdog_killed +
    SIGKILL/relaunch, never a silent in_progress."""
    start = now()
    last_progress = now()
    last_sr: Optional[str] = None
    last_text = ""
    last_err: Optional[str] = None
    last_toolerr = False

    while True:
        for rec in controller.drain():
            kind = rec.get("kind")
            if kind in ("progress", "session_start", "hook_ready"):
                last_progress = now()
                if kind == "session_start":
                    outcome.saw_session = True
                continue
            if kind == "turn_end":
                last_progress = now()
                outcome.turns += 1
                sr = rec.get("stopReason")
                last_sr = sr
                last_text = rec.get("text") or ""
                last_err = rec.get("errorMessage")
                last_toolerr = bool(rec.get("toolError"))
                if sr == "stop":
                    outcome.saw_agent_end = True
                    outcome.final_stop_reason = "stop"
                    outcome.final_text = last_text
                    outcome.last_turn_had_tool_error = last_toolerr
                    if last_err:
                        outcome.error_message = last_err
                    return outcome
                if sr in ("error", "aborted"):
                    outcome.saw_agent_end = True
                    outcome.final_stop_reason = "error"
                    outcome.error_message = last_err or (
                        "omp-Turn abgebrochen (aborted)." if sr == "aborted"
                        else "Modell/Provider-Fehler."
                    )
                    return outcome
                # toolUse | length | anything else -> agent continues; wait on.
                continue
            if kind == "agent_end":
                outcome.saw_agent_end = True
                if last_sr == "stop":
                    outcome.final_stop_reason = "stop"
                    outcome.final_text = last_text
                    outcome.last_turn_had_tool_error = last_toolerr
                else:
                    # Response ended without a clean stop (length-truncated /
                    # gave up) -> incomplete -> error family -> blocker.
                    outcome.final_stop_reason = "error"
                    outcome.error_message = last_err or (
                        f"Antwort endete ohne sauberen stop (letzter stopReason={last_sr})."
                    )
                return outcome

        # --- watchdog (out of band vs the model): child-death, wall-clock, idle
        t = now()
        if not controller.child_alive():
            return _native_watchdog_kill(controller, outcome, cwd)
        if t - start > turn_deadline:
            return _native_watchdog_kill(controller, outcome, cwd)
        if idle_timeout and (t - last_progress) > idle_timeout:
            return _native_watchdog_kill(controller, outcome, cwd)
        sleep(poll_interval)


def run_native_turn(
    controller: NativeTuiController,
    *,
    cwd: str,
    prompt: str,
    task_file_path: str,
    isolate: bool = True,
    ready_timeout: float = 45.0,
    turn_deadline: float = 1200.0,
    idle_timeout: float = 300.0,
    poll_interval: float = 1.0,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> RunOutcome:
    """One inject→observe cycle against the native TUI → a RunOutcome.

    Shaped exactly like ``run_omp_subprocess`` so it slots into the UNCHANGED
    ``drive_live_run`` retry-then-blocker policy: a retry simply re-invokes this
    (which relaunches the TUI and re-injects — a clean, isolated redo).
    """
    outcome = RunOutcome()

    # 1. Per-task isolation + correct cwd: a fresh TUI conversation.
    if isolate:
        controller.relaunch(cwd)
    controller.truncate_signal()

    # 2. Wait for the fresh session's hook to load (hook_ready / session_start).
    ready = controller.wait_for(
        ("session_start", "hook_ready"),
        timeout=ready_timeout, poll_interval=poll_interval, now=now, sleep=sleep,
    )
    if ready is None and not controller.child_alive():
        # TUI never came up (relaunch failed / hook never loaded) — a hang the
        # supervisor must recover from, not a silent in_progress.
        return _native_watchdog_kill(controller, outcome, cwd)
    if ready is not None:
        outcome.saw_session = True
        outcome.saw_agent_start = True

    # 3. Inject the wrapped dispatch via an @file mention (no paste of the body).
    _write_task_file(task_file_path, prompt)
    if not controller.inject_file(task_file_path):
        # Bug B: injection definitively failed (retries exhausted) — escalate
        # immediately via the same watchdog-kill path used for a dead/wedged
        # TUI, instead of silently waiting out the multi-minute idle timeout
        # with nothing ever having been typed. Feeds the existing
        # retry-then-blocker policy in drive_live_run.
        return _native_watchdog_kill(controller, outcome, cwd)

    # 4. Tail the hook signal for this task's terminal turn.
    return _observe_native_turn(
        controller, outcome, cwd=cwd, turn_deadline=turn_deadline,
        idle_timeout=idle_timeout, poll_interval=poll_interval, now=now, sleep=sleep,
    )


def run_native_continue(
    controller: NativeTuiController,
    *,
    cwd: str,
    nudge_prompt: str,
    task_file_path: str,
    turn_deadline: float = 1200.0,
    idle_timeout: float = 300.0,
    poll_interval: float = 1.0,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> RunOutcome:
    """Continue-Nudge (Fix B): re-inject a follow-up prompt into the ALREADY-LIVE
    native TUI session — NO relaunch, so the model keeps its context (that is the
    whole point vs a full retry, which relaunches fresh). Folds the next terminal
    turn into a RunOutcome via the SAME observe/watchdog core + classify() taxonomy.
    """
    outcome = RunOutcome()

    # The session is known-live from the turn we are continuing; claim it up front
    # so classify() reads the next turn as a real result, not a launch/preflight
    # (no fresh session_start hook fires without a relaunch).
    outcome.saw_session = True
    outcome.saw_agent_start = True
    controller.truncate_signal()

    # The child could have died between turns — recover, don't inject into a corpse.
    if not controller.child_alive():
        return _native_watchdog_kill(controller, outcome, cwd)

    _write_task_file(task_file_path, nudge_prompt)
    if not controller.inject_file(task_file_path):
        # Bug B: same escalation as run_native_turn — a failed nudge
        # injection must not sit silently in_progress either.
        return _native_watchdog_kill(controller, outcome, cwd)

    return _observe_native_turn(
        controller, outcome, cwd=cwd, turn_deadline=turn_deadline,
        idle_timeout=idle_timeout, poll_interval=poll_interval, now=now, sleep=sleep,
    )


def _task_file_for(task_id: str) -> str:
    base = os.environ.get("OMP_HOME", os.path.expanduser("~/.omp"))
    return os.path.join(base, "tasks", f"task-{task_id}.md")


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "--serve":
        return serve_loop(
            poll_interval=float(os.environ.get("OMP_POLL_INTERVAL", "5")),
        )
    return _main_replay(argv)


def _main_replay(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="omp-bridge: replay an omp json stream through the MC lifecycle.")
    ap.add_argument("stream", nargs="?", help="path to an NDJSON stream (default: stdin)")
    ap.add_argument("--task-id", default="TASK")
    ap.add_argument("--require-review", action="store_true", default=True,
                    help="board requires review before done (default on, MC-Dev board)")
    ap.add_argument("--no-require-review", dest="require_review", action="store_false")
    ap.add_argument("--retries", type=int, default=0,
                    help="retries left for retryable aborts (0 = terminal decision)")
    ap.add_argument("--json", action="store_true", help="emit a machine-readable decision line")
    args = ap.parse_args(argv)

    fh: TextIO
    if args.stream and args.stream != "-":
        fh = open(args.stream, "r", encoding="utf-8", errors="replace")
    else:
        fh = sys.stdin

    lifecycle = LoggingLifecycle()
    try:
        action = drive_run(
            fh, lifecycle,
            task_id=args.task_id,
            board_requires_review=args.require_review,
            retries_left=args.retries,
        )
    finally:
        if fh is not sys.stdin:
            fh.close()

    cls = action.classification
    if args.json:
        print(json.dumps({
            "action": action.action,
            "kind": cls.kind.value if cls else None,
            "reason": cls.reason if cls else None,
            "review": action.review,
            "blocker_type": action.blocker_type,
            "detail": cls.detail if cls else None,
        }))
    else:
        print(f"\n==> DECISION: {action.action.upper()}"
              f"  (kind={cls.kind.value}, reason={cls.reason})")
        if action.action == "blocker":
            print(f"    blocker_type={action.blocker_type}")
    # exit 0 always: the decision is in the output, not the exit code.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
