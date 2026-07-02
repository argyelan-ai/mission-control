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
    r"etimedout|timed out|\b5\d\d\b|overloaded|upstream",
    re.IGNORECASE,
)

# Streaming-delta wrapper — 509/578 lines in the real sample. Never lifecycle.
_DROP_TYPES = frozenset({"message_update"})

# Default bounded-retry budget for the abort class (design §3.3, "OMP_MAX_RETRIES
# (e.g. 2)"). The driver decrements this per re-spawn and, once exhausted, ALWAYS
# falls through to a terminal `mc blocked` — never a dangling non-terminal state.
OMP_MAX_RETRIES = 2


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

    action: str          # "finish" | "blocker" | "retry"
    review: bool = False
    blocker_type: Optional[str] = None
    question: Optional[str] = None
    reflection: Optional[str] = None
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
) -> LifecycleAction:
    """Map a classification to a single MC lifecycle action.

    Policy: FINISH -> finish/review. Retryable abort with retries left -> retry.
    Everything else (exhausted retries, non-retryable) -> blocker.
    NEVER `mc failed` (FAILED -> {INBOX} only, auto-unassigns, no auto-redispatch).
    """
    c = classification
    if c.kind is Kind.FINISH:
        return LifecycleAction(
            action="finish",
            review=board_requires_review,
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
) -> LifecycleAction:
    """Live-path analogue of drive_run for the omp SUBPROCESS model.

    drive_run consumes a TextIO stream and re-derives the outcome; the live path
    already has a fully-reduced `RunOutcome` from `run_omp_subprocess` (which ran
    the out-of-band wall-clock/no-progress watchdog), so re-serialising it would
    DROP the `watchdog_killed` verdict. This reuses the genuinely reusable cores
    — `classify` + `decide_lifecycle` — with the same retry-then-blocker policy:
    ack once, bounded retry on retryable aborts, ALWAYS terminal (finish|blocker),
    never `mc failed`, never left `in_progress`.

    `pre_acked=True` when the caller already stamped `mc ack` (serve_loop acks up
    front so a long omp run cannot trip the 10-min ACK-timeout re-dispatch and so
    `mc finish` sees the required `in_progress` precondition).
    """
    attempts_left = retries_left
    acked = pre_acked
    while True:
        outcome = run_once()
        if not acked and outcome.saw_session:
            lifecycle.ack(task_id)
            acked = True
        cls = classify(outcome)
        action = decide_lifecycle(
            cls, board_requires_review=board_requires_review, retries_left=attempts_left,
        )
        if action.action == "retry" and attempts_left > 0:
            lifecycle.comment(task_id, f"omp abort ({cls.reason}); retrying, {attempts_left} left")
            attempts_left -= 1
            continue
        if action.action == "finish":
            lifecycle.finish(task_id, outcome.reflection_block or "", review=action.review)
        else:
            if action.action == "retry":  # budget exhausted -> collapse to blocker
                action = LifecycleAction(
                    action="blocker", blocker_type="technical_problem",
                    question=cls.detail, classification=cls,
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


def serve_loop(
    *,
    poll_interval: float = 5.0,
    max_iterations: Optional[int] = None,
    _poll_fn: Optional[Callable[[], Optional[dict]]] = None,
    _lifecycle_factory: Optional[Callable[[dict], MCLifecycle]] = None,
    _run_factory: Optional[Callable[[dict, str], Callable[[], RunOutcome]]] = None,
    _sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Persistent poll→omp→lifecycle driver (ADR-045 §4.3).

    Ported skeleton of scripts/hermes-bridge.py:dispatch_poll_loop — same
    `GET /me/poll` contract + dispatch-dedup cache — but the delivery step spawns
    an omp subprocess and drives it through drive_run() instead of pasting into a
    tmux pane. Prints OMP_BRIDGE_READY exactly once, AFTER the first successful
    poll (the health-check anchor; a pre-exec echo would false-positive a
    crash-looping bridge). The `_*` params are injection seams for unit tests.
    """
    api_url = os.environ.get("MC_API_URL", "http://backend:8000").rstrip("/")
    token = os.environ.get("MC_AGENT_TOKEN", "")
    model = os.environ.get(
        "OMP_MODEL_SELECTOR"
    ) or _default_model_selector(os.environ.get("OPENAI_MODEL"))
    max_time = int(os.environ.get("OMP_MAX_TIME", "900"))
    require_review = os.environ.get("OMP_REQUIRE_REVIEW", "1") not in ("0", "false", "")
    retries = int(os.environ.get("OMP_MAX_RETRIES", str(OMP_MAX_RETRIES)))

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

            cwd = container_workspace_path(task.get("workspace_path")) or "/workspace"
            prompt = wrap_prompt(task.get("prompt") or task.get("title") or "")

            if _lifecycle_factory is not None:
                lifecycle: MCLifecycle = _lifecycle_factory(task)
            else:
                lifecycle = McCliLifecycle(
                    api_url=api_url, token=token, task_id=str(task["id"]),
                    board_id=task.get("board_id"), attempt_id=task.get("dispatch_attempt_id"),
                )

            if _run_factory is not None:
                run_once = _run_factory(task, cwd)
            else:
                def run_once(_p=prompt, _cwd=cwd, _m=model, _t=max_time) -> RunOutcome:
                    return run_omp_subprocess(_p, cwd=_cwd, model=_m, max_time=_t, tee=None)

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

    entrypoint.sh renders a `qwen-spark` models.yml provider from OPENAI_MODEL,
    so the selector is `qwen-spark/<OPENAI_MODEL>`. Never a bare token — that
    mis-resolves against omp's built-in provider catalog.
    """
    m = (openai_model or "").strip()
    if not m:
        return "qwen-spark/nvidia/Qwen3.6-35B-A3B-NVFP4"
    if "/" in m and m.split("/", 1)[0] in ("qwen-spark", "lm-studio", "openai"):
        return m  # already provider-qualified
    return f"qwen-spark/{m}"


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
