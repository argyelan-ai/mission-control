#!/usr/bin/env python3
"""grok-bridge.py — host-side bridge for the Grok Build CLI (ADR-063).

Pattern source: scripts/hermes-bridge.py (poll loop, steady heartbeat, SIGTERM
handling, localhost-only HTTP control server) + docker/omp-bridge/bridge.py
(headless subprocess, streaming-NDJSON reducer, out-of-band wall-clock/idle
watchdog, mc-cli lifecycle).

Diverges from hermes-bridge in the ONE thing that matters: Grok's `grok build`
CLI is NOT a persistent tmux TUI you paste prompts into. Every dispatch is a
one-shot headless subprocess:

    grok --prompt-file <file> --output-format streaming-json --cwd <workspace>
         --permission-mode acceptEdits --session-id <uuid>

that streams NDJSON events to stdout and then exits. So there is no tmux-paste
delivery; delivery is a subprocess whose event stream the bridge reduces, and —
because a headless CLI cannot be trusted to always drive its own MC lifecycle —
the BRIDGE owns ack/finish/blocked deterministically (the omp model), while the
grok agent itself registers deliverables/comments via the copied `mc` CLI
(mc-context.env contract).

Grok speaks ONLY to xAI cloud over its own OAuth (~/.grok/auth.json, auto
refresh). There is NO OPENAI_*/ANTHROPIC_* provider env and NO MC-bound model
endpoint — the runtime binding for a grok agent is a display/anchor only
(ADR-063). agent.env carries just the MC_* control-plane vars.

Endpoints:
  GET  /health   -> {"status","harness","dispatching","agent_env_present"}
  POST /start    -> no-op ack (grok has no long-lived session to spawn)
  POST /restart  -> drop the per-task session cache (next dispatch starts fresh)
  POST /stop     -> request cancellation of the in-flight dispatch

Auto-loaded by ~/Library/LaunchAgents/com.mc.grok-bridge.plist at login.
"""
from __future__ import annotations

import http.server
import json
import logging
import os
import queue as _queue
import shutil
import signal
import subprocess as _sp
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional, TextIO

# Ports: 18792/18793 = free-code-bridge, 18794 = hermes-bridge, 18795 = grok-bridge.
PORT = 18795
HOST = "127.0.0.1"  # localhost only, never the wildcard bind (same L-C rule as hermes-bridge)
HOME_DIR = Path(os.environ.get("HOME_HOST", str(Path.home())))
GROK_BIN = shutil.which("grok") or "/opt/homebrew/bin/grok"
CONFIG_DIR = HOME_DIR / ".mc/agents/grok"
WORKSPACE = HOME_DIR / ".mc/workspaces/grok"
ENV_FILE = CONFIG_DIR / "agent.env"
LOG_DIR = CONFIG_DIR / "logs"
HARNESS = "grok"

# Path the copied `mc` CLI reads task context from (mc_cli/config.py:from_env —
# file wins over stale process env). poll.sh writes it for the claude fleet; a
# headless bridge that replaces poll.sh MUST re-provide it or the agent's own
# `mc ack|deliverable|done` fail. Same 3-key contract as docker/shared/poll.sh.
MC_CONTEXT_ENV_PATH = os.environ.get("MC_CONTEXT_ENV_PATH", "/tmp/mc-context.env")

# Dispatch/lifecycle knobs (env-overridable).
DISPATCH_POLL_INTERVAL = int(os.environ.get("GROK_DISPATCH_POLL_INTERVAL", "5"))
# Heartbeat must stay well under the backend's 90s liveness window
# (cli_terminal.list_host_session_agents: session_running = last_seen < 90s).
HEARTBEAT_INTERVAL = int(os.environ.get("GROK_HEARTBEAT_INTERVAL", "30"))
# Wall-clock cap per dispatch and no-progress (idle) cap — mirrors the omp
# watchdog. Any NDJSON line refreshes progress; a genuinely hung generation
# (no bytes for GROK_IDLE_TIMEOUT) is SIGTERM'd so a run can never hang.
GROK_TASK_DEADLINE = float(os.environ.get("GROK_TASK_DEADLINE", "1800"))
GROK_IDLE_TIMEOUT = float(os.environ.get("GROK_IDLE_TIMEOUT", "300"))
GROK_PERMISSION_MODE = os.environ.get("GROK_PERMISSION_MODE", "acceptEdits")
GROK_MODEL = os.environ.get("GROK_MODEL", "")  # empty → CLI default (grok-4.5)
MC_BIN = os.environ.get("MC_BIN", "mc")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("grok-bridge")

# Per-task grok session ids (task_id -> grok sessionId). Lets follow-up comments
# / nudges on the same task resume the SAME grok conversation via `grok -r <id>`
# instead of starting cold every time.
_task_sessions: dict[str, str] = {}
_last_dispatched_task_id: Optional[str] = None  # dispatch-dedup cache
_dispatch_lock = threading.Lock()  # serialize: one grok subprocess at a time
_cancel_requested = threading.Event()


# ── env-file parsing (kept byte-identical to the backend escaping) ──────────────


def _unquote_env_value(raw: str) -> str:
    """Exact inverse of the backend's `_format_env_file` single-quote escaping.

    A naive `.strip("'")` leaves `'"'"'` sequences intact; kept in sync with
    backend/app/services/agent_bootstrap._unquote_env_value so a token written
    escaped is read back byte-identical (the 13 KB token-growth bug).
    """
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == "'" and raw[-1] == "'":
        return raw[1:-1].replace("'\"'\"'", "'")
    return raw.strip("'\"")


def load_env_from_file(env_path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines from agent.env, strip quotes, skip comments/blanks.

    Returns os.environ.copy() merged with file contents and HOME forced to
    HOME_DIR (so the grok subprocess resolves ~/.grok/auth.json on the host).
    """
    env = os.environ.copy()
    env["HOME"] = str(HOME_DIR)
    if not env_path.exists():
        return env
    with env_path.open() as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = _unquote_env_value(v)
    return env


# ── mc-context.env (the 3-key contract the agent's own `mc` calls read) ─────────


def write_task_context_env(task: dict, path: str = MC_CONTEXT_ENV_PATH) -> bool:
    """Write the per-dispatch task context the copied `mc` CLI needs.

    The grok agent's own `mc ack|deliverable|done` read TASK_ID / BOARD_ID /
    X_DISPATCH_ATTEMPT_ID via mc_cli/config.py:from_env, which resolves this
    file FIRST (it wins over the previous dispatch's process env). Without it
    `mc ack` fails ("TASK_ID … müssen gesetzt sein") and status calls are
    rejected 409 ("Missing X-Dispatch-Attempt-Id"). Best-effort: an unwritable
    file must never crash the serve loop.
    """
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"TASK_ID={task.get('id') or ''}\n")
            f.write(f"BOARD_ID={task.get('board_id') or ''}\n")
            f.write(f"X_DISPATCH_ATTEMPT_ID={task.get('dispatch_attempt_id') or ''}\n")
        return True
    except OSError as e:  # noqa: BLE001 — context file is best-effort
        log.warning("mc-context.env write failed: %s", e)
        return False


# ── streaming-json reducer ──────────────────────────────────────────────────────

# Event `type`s emitted per NDJSON line by `grok --output-format streaming-json`.
# Verified spike (2026-07-10): {"type":"thought","data":...},
# {"type":"text","data":...}, and a terminal
# {"type":"end","stopReason":"EndTurn","sessionId":"<uuid>","requestId":"..."}.
# Unknown types are counted but ignored so a schema addition never crashes us.


@dataclass
class GrokOutcome:
    """Everything the reducer distilled from one grok NDJSON stream."""

    saw_end: bool = False
    stop_reason: Optional[str] = None
    session_id: Optional[str] = None
    final_text: str = ""
    thought_chunks: int = 0
    text_chunks: int = 0
    error_message: Optional[str] = None
    parse_failures: int = 0
    lines_seen: int = 0
    watchdog_killed: bool = False  # set by the supervisor, not the stream
    exit_code: Optional[int] = None


def iter_grok_events(fileobj: TextIO, outcome: Optional[GrokOutcome] = None) -> Iterator[dict]:
    """Yield parsed NDJSON dicts. Malformed lines are counted, never raised —
    a truncated/partial stream (crash, SIGTERM mid-write) must still reduce.
    """
    for line in fileobj:
        line = line.strip()
        if not line:
            continue
        if outcome is not None:
            outcome.lines_seen += 1
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            if outcome is not None:
                outcome.parse_failures += 1
            continue
        if isinstance(obj, dict):
            yield obj


def reduce_grok_stream(events: Iterable[dict], outcome: Optional[GrokOutcome] = None) -> GrokOutcome:
    """Fold the grok event stream into a GrokOutcome.

    - thought → count (never surfaced; reasoning is not deliverable content)
    - text    → accumulate into final_text
    - error   → capture the message (also flips a non-EndTurn terminal)
    - end     → terminal: stopReason + sessionId
    """
    o = outcome or GrokOutcome()
    for ev in events:
        t = ev.get("type")
        if t == "thought":
            o.thought_chunks += 1
        elif t == "text":
            o.text_chunks += 1
            data = ev.get("data")
            if isinstance(data, str):
                o.final_text += data
        elif t == "error":
            # grok may emit a stand-alone error event; keep the first message.
            msg = ev.get("data") or ev.get("message")
            if isinstance(msg, str) and not o.error_message:
                o.error_message = msg
        elif t == "end":
            o.saw_end = True
            o.stop_reason = ev.get("stopReason")
            sid = ev.get("sessionId")
            if isinstance(sid, str) and sid:
                o.session_id = sid
    return o


@dataclass
class LifecycleAction:
    """What the bridge decided to do about a reduced run."""

    action: str  # "finish" | "blocked"
    reason: str  # short machine tag
    detail: str  # human-readable (becomes the blocker question when blocked)
    review: bool = True


# grok stopReasons that mean the turn ended cleanly. Everything else (an aborted
# turn, a max-turns cutoff, an error, a watchdog kill, or NO end event at all)
# collapses to a terminal blocker so a dispatch can never hang in_progress.
_CLEAN_STOP_REASONS = frozenset({"EndTurn", "endturn", "end_turn", "stop", "Stop"})


def map_lifecycle(outcome: GrokOutcome, *, board_requires_review: bool = True) -> LifecycleAction:
    """Deterministic stream → MC lifecycle mapping (bridge-owned, ADR-063).

    EndTurn + no error → finish (hand off to review). Anything else — watchdog
    kill, missing end event, error event, non-EndTurn stopReason, non-zero exit
    — is a blocker (reversible, notifies Mark) rather than a silent hang.
    """
    if outcome.watchdog_killed:
        return LifecycleAction(
            "blocked", "watchdog",
            "grok-bridge hat den Dispatch abgebrochen (Wall-Clock- oder "
            "Idle-Timeout überschritten) — kein hängender in_progress-Task. "
            "Bitte Ergebnis prüfen und Task erneut zuweisen.",
        )
    if outcome.error_message:
        return LifecycleAction(
            "blocked", "grok_error",
            f"grok meldete einen Fehler: {outcome.error_message[:400]}",
        )
    if not outcome.saw_end:
        return LifecycleAction(
            "blocked", "no_end",
            "grok-Stream endete ohne `end`-Event (Prozess abgestürzt / Stream "
            "abgeschnitten). Automatisch blockiert statt still in_progress zu "
            "lassen — bitte prüfen.",
        )
    if outcome.exit_code not in (None, 0):
        return LifecycleAction(
            "blocked", "nonzero_exit",
            f"grok beendete sich mit exit={outcome.exit_code} trotz "
            f"stopReason={outcome.stop_reason!r}. Bitte Ergebnis prüfen.",
        )
    if (outcome.stop_reason or "") in _CLEAN_STOP_REASONS:
        return LifecycleAction(
            "finish", "end_turn",
            outcome.final_text.strip() or "grok run complete (EndTurn).",
            review=board_requires_review,
        )
    return LifecycleAction(
        "blocked", "unclean_stop",
        f"grok endete mit stopReason={outcome.stop_reason!r} (nicht EndTurn) — "
        f"Turn wurde nicht sauber abgeschlossen. Bitte prüfen.",
    )


# ── dispatch prompt ─────────────────────────────────────────────────────────────


def build_dispatch_prompt(task: dict) -> str:
    """Build the single-turn prompt handed to `grok --prompt-file`.

    Mirrors the hermes dispatch contract but adapted for the headless model: the
    BRIDGE owns the terminal transition (finish/blocked), so grok is told to do
    the work + register deliverables/comments/checklist via the `mc` CLI, and to
    NOT itself move the task to review/done (the bridge does that from the
    stream's `end` event). task_id/board_id/attempt_id are surfaced in the
    header AND written to mc-context.env, so the `mc` CLI resolves them without
    grok having to thread env through subshells.

    SECURITY: never materialize the literal MC_AGENT_TOKEN — only $MC_AGENT_TOKEN
    references (resolved from the subprocess env) are allowed.
    """
    task_id = str(task.get("id") or "")
    board_id = str(task.get("board_id") or "")
    attempt_id = str(task.get("dispatch_attempt_id") or "")
    title = str(task.get("title") or "")
    body = str(task.get("description") or task.get("prompt") or "")

    return (
        f"[MC DISPATCH] task_id={task_id} board_id={board_id} attempt_id={attempt_id}\n"
        f"Title: {title}\n"
        f"\n"
        f"{body}\n"
        f"\n"
        f"PROTOCOL (grok headless via Mission Control):\n"
        f"- Your task context is already in {MC_CONTEXT_ENV_PATH} — the `mc` CLI\n"
        f"  reads TASK_ID / BOARD_ID / X_DISPATCH_ATTEMPT_ID from it. Just call `mc`.\n"
        f"- Register every concrete artefact you produce: `mc deliverable <path-or-url>`.\n"
        f"- Post progress as you go: `mc comment progress \"Update: ...\"`.\n"
        f"- Do the work in the current directory (your task workspace).\n"
        f"- Do NOT run `mc done` / `mc finish` / move the task to review yourself —\n"
        f"  the grok-bridge sets the terminal state from your turn's end. Just\n"
        f"  finish your turn cleanly when the work is done.\n"
    )


# ── grok subprocess ─────────────────────────────────────────────────────────────


def build_grok_command(
    *,
    prompt_file: str,
    workspace: str,
    session_id: Optional[str] = None,
    resume_session: Optional[str] = None,
) -> list[str]:
    """Assemble the `grok` argv for one headless dispatch.

    Round 1 of a task: pass a fresh `--session-id <uuid>` (a NEW named session).
    Follow-ups (comments/nudges on the same task): pass `-r <sessionId>` to
    resume the SAME grok conversation. `--prompt-file` (not `-p`) avoids any
    shell-escaping of multi-line prompts.
    """
    cmd = [
        GROK_BIN,
        "--output-format", "streaming-json",
        "--cwd", workspace,
        "--permission-mode", GROK_PERMISSION_MODE,
        "--prompt-file", prompt_file,
    ]
    if GROK_MODEL:
        cmd += ["--model", GROK_MODEL]
    if resume_session:
        cmd += ["-r", resume_session]
    elif session_id:
        cmd += ["-s", session_id]
    return cmd


def _supervise(
    stream: TextIO,
    outcome: GrokOutcome,
    *,
    kill: Callable[[], None],
    deadline: float,
    idle_timeout: float,
    now: Callable[[], float] = time.monotonic,
    poll_interval: float = 1.0,
) -> GrokOutcome:
    """Reduce `stream` while an out-of-band wall-clock + no-progress watchdog runs.

    A genuine hang emits no NDJSON, so a blocking `readline()` would never
    return and an inline deadline check would never run. So the blocking read
    lives on a daemon reader thread that stamps a real last-progress timestamp
    on every line; the main thread drains parsed events on a timer and evaluates
    both deadlines even while the reader is wedged. On either deadline it flips
    `watchdog_killed`, calls `kill()` (which SIGTERMs grok → EOF unblocks the
    reader), and stops. `kill` is injected so this is unit-testable against a
    fake blocking pipe.
    """
    q: "_queue.Queue[object]" = _queue.Queue()
    _EOF = object()
    last_progress = now()
    lock = threading.Lock()

    def _reader() -> None:
        nonlocal last_progress
        try:
            while True:
                raw = stream.readline()
                if raw == "":
                    break  # EOF (process exited / pipe closed by kill()).
                with lock:
                    last_progress = now()
                line = raw.strip()
                if not line:
                    continue
                outcome.lines_seen += 1
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    outcome.parse_failures += 1
                    continue
                if isinstance(obj, dict):
                    q.put(obj)
        finally:
            q.put(_EOF)

    reader = threading.Thread(target=_reader, name="grok-reader", daemon=True)
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
            if t_now > deadline or idle > idle_timeout:
                outcome.watchdog_killed = True
                kill()
                return
            if item is _EOF:
                return
            if item is not None:
                yield item  # type: ignore[misc]

    reduce_grok_stream(_drain(), outcome)
    reader.join(timeout=5)
    return outcome


def run_grok_dispatch(
    prompt: str,
    *,
    workspace: str = str(WORKSPACE),
    env: Optional[dict] = None,
    session_id: Optional[str] = None,
    resume_session: Optional[str] = None,
    deadline: float = GROK_TASK_DEADLINE,
    idle_timeout: float = GROK_IDLE_TIMEOUT,
    _popen: Optional[Callable[..., "_sp.Popen"]] = None,
) -> GrokOutcome:
    """Spawn one headless grok subprocess and reduce its NDJSON stream.

    Writes `prompt` to a temp file (avoids shell escaping), builds argv via
    build_grok_command, streams stdout through the out-of-band watchdog, and
    returns the reduced GrokOutcome (incl. exit_code). `_popen` is injected in
    tests; real runs use subprocess.Popen. SIGTERM (not SIGKILL) is used to
    cancel so grok can flush/checkpoint; a follow-up kill guards a wedged child.
    """
    outcome = GrokOutcome()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    prompt_file = str(LOG_DIR / f"dispatch-{uuid.uuid4().hex[:8]}.prompt")
    try:
        Path(prompt_file).write_text(prompt, encoding="utf-8")
    except OSError as e:
        log.error("run_grok_dispatch: could not write prompt file: %s", e)
        outcome.error_message = f"prompt file write failed: {e}"
        return outcome

    cmd = build_grok_command(
        prompt_file=prompt_file, workspace=workspace,
        session_id=session_id, resume_session=resume_session,
    )
    log.info("run_grok_dispatch: %s", " ".join(cmd))

    popen = _popen or _sp.Popen
    proc = popen(
        cmd,
        stdout=_sp.PIPE,
        stderr=_sp.PIPE,
        env=env or os.environ,
        cwd=workspace,
        text=True,
        start_new_session=True,
    )

    def _kill() -> None:
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    # External cancel request (POST /stop) races the watchdog.
    def _watch_cancel() -> None:
        while proc.poll() is None:
            if _cancel_requested.is_set():
                outcome.watchdog_killed = True
                _kill()
                return
            time.sleep(0.5)

    cancel_thread = threading.Thread(target=_watch_cancel, name="grok-cancel", daemon=True)
    cancel_thread.start()

    if proc.stdout is not None:
        _supervise(
            proc.stdout, outcome,
            kill=_kill, deadline=time.monotonic() + deadline, idle_timeout=idle_timeout,
        )
    outcome.exit_code = proc.wait()
    if outcome.exit_code not in (0, None) and not outcome.error_message and proc.stderr is not None:
        try:
            err = proc.stderr.read()
            if err:
                outcome.error_message = err.strip()[:400]
        except Exception:  # noqa: BLE001
            pass
    try:
        os.remove(prompt_file)
    except OSError:
        pass
    return outcome


# ── mc-cli lifecycle (bridge-driven, shells out to the copied `mc`) ─────────────


class GrokLifecycle:
    """Bridge-driven lifecycle — shells out to the copied `mc` CLI.

    Same lifecycle the whole fleet uses (`mc ack|finish|blocked|comment`); the
    task/board/attempt context is injected via env (the exact contract
    mc_cli/config.py:from_env reads). No new backend endpoint. Every call is
    best-effort logging — a failed lifecycle call must never crash the poll loop.
    """

    def __init__(self, *, base_url: str, token: str, board_id: str, attempt_id: str,
                 mc_bin: str = MC_BIN) -> None:
        self.base_url = base_url
        self.token = token
        self.board_id = board_id or ""
        self.attempt_id = attempt_id or ""
        self.mc_bin = mc_bin

    def _env(self, task_id: str) -> dict:
        env = dict(os.environ)
        env["HOME"] = str(HOME_DIR)
        env["MC_API_URL"] = self.base_url
        env["MC_BASE_URL"] = self.base_url
        env["MC_AGENT_TOKEN"] = self.token
        env["TASK_ID"] = task_id
        env["BOARD_ID"] = self.board_id
        env["X_DISPATCH_ATTEMPT_ID"] = self.attempt_id
        return env

    def _run(self, task_id: str, args: list[str]) -> int:
        try:
            proc = _sp.run(
                [self.mc_bin, *args], env=self._env(task_id),
                capture_output=True, text=True, timeout=60,
            )
            if proc.returncode != 0:
                log.warning(
                    "mc %s exit=%s: %s", args[0], proc.returncode,
                    (proc.stderr or proc.stdout or "").strip()[:300],
                )
            return proc.returncode
        except Exception as e:  # noqa: BLE001 — lifecycle must never crash the loop
            log.warning("mc %s raised %s: %s", args[0], type(e).__name__, e)
            return -1

    def ack(self, task_id: str) -> None:
        self._run(task_id, ["ack", task_id])

    def comment(self, task_id: str, text: str) -> None:
        self._run(task_id, ["comment", "progress", text])

    def finish(self, task_id: str, reflection: str, *, review: bool) -> None:
        args = ["finish", task_id, reflection]
        if review:
            args.append("--review")
        rc = self._run(task_id, args)
        if rc != 0:
            # Terminal guarantee: a rejected `mc finish` must NOT leave the task
            # silently in_progress — fall back to a blocker (reversible, notifies).
            log.warning("mc finish failed (rc=%s) -> falling back to blocked", rc)
            self.blocked(
                task_id,
                "grok-bridge konnte den Task nicht auf review setzen "
                f"(mc finish exit={rc}). Automatisch blockiert statt still "
                "in_progress zu lassen — bitte Ergebnis prüfen und neu zuweisen.",
            )

    def blocked(self, task_id: str, question: str) -> None:
        self._run(
            task_id,
            ["blocked", task_id, "--blocker-type", "technical_problem", "--question", question],
        )


def apply_lifecycle(lifecycle: GrokLifecycle, task_id: str, action: LifecycleAction) -> None:
    """Execute the mapped LifecycleAction against MC."""
    if action.action == "finish":
        lifecycle.finish(task_id, action.detail, review=action.review)
    else:
        lifecycle.comment(task_id, f"grok-bridge: {action.reason} — {action.detail[:200]}")
        lifecycle.blocked(task_id, action.detail)


# ── dispatch driver ─────────────────────────────────────────────────────────────


def dispatch_task(task: dict, env: dict) -> LifecycleAction:
    """Run one task end-to-end: context env → ack → grok subprocess → lifecycle.

    Serialized by _dispatch_lock (one grok subprocess at a time — a single
    OAuth session, rate-limit friendly). Returns the applied LifecycleAction.
    """
    task_id = str(task.get("id") or "")
    board_id = str(task.get("board_id") or "")
    attempt_id = str(task.get("dispatch_attempt_id") or "")
    base_url = (env.get("MC_BASE_URL") or "").rstrip("/")
    token = env.get("MC_AGENT_TOKEN") or ""

    lifecycle = GrokLifecycle(
        base_url=base_url, token=token, board_id=board_id, attempt_id=attempt_id,
    )
    with _dispatch_lock:
        _cancel_requested.clear()
        write_task_context_env(task)
        lifecycle.ack(task_id)  # protect against the 10-min ACK-timeout re-dispatch

        session_id = _task_sessions.get(task_id) or str(uuid.uuid4())
        _task_sessions[task_id] = session_id
        prompt = build_dispatch_prompt(task)
        outcome = run_grok_dispatch(
            prompt, workspace=str(WORKSPACE), env=env, session_id=session_id,
        )
        # grok may mint its own sessionId — track it for resume.
        if outcome.session_id:
            _task_sessions[task_id] = outcome.session_id

        action = map_lifecycle(outcome)
        log.info(
            "dispatch_task %s: lines=%d stop=%s -> %s (%s)",
            task_id[:8], outcome.lines_seen, outcome.stop_reason,
            action.action, action.reason,
        )
        apply_lifecycle(lifecycle, task_id, action)
        return action


def deliver_comment_nudge(task: dict, comment: dict, env: dict) -> Optional[GrokOutcome]:
    """Resume the task's grok session with a new user comment as a follow-up turn.

    Only fires when a session for the task already exists (i.e. the task was
    dispatched in this bridge's lifetime). Otherwise there is nothing to resume
    and the comment is left for the next full dispatch. Best-effort.
    """
    task_id = str(task.get("id") or comment.get("task_id") or "")
    session_id = _task_sessions.get(task_id)
    if not session_id:
        return None
    content = str(comment.get("content") or "")
    prompt = (
        f"[MC COMMENT] Neuer Kommentar auf Task {task_id}:\n\n{content}\n\n"
        f"Reagiere, arbeite am Task weiter. Kein `mc done` — die Bridge setzt den "
        f"Endstatus."
    )
    with _dispatch_lock:
        _cancel_requested.clear()
        write_task_context_env({"id": task_id,
                                "board_id": task.get("board_id") or comment.get("board_id"),
                                "dispatch_attempt_id": task.get("dispatch_attempt_id")})
        return run_grok_dispatch(prompt, workspace=str(WORKSPACE), env=env,
                                 resume_session=session_id)


# ── poll + heartbeat loops (mirror hermes-bridge) ───────────────────────────────


def dispatch_poll_loop() -> None:
    """Poll MC for the agent's active task; run new ones headless via grok.

    Idempotent via _last_dispatched_task_id. Network/JSON errors are logged and
    swallowed — the loop never crashes the bridge HTTP server. Endpoint:
    GET /api/v1/agent/me/poll (a CLAIM endpoint: sets ack_at + status=in_progress
    on inbox tasks). state=new_task → dispatch; idle/cancelled/stopped → clear
    dedup cache.
    """
    global _last_dispatched_task_id
    try:
        env = load_env_from_file(ENV_FILE)
        base_url = env.get("MC_BASE_URL")
        token = env.get("MC_AGENT_TOKEN")
        if not base_url or not token:
            log.error("dispatch_poll_loop: MC_BASE_URL / MC_AGENT_TOKEN missing in %s — loop exits", ENV_FILE)
            return
        url = f"{base_url.rstrip('/')}/api/v1/agent/me/poll"
        headers = {"Authorization": f"Bearer {token}"}
        log.info("dispatch_poll_loop: polling %s every %ss", url, DISPATCH_POLL_INTERVAL)

        import urllib.error
        import urllib.request

        while True:
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    body = resp.read().decode("utf-8")
                payload = json.loads(body) if body.strip() else None
                task = None
                if payload:
                    state = payload.get("state")
                    if state == "new_task":
                        task = payload.get("task")
                    elif state in ("idle", "cancelled", "stopped"):
                        if _last_dispatched_task_id is not None:
                            log.info("dispatch_poll_loop: agent %s, clearing dispatch cache", state)
                            _last_dispatched_task_id = None

                if task and task.get("id") and task["id"] != _last_dispatched_task_id:
                    _last_dispatched_task_id = task["id"]
                    # Run in a worker thread so a long grok dispatch doesn't stall
                    # polling/heartbeat; the _dispatch_lock still serializes runs.
                    threading.Thread(
                        target=_safe_dispatch, args=(task, env),
                        name=f"grok-dispatch-{str(task['id'])[:8]}", daemon=True,
                    ).start()

                # Follow-up comments on an active, already-dispatched task → resume.
                for c in (payload or {}).get("new_comments") or []:
                    if c.get("source") != "user":
                        continue
                    ct_id = str(c.get("task_id") or "")
                    if ct_id in _task_sessions:
                        threading.Thread(
                            target=deliver_comment_nudge,
                            args=({"id": ct_id, "board_id": c.get("board_id")}, c, env),
                            name=f"grok-nudge-{ct_id[:8]}", daemon=True,
                        ).start()
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    log.warning("dispatch_poll_loop: HTTP %s — %s", e.code, e.reason)
            except Exception as e:  # noqa: BLE001
                log.warning("dispatch_poll_loop: poll error: %s", type(e).__name__)
            time.sleep(DISPATCH_POLL_INTERVAL)
    except Exception as e:
        log.exception("[fatal] dispatch_poll_loop crashed: %s", e)
        raise


def _safe_dispatch(task: dict, env: dict) -> None:
    try:
        dispatch_task(task, env)
    except Exception as e:  # noqa: BLE001 — a dispatch crash must not kill the loop
        log.exception("dispatch failed for task %s: %s", str(task.get("id"))[:8], e)


def heartbeat_loop() -> None:
    """Keep the grok agent's last_seen_at fresh so it stays on the Sessions page.

    Headless grok has no persistent process to derive liveness from, so — like
    hermes-bridge — the bridge POSTs an empty /agent/me/heartbeat every
    HEARTBEAT_INTERVAL. /heartbeat only refreshes last_seen (events fire on
    status transitions only), so this is not noisy.
    """
    import urllib.error
    import urllib.request

    try:
        env = load_env_from_file(ENV_FILE)
        base_url = env.get("MC_BASE_URL")
        token = env.get("MC_AGENT_TOKEN")
        if not base_url or not token:
            log.error("heartbeat_loop: MC_BASE_URL / MC_AGENT_TOKEN missing — loop exits")
            return
        url = f"{base_url.rstrip('/')}/api/v1/agent/me/heartbeat"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        log.info("heartbeat_loop: POST %s every %ss", url, HEARTBEAT_INTERVAL)
        while True:
            try:
                req = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=10):
                    pass
            except urllib.error.HTTPError as e:
                log.warning("heartbeat_loop: HTTP %s — %s", e.code, e.reason)
            except Exception as e:  # noqa: BLE001
                log.warning("heartbeat_loop: error: %s", type(e).__name__)
            time.sleep(HEARTBEAT_INTERVAL)
    except Exception as e:
        log.exception("[fatal] heartbeat_loop crashed: %s", e)
        raise


# ── HTTP control server ─────────────────────────────────────────────────────────


class Handler(http.server.BaseHTTPRequestHandler):
    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/health"):
            self._send_json(200, {
                "status": "ok",
                "harness": HARNESS,
                "dispatching": _dispatch_lock.locked(),
                "agent_env_present": ENV_FILE.exists(),
            })
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/start":
            # grok has no long-lived session; /start is a readiness ack.
            self._send_json(200, {"ok": True, "harness": HARNESS, "note": "headless — no persistent session"})
            return
        if self.path == "/restart":
            # Drop the per-task session cache so the next dispatch starts fresh
            # (picks up a freshly re-sourced agent.env for the next `grok` call).
            _task_sessions.clear()
            _cancel_requested.set()
            self._send_json(200, {"ok": True, "restart": "session cache cleared"})
            return
        if self.path == "/stop":
            _cancel_requested.set()
            self._send_json(200, {"ok": True, "stopped": "cancel requested"})
            return
        self._send_json(404, {"error": "not found"})

    def log_message(self, fmt, *args):  # noqa: A003
        log.info("%s - %s", self.address_string(), fmt % args)


def _handle_sigterm(signum, frame):  # noqa: ARG001
    log.info("[shutdown] received SIGTERM, exiting cleanly")
    _cancel_requested.set()
    sys.exit(0)


def main() -> None:
    try:
        signal.signal(signal.SIGTERM, _handle_sigterm)
        # Background dispatcher + steady heartbeat (daemon → die with HTTP server).
        threading.Thread(target=dispatch_poll_loop, name="grok-dispatcher", daemon=True).start()
        log.info("grok-dispatcher thread started (poll every %ss)", DISPATCH_POLL_INTERVAL)
        threading.Thread(target=heartbeat_loop, name="grok-heartbeat", daemon=True).start()
        log.info("grok-heartbeat thread started (POST every %ss)", HEARTBEAT_INTERVAL)
        server = http.server.HTTPServer((HOST, PORT), Handler)
        log.info("grok-bridge listening on %s:%d (bin=%s)", HOST, PORT, GROK_BIN)
        server.serve_forever()
        log.info("[shutdown] grok-bridge main loop exited normally")
    except SystemExit:
        raise
    except Exception as e:
        log.exception("[fatal] grok-bridge main crashed: %s", e)
        log.error("[fatal] bridge exiting due to %s", type(e).__name__)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
