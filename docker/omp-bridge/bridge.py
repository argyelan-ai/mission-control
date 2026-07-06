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
    """Slice the block from the first header to the line before the sentinel.

    Returns None if the first required header is absent.
    """
    idx = text.find(REFLECTION_HEADERS[0])
    if idx == -1:
        return None
    block = text[idx:]
    # Trim everything from a standalone TASK_COMPLETE line onward.
    lines = block.splitlines()
    kept = []
    for line in lines:
        if line.strip() == SENTINEL:
            break
        kept.append(line)
    return "\n".join(kept).strip()


def validate_reflection(block: Optional[str]) -> bool:
    """Mirror mc_cli._validate_reflection: 4 headers present + >=80 chars."""
    if not block:
        return False
    if len(block) < MIN_REFLECTION_CHARS:
        return False
    return all(h in block for h in REFLECTION_HEADERS)


def sentinel_present(text: str) -> bool:
    """Anti-echo: TASK_COMPLETE counts only as the last non-empty line, alone."""
    return last_nonempty_line(text) == SENTINEL


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


def wrap_prompt(prompt: str) -> str:
    """Append the §3.4 completion contract to the MC-built dispatch prompt."""
    return (prompt or "").rstrip() + COMPLETION_INSTRUCTIONS


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

    def _run(self, task_id: str, args: list[str], *, best_effort: bool = False) -> int:
        """Run an mc-cli subcommand. Returns the process exit code (``-1`` if the
        command could not be launched and ``best_effort`` swallowed the error)."""
        cmd = [self.mc_bin, *args]
        try:
            proc = subprocess.run(
                cmd, env=self._env(task_id), capture_output=True, text=True, timeout=60,
            )
            if proc.returncode != 0:
                sys.stderr.write(
                    f"[mc-cli] {args[0]} exit={proc.returncode}: "
                    f"{(proc.stderr or proc.stdout or '').strip()[:400]}\n"
                )
            return proc.returncode
        except Exception as e:  # noqa: BLE001 — lifecycle must never crash the loop
            sys.stderr.write(f"[mc-cli] {args[0]} raised {type(e).__name__}: {e}\n")
            if not best_effort:
                raise
            return -1

    def ack(self, task_id: str) -> None:
        self._run(task_id, ["ack", task_id])

    def finish(self, task_id: str, reflection: str, *, review: bool) -> None:
        args = ["finish", task_id, reflection]
        if review:
            args.append("--review")
        # Terminal guarantee: a non-zero `mc finish` (backend rejected the
        # reflection, transient 5xx, ...) must NOT leave the task silently
        # in_progress — that is the exact silent-hang this runtime exists to
        # close. Fall back to `blocked` (reversible, notifies Mark) so every
        # run reaches a terminal state. best_effort=True so a hard launch
        # failure returns rc=-1 here instead of raising past the fallback.
        rc = self._run(task_id, args, best_effort=True)
        if rc != 0:
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
            lifecycle.comment(
                task_id, f"omp {cls.reason}; continue-nudge, {continues} left"
            )
            continues -= 1
            outcome = continue_once(action.nudge_prompt or "")
            continue
        if action.action == "retry" and attempts_left > 0:
            lifecycle.comment(task_id, f"omp abort ({cls.reason}); retrying, {attempts_left} left")
            attempts_left -= 1
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

    # One controller for the container's lifetime; run_native_turn relaunches +
    # truncates the signal per task, so state never bleeds between tasks.
    tui = NativeTuiController(session=session, signal_file=signal_file, window=tui_window,
                              launcher=launcher)

    poll_fn = _poll_fn or _make_http_poll(api_url, token)
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

        if task and task.get("id"):
            attempt_id = task.get("dispatch_attempt_id") or task["id"]
            if attempt_id == last_attempt_id:
                _sleep(poll_interval)
                continue
            last_attempt_id = attempt_id

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
                def continue_once(nudge: str, _cwd=cwd, _tf=task_file) -> RunOutcome:
                    return run_native_continue(
                        tui, cwd=_cwd, nudge_prompt=wrap_prompt(nudge), task_file_path=_tf,
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


def _make_http_poll(api_url: str, token: str) -> Callable[[], Optional[dict]]:
    import urllib.request

    url = f"{api_url}/api/v1/agent/me/poll"
    headers = {"Authorization": f"Bearer {token}"}

    def _poll() -> Optional[dict]:
        req = urllib.request.Request(url, headers=headers)
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

    def inject_file(self, path: str) -> None:
        """Inject the dispatch as an `@/abs/path` mention (no body paste).

        Verified in-container (omp v16.2.13 TUI): typing `@<existing-path>`
        opens a file-mention AUTOCOMPLETE POPUP that swallows a bare Enter, so a
        single Enter never submits. The robust sequence is:
          1. type `@path`      -> popup appears, text in the input box,
          2. `Escape`          -> dismisses the popup, KEEPS the `@path` text,
          3. `Enter`           -> submits; omp resolves `@path` -> Read(file).
        Escape is a no-op when no popup is showing, so this is safe either way.
        Small key delays let the TUI process each event in order.
        """
        self._run(["send-keys", "-t", self.target, "--", f"@{path}"])
        self._sleep(self.key_delay)
        self._run(["send-keys", "-t", self.target, "Escape"])
        self._sleep(self.key_delay)
        self._run(["send-keys", "-t", self.target, "Enter"])

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
    controller.inject_file(task_file_path)

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
    controller.inject_file(task_file_path)

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
