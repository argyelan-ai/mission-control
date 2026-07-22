#!/usr/bin/env python3
"""hermes-bridge.py — host-side bridge for Hermes Worker (Phase 24).

Pattern source: scripts/free-code-bridge.py
Diverges in: binds 127.0.0.1 only (per Phase 24 L-C decision); spawns long-lived
tmux 'hermes-worker' session running the Hermes binary in a watchdog loop via
entrypoint.sh.

Endpoints:
  GET  /health  -> {"status","session","tmux_running","agent_env_present"}
  POST /start   -> spawn tmux session if not running

Auto-loaded by ~/Library/LaunchAgents/com.mc.hermes-bridge.plist at login.
"""
from __future__ import annotations

import http.server
import json
import logging
import os
import re
import shutil
import signal
import subprocess as _sp
import sys
import threading
import time
from pathlib import Path

# Ports: 18792 = free-code-bridge, 18793 = bridge-WS, 18794 = hermes-bridge
PORT = 18794
HOST = "127.0.0.1"  # L-C: localhost only, no public bind
HOME_DIR = Path(os.environ.get("HOME_HOST", str(Path.home())))
HERMES_BIN = str(HOME_DIR / ".local/bin/hermes")
WORKSPACE = HOME_DIR / ".mc/agents/hermes"
ENV_FILE = WORKSPACE / "agent.env"
SESSION = "hermes-worker"
ENTRYPOINT = WORKSPACE / "entrypoint.sh"
TMUX_BIN = shutil.which("tmux") or "/opt/homebrew/bin/tmux"

# Phase 25-06: Dispatch poll loop — pulls active tasks from MC backend and
# delivers them as prompts into the Hermes tmux session. See plan 25-06.
DISPATCH_POLL_INTERVAL = int(os.environ.get("HERMES_DISPATCH_POLL_INTERVAL", "5"))
_last_dispatched_task_id: str | None = None  # Idempotency cache (module-scoped)
# Bug fix (W2 bridge parity, 2026-07): dedup MUST key on (task_id, attempt_id),
# not task_id alone — mirrors grok-bridge.py's _last_dispatched_attempt_id and
# poll.sh's attempt-id dedup guard. Every re-dispatch of the SAME task_id (e.g.
# a review_rejection redispatch, "auch bei new_task" per poll.sh) carries a
# fresh dispatch_attempt_id; task_id-only dedup silently swallowed that
# redispatch forever because _last_dispatched_task_id is only cleared on
# idle/cancelled/stopped, not on same-task revision.
_last_dispatched_attempt_id: str | None = None

# ── Interaction Model 2.0 (comm_v2): Turn-Grenzen-Gate fuer Thread-Messages ──
# Twin of docker/shared/poll.sh's build_acked_seq_param/queue_or_deliver/
# msg_gate_open/flush_msg_queue/_record_ack/deliver_messages. Backend is
# UNCHANGED — /me/poll only returns `new_messages` for comm_v2=true agents;
# the key being absent means byte-identical legacy behavior (no new codepath
# is ever entered for non-pilot agents).
MSG_QUIET_SECONDS = float(os.environ.get("HERMES_MSG_QUIET_SECONDS", "10"))
MSG_QUEUE_DIR = WORKSPACE / "logs" / "msg-queue"
MSG_ACK_DIR = WORKSPACE / "logs" / "msg-acked"
# Pane-diff state for the quiet-heuristic gate (hermes HAS a tmux pane, unlike
# a hypothetical headless bridge — so we reuse the grok-bridge pane-quiet
# pattern here rather than falling back to the weaker dispatch-only gate the
# spec allows when no pane is capturable).
_msg_pane_state: dict = {"pane": None, "last_change_ts": 0.0}

# Review fix (2026-07-21, live-confirmed): the TUI can render VOLATILE status
# text (a ticking timer, token/context counter, spinner frame) that changes
# every second even while the agent is genuinely idle between tasks. Compared
# raw, that made the pane look "changed" on every poll — the quiet clock
# never reached MSG_QUIET_SECONDS, the gate never opened, and comm_v2
# messages hung forever (not just delayed past the 600s reminder — the
# reminder paste ALSO requires the gate open). Concrete trigger: hermes'
# Qwen3.6-27B-FP8 status line, e.g.
#   "⚕ Qwen3.6-27B-FP8 │ 31.1K/262.1K │ … │ 5h │ ⏲ 18m 58s │ ✓ 2h 10m │ ⚠ YOLO"
# Fix: strip known-volatile SUBSTRINGS (not whole lines — a line staying
# structurally identical while only its timer ticks must still compare equal,
# but genuine new content in that same line must still be seen) before the
# quiet-clock comparison. Extendable via MSG_QUIET_VOLATILE_PATTERNS (newline-
# separated extra regexes, appended to the defaults below).
_VOLATILE_PATTERN_SOURCES = [
    r"\d+h(?:\s*\d+m)?",                # 5h, 5h 30m
    r"\d+m\s*\d+s",                     # 18m 58s
    r"\d+(?:\.\d+)?K/\d+(?:\.\d+)?K",   # 31.1K/262.1K (token/context counter)
    r"\d+%",                            # 42% (progress display)
    r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]",                    # braille spinner frames
    r"[✻✽·✢]",                          # asterisk-rotation spinner frames
    r"[⏲⏳]",                            # timer/hourglass icons
]
_extra_volatile = os.environ.get("MSG_QUIET_VOLATILE_PATTERNS", "")
if _extra_volatile.strip():
    _VOLATILE_PATTERN_SOURCES.extend(
        p for p in _extra_volatile.splitlines() if p.strip()
    )
_VOLATILE_RE = re.compile("|".join(f"(?:{p})" for p in _VOLATILE_PATTERN_SOURCES))


def _normalize_volatile(pane: str) -> str:
    """Replace volatile substrings (timers, token counters, spinner frames,
    percentages) with a fixed placeholder so a status line that only ticks
    those values compares equal to itself across polls. Genuine new content
    elsewhere in the line/pane still differs and still resets the clock."""
    return _VOLATILE_RE.sub("•", pane)

# Session-reset on GENUINE task switch (ADR-068 addendum, twin of grok-bridge).
# Dispatch semantics (dispatch.py:8-18) promise a fresh context per NEW task —
# without a reset the paste model accumulates every task into one conversation
# (observed live 2026-07-12: 30% context fill from prior tasks). hermes-agent
# gates bare /new behind a destructive-command confirm modal; the inline skip
# token `now` (cli.py _split_destructive_skip) bypasses it non-interactively.
RESET_COMMAND = os.environ.get("HERMES_RESET_COMMAND", "/new now")
# Last dispatched task id, persisted to DISK so a bridge restart cannot erase
# switch detection (_last_dispatched_task_id above resets on restart).
LAST_TASK_FILE = WORKSPACE / "logs" / "last-task-id"
# How long the TUI needs to rotate sessions before the next paste is safe.
RESET_SETTLE_SECONDS = float(os.environ.get("HERMES_RESET_SETTLE_SECONDS", "3"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("hermes-bridge")


def load_env_from_file(env_path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines from agent.env, strip quotes, skip comments/blanks.

    Returns os.environ.copy() merged with file contents and HOME forced to HOME_DIR.
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


def _unquote_env_value(raw: str) -> str:
    """Exact inverse of the backend's _format_env_file single-quote escaping.

    A naive .strip("'") leaves '"'"' sequences intact; kept in sync with
    backend/app/services/agent_bootstrap._unquote_env_value so a token that was
    written escaped is read back byte-identical (see the 13 KB token bug).
    entrypoint.sh re-sources agent.env anyway, but this keeps the pass-through
    env correct on its own.
    """
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == "'" and raw[-1] == "'":
        return raw[1:-1].replace("'\"'\"'", "'")
    return raw.strip("'\"")


def is_session_running() -> bool:
    r = _sp.run([TMUX_BIN, "has-session", "-t", SESSION], capture_output=True)
    return r.returncode == 0


def capture_pane() -> str:
    """Return the visible pane text of the hermes tmux session (empty on failure).

    Used by the comm_v2 message-gate (pane-quiet heuristic) and its verify step.
    """
    r = _sp.run([TMUX_BIN, "capture-pane", "-p", "-t", SESSION], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def start_hermes_session() -> dict:
    if not ENV_FILE.exists():
        raise FileNotFoundError(f"agent.env missing at {ENV_FILE} — provision first")
    env = load_env_from_file(ENV_FILE)
    if is_session_running():
        return {"status": "already_running", "session": SESSION}
    # entrypoint.sh manages the watchdog loop + Hermes invocation. Invoke it
    # DIRECTLY as a detached background process — NOT wrapped in `tmux new-session`.
    # entrypoint.sh internally runs `tmux kill-session` then `tmux new-session -d`;
    # if we wrap it in tmux, the kill-session kills the wrapping session and the
    # script dies via SIGHUP before the new session is spawned (race).
    # Detaching with start_new_session keeps it alive after bridge HTTP returns.
    if ENTRYPOINT.exists():
        _sp.Popen(
            [str(ENTRYPOINT)],
            env=env,
            stdout=_sp.DEVNULL,
            stderr=_sp.DEVNULL,
            start_new_session=True,
        )
        # Wait briefly for entrypoint to spawn the tmux session before returning,
        # so the immediate /health check after /start sees tmux_running=true.
        import time as _t
        for _ in range(20):  # up to 2 seconds
            if is_session_running():
                break
            _t.sleep(0.1)
    else:
        # Fallback: spawn empty tmux + send hermes binary path
        _sp.run(
            [TMUX_BIN, "new-session", "-d", "-s", SESSION, "-x", "220", "-y", "50"],
            check=True, env=env,
        )
        _sp.run(
            [TMUX_BIN, "send-keys", "-t", SESSION, HERMES_BIN, "Enter"],
            check=True, env=env,
        )
    log.info("started tmux session %s with Hermes binary", SESSION)
    return {"status": "started", "session": SESSION}


def _build_dispatch_prompt(task: dict) -> str:
    """Build the dispatch prompt that gets pasted into the Hermes tmux session.

    SECURITY: This function MUST NEVER materialize the literal MC_AGENT_TOKEN
    value. Only the variable reference `$MC_AGENT_TOKEN` is allowed. Hermes
    resolves it from its own tmux env when executing the curl command.

    Accepts the `task` dict from `GET /api/v1/agent/me/poll` (state=new_task)
    response. Required keys: id, board_id. Optional: title, description,
    prompt, dispatch_attempt_id.
    Missing/None fields are rendered as empty strings (no crash).

    The dispatch_attempt_id is surfaced in the header (Phase B requirement:
    every PATCH/POST against the task needs `X-Dispatch-Attempt-Id`; without
    it the backend returns 409 + emits `task.missing_dispatch_attempt_id`).
    Previously Hermes learned the ID from the first 409 — that produced
    Discord noise on every dispatch. Now it lands in the prompt directly.
    """
    task_id = str(task.get("id") or "")
    board_id = str(task.get("board_id") or "")
    attempt_id = str(task.get("dispatch_attempt_id") or "")
    title = str(task.get("title") or "")
    # `description` slot — when called from the live poll loop we feed the
    # backend-built `prompt` (which contains full MC dispatch context); unit
    # tests pass a plain `description` field. Either is rendered verbatim.
    body = str(task.get("description") or task.get("prompt") or "")

    return (
        f"[MC DISPATCH] task_id={task_id} board_id={board_id} attempt_id={attempt_id}\n"
        f"Title: {title}\n"
        f"\n"
        f"{body}\n"
        f"\n"
        f"PROTOCOL — extract task_id, board_id and attempt_id from the header\n"
        f"above and pass as explicit args (env vars are NOT propagated through\n"
        f"Hermes' LLM context — do not assume $MC_TASK_ID exists in subshells).\n"
        f"Every PATCH/POST against this task MUST carry the header\n"
        f'`X-Dispatch-Attempt-Id: {attempt_id}` — without it the server returns 409.\n'
        f'  ACK NOW: mc_patch_task(task_id="{task_id}", board_id="{board_id}", status="in_progress")\n'
        f'  Checklist FIRST: mc_checklist_add(task_id="{task_id}", board_id="{board_id}", '
        f'items=["step 1", "step 2", ...]) — it is the single source of truth for progress '
        f'and shows in the task detail panel. Tick items off: mc_checklist_done(task_id="{task_id}", '
        f'board_id="{board_id}", item_id="<id>"). Use the mc_checklist* tools, NOT a shell `mc checklist`.\n'
        f"  Comment format: 3 lines exactly — Update / Evidence / Next.\n"
        f'  Comment via: mc_patch_task(task_id="{task_id}", board_id="{board_id}", comment="Update: ...\\nEvidence: ...\\nNext: ...")\n'
        f'  Hand off: mc_patch_task(task_id="{task_id}", board_id="{board_id}", status="review") when done.\n'
        f"  Skill: ~/.hermes/skills/mission-control/SKILL.md\n"
        f"  Workspace: cd ~/.mc/workspaces/hermes  # task workspace (browsable in Files)\n"
    )


def _send_to_tmux(prompt: str) -> None:
    """Paste prompt into the Hermes tmux session via two send-keys calls.

    Uses `-l` (literal) so prompt content is NOT interpreted as tmux key-names
    (e.g. embedded "Enter" text would otherwise submit prematurely). Errors
    are swallowed (check=False) — caller logs separately if needed.
    """
    _sp.run([TMUX_BIN, "send-keys", "-t", SESSION, "-l", prompt], check=False, env=os.environ)
    _sp.run([TMUX_BIN, "send-keys", "-t", SESSION, "Enter"], check=False, env=os.environ)


def load_last_task_id() -> str | None:
    """Read the persisted last-dispatched task id (None if never dispatched)."""
    try:
        value = LAST_TASK_FILE.read_text(encoding="utf-8").strip()
        return value or None
    except FileNotFoundError:
        return None
    except OSError as e:  # unreadable state file → treat as unknown, never crash
        log.warning("load_last_task_id: %s — treating as no prior task", e)
        return None


def save_last_task_id(task_id: str) -> None:
    """Persist the last-dispatched task id for switch detection across restarts."""
    try:
        LAST_TASK_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_TASK_FILE.write_text(f"{task_id}\n", encoding="utf-8")
    except OSError as e:
        log.warning("save_last_task_id: %s — switch detection may miss one restart", e)


def should_reset_session(new_task_id: str, last_task_id: str | None) -> bool:
    """Reset ONLY on a genuine task switch (dispatch.py:8-18 semantics).

    - different task than the last dispatched one → True (fresh context)
    - same task (revision / request_changes / restart redelivery) → False
    - no known prior task → False (nothing to clear)
    """
    return bool(last_task_id) and new_task_id != last_task_id


def reset_tui_session() -> None:
    """Submit RESET_COMMAND (/new now) into the hermes TUI and let it settle.

    Reuses the proven _send_to_tmux mechanic (literal keys + Enter — the
    hermes prompt_toolkit TUI submits on LF, unlike raw-mode ptys). The `now`
    token bypasses the destructive-command confirm modal, so no second
    keystroke is needed. A short settle sleep keeps the subsequent task paste
    out of the session rotation window.
    """
    log.info("task switch — resetting hermes TUI session (%s)", RESET_COMMAND)
    _send_to_tmux(RESET_COMMAND)
    time.sleep(RESET_SETTLE_SECONDS)


def _build_comments_prompt(comments: list) -> str:
    """Build a paste-ready prompt for a batch of new_comments from /me/poll.

    Bug 11 fix (2026-05-14): hermes-bridge.py used to ignore the `new_comments`
    array in the /me/poll response. Host-agents (Boss, kuenftige Host-Worker)
    therefore never saw User-/handoff-comments while idle. Mirrors the pattern
    used by docker/shared/poll.sh:deliver_comments — separates user vs system
    source, formats with task header + content, ends with an action hint.

    Backend already filters out the agent's own comments via author_type, so
    no client-side dedup needed.
    """
    user_c = [c for c in comments if c.get("source") == "user"]
    sys_c = [c for c in comments if c.get("source") == "system"]

    lines: list[str] = []
    if user_c:
        lines += [
            "[MC COMMENT] Neue User-Kommentare auf deinen aktiven Tasks",
            "",
            "Der Operator hat kommentiert. Lies, antworte im Task-Thread, arbeite am Task weiter.",
            "",
        ]
        for c in user_c:
            lines.append(f"## Task: {c.get('task_title', '?')}  (id: {c.get('task_id', '?')})")
            lines.append(f"- Zeit: {c.get('created_at', '?')}")
            lines.append("- Inhalt:")
            for line in (c.get("content") or "").splitlines():
                lines.append(f"  > {line}")
            lines.append("")

    if sys_c:
        if user_c:
            lines += ["---", ""]
        lines += [
            "[MC EVENT] System-Events auf deinen aktiven Tasks",
            "",
            "Automatische Events (kein User-Input). Reagiere faktenbasiert:",
            "- subtask_completed: Subtask ist fertig. Pruefe Deliverables, entscheide ob Parent-Task auf review kann.",
            "- resolution: Agent hat Task abgeschlossen.",
            "- blocker: Task blockiert. Pruefe Impact + Entscheidung.",
            "",
        ]
        for c in sys_c:
            ct = c.get("comment_type", "system")
            lines.append(f"## [{ct}] {c.get('task_title', '?')}  (id: {c.get('task_id', '?')})")
            lines.append(f"- Zeit: {c.get('created_at', '?')}")
            lines.append("- Inhalt:")
            for line in (c.get("content") or "").splitlines():
                lines.append(f"  > {line}")
            lines.append("")

    if not lines:
        return ""

    lines.append(
        "**Aktion:** Reagiere im Task-Thread per "
        "mc_patch_task(task_id=..., board_id=..., comment=\"Update: ...\\nEvidence: ...\\nNext: ...\")."
    )
    return "\n".join(lines)


def build_acked_seq_param() -> str:
    """Twin of poll.sh:build_acked_seq_param — urlencoded JSON {thread_id: seq}
    of the highest actually-delivered seq per thread, read from MSG_ACK_DIR.

    Empty string when no ack files exist yet (no acked_seq query param sent —
    identical to a non-pilot agent's poll call).
    """
    if not MSG_ACK_DIR.exists():
        return ""
    import urllib.parse

    acked: dict[str, int] = {}
    for f in MSG_ACK_DIR.iterdir():
        if not f.is_file():
            continue
        try:
            seq = int(f.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            continue
        acked[f.name] = seq
    if not acked:
        return ""
    return urllib.parse.quote(json.dumps(acked))


def queue_or_deliver(payload: dict) -> int:
    """Twin of poll.sh:queue_or_deliver — persist every `new_messages` entry as
    a seq-named file in MSG_QUEUE_DIR BEFORE any paste attempt (crash-safe).

    Idempotent: redelivery of the same seq/thread overwrites the same file
    with identical content. Returns the number of messages queued.
    """
    msgs = (payload or {}).get("new_messages") or []
    if not msgs:
        return 0
    MSG_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    n = 0
    for m in msgs:
        seq = int(m["seq"])
        tid = str(m["thread_id"])
        sender = m.get("sender", "?")
        mtype = m.get("message_type", "?")
        body = m.get("body") or ""
        # Format taken verbatim from poll.sh:queue_or_deliver's footer line —
        # this exact anchor is what flush_msg_queue's verify step greps for.
        lines = [
            "# Neue Nachricht (Interaction 2.0)",
            "",
            body,
            "",
            f"[thread {tid} · seq {seq} · von {sender} · typ {mtype}]",
        ]
        fname = f"{seq:08d}__{tid}.msg"
        (MSG_QUEUE_DIR / fname).write_text("\n".join(lines), encoding="utf-8")
        n += 1
    return n


def msg_queue_files() -> list[str]:
    """Twin of poll.sh:msg_queue_files — queued message basenames, sorted by
    leading seq (numeric, not lexical — zero-padded so both orders agree)."""
    if not MSG_QUEUE_DIR.exists():
        return []
    names = [p.name for p in MSG_QUEUE_DIR.glob("*.msg")]
    return sorted(names, key=lambda n: int(n.split("__", 1)[0]))


def _pane_quiet_seconds(now: float, pane: str) -> float:
    """Track pane-diff state; return seconds since the pane content last
    changed. Pane change = progress → resets the quiet clock to 0.

    Pure w.r.t. tmux (caller supplies now/pane) — mirrors grok-bridge's
    _watchdog_tick pane-diff idea, reused here to drive the message gate.

    `pane` is normalized (volatile substrings stripped) BEFORE the compare —
    see _normalize_volatile. This only affects the message-gate quiet clock,
    NOT any watchdog/no-progress pane-diff (there isn't one in this module —
    hermes has no separate watchdog pane tracker to disturb).
    """
    pane = _normalize_volatile(pane)
    if pane != _msg_pane_state["pane"]:
        _msg_pane_state["pane"] = pane
        _msg_pane_state["last_change_ts"] = now
        return 0.0
    return now - _msg_pane_state["last_change_ts"]


def msg_gate_open(*, dispatch_in_flight: bool = False) -> bool:
    """Twin of poll.sh:msg_gate_open — pane-quiet heuristic (spec Bridge C):
    the tmux pane must be unchanged for >= MSG_QUIET_SECONDS, no task dispatch
    just happened in this poll iteration, AND no task is currently active.

    hermes DOES have a capturable tmux pane (unlike a hypothetical headless
    bridge) — that's why we implement the full Pane-Quiet gate + Verify below
    instead of the spec's weaker fallback ("kein Dispatch in flight" only,
    Ack after send-keys returncode with no Verify).

    Review fix (2026-07): pane-quiet alone is NOT a turn boundary. poll.sh's
    real msg_gate_open checks `detect_turn_state == idle` — a genuine "claude
    is not working" signal. hermes has no such signal; a long silent tool call
    or a thinking pause INSIDE an active turn also holds the pane still for
    >= MSG_QUIET_SECONDS, which would previously have opened the gate and
    pasted a message mid-turn. We close that gap by additionally requiring
    "no active task" — using _last_dispatched_task_id (the same state that's
    only cleared on idle/cancelled/stopped, see dispatch_poll_loop) as the
    honest proxy for "hermes is between tasks, not mid-turn". This is more
    conservative than poll.sh (messages only flush between tasks, not at
    every turn boundary within a task) but hermes has no per-turn signal to
    do better — flushing mid-turn on a false pane-quiet read would be worse.
    """
    if dispatch_in_flight:
        return False
    if _last_dispatched_task_id is not None:
        return False
    if not is_session_running():
        return False
    quiet = _pane_quiet_seconds(time.monotonic(), capture_pane())
    return quiet >= MSG_QUIET_SECONDS


def _record_ack(tid: str, seq: int) -> None:
    """Twin of poll.sh:_record_ack — one file per thread_id holding the
    highest seq actually delivered (high-water mark for the next poll's
    acked_seq param). Never regresses on out-of-order calls."""
    MSG_ACK_DIR.mkdir(parents=True, exist_ok=True)
    f = MSG_ACK_DIR / tid
    cur = 0
    try:
        cur = int(f.read_text(encoding="utf-8").strip() or "0")
    except (FileNotFoundError, ValueError, OSError):
        cur = 0
    if seq > cur:
        f.write_text(f"{seq}\n", encoding="utf-8")


def _anchor_was_submitted(pane: str, anchor: str) -> bool:
    """True only if `anchor` is visible AND has scrolled OUT of the input
    line — i.e. it is genuinely part of the submitted transcript, not still
    sitting un-sent in the edit buffer.

    Review fix (2026-07): grepping the whole pane for the anchor is a
    false-positive trap. `_send_to_tmux` is send-keys -l (paste text) + a
    SEPARATE send-keys Enter — exactly the swallowed-end-marker failure mode
    poll.sh's own paste_and_submit comments call out ("a TUI occasionally
    swallows the end marker, which would leave a subsequent bare Enter
    interpreted as a newline INSIDE the paste"). If that happens here, the
    footer anchor is still visible — just un-submitted, sitting on the
    pane's trailing input line — and a naive `anchor in pane` check would
    ack a message that was never actually delivered.

    We don't know hermes' prompt_toolkit glyphs precisely (no live TUI
    access from this bridge's test/dev environment), so we use the most
    robust invariant available from a bare pane capture: a submitted paste
    scrolls into the transcript and something else (an empty/reset input
    row) renders below it; an un-submitted paste is still the LAST thing
    visible in the pane. Requiring "anchor present AND not on the trailing
    non-blank line" holds regardless of the exact prompt rendering.
    """
    if anchor not in pane:
        return False
    lines = [ln for ln in pane.splitlines() if ln.strip()]
    if not lines:
        return False
    return anchor not in lines[-1]


def _verify_msg_delivered(tid: str, seq: int, timeout: float = 2.0) -> bool:
    """Verify a paste was actually SUBMITTED (not just visible, possibly
    stuck un-sent in the input line — see _anchor_was_submitted) by looking
    for its footer anchor (`[thread <tid> · seq <seq> ·`) having scrolled
    into the pane's transcript area.

    hermes' delivery mechanism (_send_to_tmux) is a blind send-keys -l with no
    built-in ack signal — unlike poll.sh's paste_and_submit --no-fail-open,
    which detects a closed gate mid-paste via its own return code. Capturing
    the pane after the send and checking the anchor's position is the closest
    equivalent verify we have for this delivery mechanism.
    """
    anchor = f"[thread {tid} · seq {seq} ·"
    deadline = time.monotonic() + timeout
    while True:
        if _anchor_was_submitted(capture_pane(), anchor):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)


def flush_msg_queue() -> None:
    """Twin of poll.sh:flush_msg_queue — paste all queued messages in seq
    order. Re-checks the gate before EVERY message (stronger than poll.sh,
    which only detects a closed gate via paste_and_submit's return code) —
    since _send_to_tmux has no built-in gate-awareness, we check explicitly.
    Verify-fail or gate-closed mid-flush → stop, remaining files stay queued,
    NO ack (at-least-once; backend redelivers seq > last_acked_seq).
    """
    for fname in msg_queue_files():
        if not msg_gate_open():
            log.info(
                "flush_msg_queue: Gate zu (Pane nicht %ss ruhig) — %s bleibt gequeued, kein Ack.",
                MSG_QUIET_SECONDS, fname,
            )
            return
        path = MSG_QUEUE_DIR / fname
        if not path.exists():
            continue
        seq_str, rest = fname.split("__", 1)
        tid = rest[: -len(".msg")]
        seq = int(seq_str)  # filename is zero-padded; the footer anchor is not
        body = path.read_text(encoding="utf-8")
        _send_to_tmux(body)
        if _verify_msg_delivered(tid, seq):
            _record_ack(tid, seq)
            path.unlink(missing_ok=True)
            log.info(
                "flush_msg_queue: seq %s (thread %s) zugestellt, ack bis seq %s",
                seq, tid, seq,
            )
        else:
            log.warning(
                "flush_msg_queue: Paste fuer seq %s (thread %s) FEHLGESCHLAGEN (Verify) — "
                "Flush gestoppt, Rest bleibt gequeued, kein Ack.",
                seq, tid,
            )
            return


def deliver_messages(payload: dict, *, dispatch_in_flight: bool = False) -> None:
    """Twin of poll.sh:deliver_messages — entry point from the poll loop
    (comm_v2 path). Queues new messages, then flushes only at the turn gate.
    """
    queue_or_deliver(payload)
    pending = msg_queue_files()
    if not pending:
        return
    if not is_session_running():
        log.warning(
            "deliver_messages: %d message(s) queued but tmux not running — skipping flush",
            len(pending),
        )
        return
    if msg_gate_open(dispatch_in_flight=dispatch_in_flight):
        flush_msg_queue()
    else:
        log.info(
            "deliver_messages: Gate zu (Pane nicht %ss ruhig / Dispatch in flight) — "
            "%d Message(s) bleiben gequeued, kein Ack.",
            MSG_QUIET_SECONDS, len(pending),
        )


def dispatch_poll_loop() -> None:
    """Poll MC for the agent's active task; tmux-dispatch new ones + new comments.

    Idempotent via _last_dispatched_task_id module cache. Network/JSON errors
    are logged and swallowed; the loop never crashes the bridge HTTP server.

    Endpoint: GET /api/v1/agent/me/poll
      - state=new_task → claim the task + return {task: {id, board_id, title, prompt, ...}}
      - state=working|idle|cancelled|stopped → no task dispatch needed
      - `new_comments` (any state) → batch of User-/System-Comments since last poll
    Note: /me/poll is a CLAIM endpoint (sets ack_at + status=in_progress on
    inbox tasks). The MC-built `task.prompt` already contains the full
    dispatch context — we wrap it with a Hermes-specific header for the pane.

    Bug 11 fix (2026-05-14): also delivers `new_comments` to the tmux session
    (previously ignored — Host-Agents missed User-Comments while idle).

    W2 bridge parity (2026-07): also consumes `new_messages` via the comm_v2
    Turn-Grenzen-Gate (deliver_messages) — a no-op unless the backend sends
    the key (comm_v2 pilot agents only). Also fixes a dedup bug: redispatch
    of the SAME task_id with a NEW dispatch_attempt_id (e.g. review_rejection
    redispatch) used to be silently swallowed because the in-memory cache
    only cleared on idle/cancelled/stopped, never on same-task revision.
    """
    global _last_dispatched_task_id, _last_dispatched_attempt_id
    try:
        env = load_env_from_file(ENV_FILE)
        base_url = env.get("MC_BASE_URL")
        token = env.get("MC_AGENT_TOKEN")
        if not base_url or not token:
            log.error(
                "dispatch_poll_loop: MC_BASE_URL / MC_AGENT_TOKEN missing in %s — loop exits",
                ENV_FILE,
            )
            return
        url = f"{base_url.rstrip('/')}/api/v1/agent/me/poll"
        headers = {"Authorization": f"Bearer {token}"}
        log.info("dispatch_poll_loop: polling %s every %ss", url, DISPATCH_POLL_INTERVAL)

        import urllib.error
        import urllib.request

        while True:
            try:
                # comm_v2: attach acked_seq=<urlencoded JSON {thread_id: seq}>
                # so the backend knows what's already been delivered. Empty
                # when nothing's been acked yet — no param appended, identical
                # to a non-pilot agent's poll call (byte-identical parity).
                acked_param = build_acked_seq_param()
                poll_url = f"{url}?acked_seq={acked_param}" if acked_param else url
                req = urllib.request.Request(poll_url, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    body = resp.read().decode("utf-8")
                payload = json.loads(body) if body.strip() else None
                task = None
                if payload:
                    state = payload.get("state")
                    if state == "new_task":
                        task = payload.get("task")
                    elif state in ("idle", "cancelled", "stopped"):
                        # Agent has no active task — clear dedup cache so any
                        # re-opened or freshly assigned task can dispatch freely.
                        if _last_dispatched_task_id is not None:
                            log.info(
                                "dispatch_poll_loop: agent %s, clearing dispatch cache (was %s)",
                                state,
                                _last_dispatched_task_id[:8],
                            )
                            _last_dispatched_task_id = None
                            _last_dispatched_attempt_id = None
                # Dedup key is (task_id, attempt_id) — NOT task_id alone. A
                # redispatch of the same task_id with a fresh attempt_id (the
                # review_rejection flow poll.sh explicitly redelivers, "auch
                # bei new_task") must still fire; a redundant poll of the
                # SAME (task_id, attempt_id) before it's acked must not.
                task_attempt_id = str(task.get("dispatch_attempt_id") or "") if task else ""
                should_dispatch = bool(
                    task
                    and task.get("id")
                    and (
                        task["id"] != _last_dispatched_task_id
                        or task_attempt_id != (_last_dispatched_attempt_id or "")
                    )
                )
                # Also gates comm_v2 message delivery below: never interleave a
                # task-dispatch paste with a message-queue flush in the same
                # iteration ("Turn-Gate ... UND kein Dispatch gerade läuft").
                dispatch_happened_this_tick = False
                if should_dispatch:
                    if not is_session_running():
                        log.warning(
                            "dispatch_poll_loop: task %s available but tmux not running — calling /start",
                            task["id"],
                        )
                        try:
                            start_hermes_session()
                        except Exception as e:
                            log.error(
                                "dispatch_poll_loop: auto-start failed: %s — skipping dispatch",
                                e,
                            )
                            time.sleep(DISPATCH_POLL_INTERVAL)
                            continue
                    # Fresh context per NEW task (dispatch.py:8-18); same-task
                    # redeliveries (revision / restart) keep the context.
                    if should_reset_session(str(task["id"]), load_last_task_id()):
                        reset_tui_session()
                    prompt = _build_dispatch_prompt(task)
                    _send_to_tmux(prompt)
                    save_last_task_id(str(task["id"]))
                    _last_dispatched_task_id = task["id"]
                    _last_dispatched_attempt_id = task_attempt_id
                    dispatch_happened_this_tick = True
                    log.info(
                        "dispatch_poll_loop: dispatched task %s (%s)",
                        task["id"],
                        str(task.get("title") or "?")[:60],
                    )

                # Bug 11 fix (2026-05-14): deliver new_comments regardless of state.
                # Backend already filtered out the agent's own comments — no dedup
                # needed here. Skip silently if tmux isn't running (comments are
                # ephemeral; no auto-start to avoid spam during boot).
                new_comments = (payload or {}).get("new_comments") or []
                if new_comments:
                    if not is_session_running():
                        log.warning(
                            "dispatch_poll_loop: %d new comment(s) but tmux not running — skipping",
                            len(new_comments),
                        )
                    else:
                        comments_prompt = _build_comments_prompt(new_comments)
                        if comments_prompt:
                            _send_to_tmux(comments_prompt)
                            log.info(
                                "dispatch_poll_loop: delivered %d comment(s) to tmux",
                                len(new_comments),
                            )

                # comm_v2-Pilot: `new_messages` via Turn-Grenzen-Gate. Only when
                # the backend sends the key (non-pilot agents unchanged — byte-
                # identical to legacy behavior). Call even on an empty list, so a
                # previously-queued-but-not-yet-flushed message gets another
                # chance to flush once the pane goes quiet.
                if payload is not None and "new_messages" in payload:
                    deliver_messages(payload, dispatch_in_flight=dispatch_happened_this_tick)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    pass  # no active task — normal
                else:
                    log.warning("dispatch_poll_loop: HTTP %s — %s", e.code, e.reason)
            except Exception as e:
                log.warning("dispatch_poll_loop: poll error: %s", type(e).__name__)
            time.sleep(DISPATCH_POLL_INTERVAL)
    except Exception as e:
        log.exception("[fatal] dispatch_poll_loop crashed: %s", e)
        raise


# Heartbeat interval — must stay well under the backend's 90s liveness window
# (cli_terminal.list_host_session_agents: session_running = last_seen < 90s).
HEARTBEAT_INTERVAL = int(os.environ.get("HERMES_HEARTBEAT_INTERVAL", "30"))


def heartbeat_loop() -> None:
    """Keep Hermes' last_seen_at fresh so it stays visible on the Sessions page.

    Unlike Docker agents (poll.sh POSTs /heartbeat every loop), the Hermes
    native-TUI runtime has no poll window — its only heartbeats came from a
    per-turn hook, so an IDLE Hermes went stale after 90s and dropped off the
    Sessions page even though hermes-worker was alive. This daemon POSTs an
    empty /agent/me/heartbeat every HEARTBEAT_INTERVAL while the tmux session
    is running, mirroring Boss's steady cadence. /heartbeat only refreshes
    last_seen (events fire on status transitions only), so this is not noisy.
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
                if is_session_running():
                    req = urllib.request.Request(
                        url, data=b"{}", headers=headers, method="POST"
                    )
                    with urllib.request.urlopen(req, timeout=10):
                        pass
            except urllib.error.HTTPError as e:
                log.warning("heartbeat_loop: HTTP %s — %s", e.code, e.reason)
            except Exception as e:
                log.warning("heartbeat_loop: error: %s", type(e).__name__)
            time.sleep(HEARTBEAT_INTERVAL)
    except Exception as e:
        log.exception("[fatal] heartbeat_loop crashed: %s", e)
        raise


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
                "session": SESSION,
                "tmux_running": is_session_running(),
                "agent_env_present": ENV_FILE.exists(),
            })
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/start":
            try:
                result = start_hermes_session()
                self._send_json(200, result)
            except FileNotFoundError as e:
                self._send_json(412, {"error": str(e)})
            except _sp.CalledProcessError as e:
                self._send_json(500, {"error": f"tmux failed: {e}"})
            return
        if self.path == "/restart":
            try:
                _sp.run([TMUX_BIN, "kill-session", "-t", SESSION],
                        check=False, capture_output=True)
                result = start_hermes_session()
                self._send_json(200, {"ok": True, "restart": result})
            except FileNotFoundError as e:
                self._send_json(412, {"error": str(e)})
            except _sp.CalledProcessError as e:
                self._send_json(500, {"error": f"tmux failed: {e}"})
            return
        if self.path == "/stop":
            try:
                _sp.run([TMUX_BIN, "kill-session", "-t", SESSION],
                        check=False, capture_output=True)
                self._send_json(200, {"ok": True, "stopped": SESSION})
            except _sp.CalledProcessError as e:
                self._send_json(500, {"error": f"tmux failed: {e}"})
            return
        self._send_json(404, {"error": "not found"})

    def log_message(self, fmt, *args):  # noqa: A003
        log.info("%s - %s", self.address_string(), fmt % args)


def _handle_sigterm(signum, frame):  # noqa: ARG001
    log.info("[shutdown] received SIGTERM, exiting cleanly")
    sys.exit(0)


def main() -> None:
    try:
        # Phase 26 / Plan 26-05: SIGTERM handler — distinguishes graceful exit
        # from crash so launchd KeepAlive:true doesn't restart on user-initiated
        # shutdowns. Crash path takes the except branch below.
        signal.signal(signal.SIGTERM, _handle_sigterm)

        # Try start on bridge boot — non-fatal if env missing (provisioning may run later)
        try:
            start_hermes_session()
        except FileNotFoundError as e:
            log.warning("Hermes session not started: %s", e)
        # Phase 25-06: background dispatcher thread (daemon → dies with HTTP server)
        t = threading.Thread(target=dispatch_poll_loop, name="hermes-dispatcher", daemon=True)
        t.start()
        log.info("hermes-dispatcher thread started (poll every %ss)", DISPATCH_POLL_INTERVAL)
        # Steady liveness heartbeat so Hermes stays on the Sessions page while idle.
        hb = threading.Thread(target=heartbeat_loop, name="hermes-heartbeat", daemon=True)
        hb.start()
        log.info("hermes-heartbeat thread started (POST every %ss)", HEARTBEAT_INTERVAL)
        server = http.server.HTTPServer((HOST, PORT), Handler)
        log.info("hermes-bridge listening on %s:%d", HOST, PORT)
        server.serve_forever()
        log.info("[shutdown] hermes-bridge main loop exited normally")
    except SystemExit:
        # Clean exit path (SIGTERM handler) — re-raise without [fatal] log.
        raise
    except Exception as e:
        log.exception("[fatal] hermes-bridge main crashed: %s", e)
        log.error("[fatal] bridge exiting due to %s", type(e).__name__)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
