#!/usr/bin/env python3
"""grok-bridge.py — host-side bridge for the Grok Build CLI (ADR-066 / ADR-068).

v2 TUI paste model (ADR-068). The fleet-wide ban on the CLI print/headless mode
(`grok -p` / `--output-format streaming-json`) — it bills extra on Claude Code and
Mark wants ONE uniform delivery model across the fleet — retires v1's per-dispatch
subprocess. Instead, mirroring the Hermes host worker (ADR-029) and the cli-bridge
poll.sh mechanic, a SINGLE persistent `grok` TUI runs in a tmux session; this bridge
polls MC and PASTES each dispatch into that TUI. The grok agent works interactively
and drives its OWN MC lifecycle (`mc ack|comment|finish|blocked`) via the copied
`mc` CLI — exactly like every claude/hermes host agent. The BRIDGE never closes a
task: an unfinished run stays in_progress until the agent finishes/blocks it, and a
silently stalled turn is un-stuck by a pane-capture no-progress nudge, not by the
bridge forcing a terminal state.

Pattern source:
  - scripts/hermes-bridge.py — poll loop, steady heartbeat, SIGTERM handling,
    localhost-only HTTP control server, tmux session autostart.
  - docker/shared/poll.sh — paste_and_submit (load-buffer / paste-buffer +
    bracketed-paste-end marker + Enter), tmux set-environment task context,
    attempt-id dedup guard.
  - docker/omp-bridge/bridge.py (NativeTuiController) — pane-capture readiness +
    no-progress watchdog.

There is NO `-p`, NO `--prompt-file`, NO streaming-json subprocess anywhere — grok
runs as a long-lived interactive TUI only.

Grok speaks ONLY to xAI cloud over its own OAuth (~/.grok/auth.json, auto refresh).
There is NO OPENAI_*/ANTHROPIC_* provider env and NO MC-bound model endpoint — the
runtime binding for a grok agent is a display/anchor only (ADR-066). agent.env
carries just the MC_* control-plane vars.

Endpoints:
  GET  /health   -> {"status","harness","session","tmux_running","agent_env_present"}
  POST /start    -> start the grok tmux session if not running
  POST /restart  -> kill + restart the grok tmux session (re-sources agent.env)
  POST /stop     -> send Escape into the session (interrupt the current turn)

Auto-loaded by ~/Library/LaunchAgents/com.mc.grok-bridge.plist at login.
"""
from __future__ import annotations

import http.server
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess as _sp
import sys
import threading
import time
from pathlib import Path
from typing import Optional

# Ports: 18792/18793 = free-code-bridge, 18794 = hermes-bridge, 18795 = grok-bridge.
PORT = 18795
HOST = "127.0.0.1"  # localhost only, never the wildcard bind (same L-C rule as hermes-bridge)
HOME_DIR = Path(os.environ.get("HOME_HOST", str(Path.home())))
GROK_BIN = shutil.which("grok") or "/opt/homebrew/bin/grok"
TMUX_BIN = shutil.which("tmux") or "/opt/homebrew/bin/tmux"
CONFIG_DIR = HOME_DIR / ".mc/agents/grok"
WORKSPACE = HOME_DIR / ".mc/workspaces/grok"
ENV_FILE = CONFIG_DIR / "agent.env"
LOG_DIR = CONFIG_DIR / "logs"
HARNESS = "grok"

# The tmux session name the grok TUI runs in. Slug convention (agent "Grok" →
# "grok"); the Sessions page mounts it via _HOST_AGENT_TMUX_TARGETS["grok"].
SESSION = os.environ.get("GROK_SESSION", "grok")
# The prompt glyph the grok TUI renders when it is idle and ready for input.
# Verified on the live host: a `❯` inside a box, Statuszeile "Grok 4.5 (high)".
READY_GLYPH = os.environ.get("GROK_READY_GLYPH", "❯")
# Unattended TUI: acceptEdits so file edits don't stall on an approval prompt.
GROK_PERMISSION_MODE = os.environ.get("GROK_PERMISSION_MODE", "acceptEdits")
# Extra flags for the grok launch (space-separated); empty by default.
GROK_EXTRA_FLAGS = os.environ.get("GROK_EXTRA_FLAGS", "")

# Session-reset on GENUINE task switch (ADR-068 addendum). Dispatch semantics
# (dispatch.py:8-18) promise a fresh context per NEW task; the TUI paste model
# would otherwise accumulate every task into one conversation. `/new` verified
# live 2026-07-12: instant fresh session, no picker (workspace is not a git
# repo, so grok never offers the worktree variant).
RESET_COMMAND = os.environ.get("GROK_RESET_COMMAND", "/new")
# Last dispatched task id, persisted to DISK so a bridge restart cannot erase
# switch detection (the in-memory dedup below resets on restart; this doesn't).
LAST_TASK_FILE = LOG_DIR / "last-task-id"

# Path the copied `mc` CLI reads task context from (mc_cli/config.py:from_env —
# file wins over stale process env). poll.sh writes it for the claude fleet; this
# bridge MUST re-provide it BEFORE each paste or the agent's own `mc ack|finish`
# fail. Same 3-key contract as docker/shared/poll.sh.
MC_CONTEXT_ENV_PATH = os.environ.get("MC_CONTEXT_ENV_PATH", "/tmp/mc-context.env")

# Poll / heartbeat cadence (env-overridable).
DISPATCH_POLL_INTERVAL = int(os.environ.get("GROK_DISPATCH_POLL_INTERVAL", "5"))
# Heartbeat must stay well under the backend's 90s liveness window
# (cli_terminal.list_host_session_agents: session_running = last_seen < 90s).
HEARTBEAT_INTERVAL = int(os.environ.get("GROK_HEARTBEAT_INTERVAL", "30"))
# How long to wait for the TUI prompt glyph after (re)starting the session.
READY_TIMEOUT = float(os.environ.get("GROK_READY_TIMEOUT", "45"))
# No-progress nudge: if a dispatched task's pane shows no change for this long,
# paste a gentle reminder to drive the mc lifecycle. The bridge never closes the
# task itself — this only un-sticks a silently stalled turn.
NUDGE_IDLE_TIMEOUT = float(os.environ.get("GROK_NUDGE_IDLE_TIMEOUT", "300"))
NUDGE_MAX = int(os.environ.get("GROK_NUDGE_MAX", "2"))
WATCHDOG_INTERVAL = float(os.environ.get("GROK_WATCHDOG_INTERVAL", "30"))

# Interaction Model 2.0 (comm_v2 pilot, W2 bridge parity): Thread-message queue
# + turn-gate, mirroring docker/shared/poll.sh's queue_or_deliver/msg_gate_open/
# flush_msg_queue/_record_ack/build_acked_seq_param. Only exercised when the
# backend actually returns a `new_messages` field on /me/poll (agent.comm_v2=true)
# — the old `new_comments` path below is untouched and byte-identical otherwise.
# grok has no native turn-state signal (unlike omp's OMP_TURN_SIGNAL_FILE), so
# the gate is a pane-quiet heuristic: the tmux pane must be unchanged for
# MSG_QUIET_SECONDS AND no dispatch/reset must be in flight.
MSG_QUIET_SECONDS = float(os.environ.get("GROK_MSG_QUIET_SECONDS", "10"))
MSG_QUEUE_DIR = CONFIG_DIR / "msg-queue"
MSG_ACK_DIR = CONFIG_DIR / "msg-acked"

# W2.1 nudge+pull (ADR-071, port of poll.sh's deliver_messages_nudge): in
# nudge mode the bridge pastes only a short 📬 wake-up line at the turn gate;
# the agent pulls content itself via `mc inbox` and the SERVER-side ack from
# that call advances the cursor — the bridge never acks locally. Default stays
# "paste" (byte-identical behavior) until the per-agent live gate has passed.
MSG_DELIVERY_MODE = (os.environ.get("MSG_DELIVERY_MODE", "paste").strip() or "paste")
NUDGE_REMIND_SECONDS = float(os.environ.get("NUDGE_REMIND_SECONDS", "600"))
NUDGE_STATE_FILE = CONFIG_DIR / "msg-nudge-state"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("grok-bridge")

# Dispatch-dedup: a task stays state=new_task on /me/poll until the agent runs
# `mc ack`, so we guard re-paste by (task_id, attempt_id) — the poll.sh pattern.
_last_dispatched_task_id: Optional[str] = None
_last_dispatched_attempt_id: Optional[str] = None

# Watchdog state — the currently active (dispatched) task and its pane progress.
_state_lock = threading.Lock()
_active_task: Optional[dict] = None
_last_pane: str = ""
_last_progress_ts: float = 0.0
_nudges_sent: int = 0

# Message-gate pane-quiet tracking (separate clock from the no-progress
# watchdog above — the gate must open even when there is no active task, e.g.
# a follow-up message arriving while the agent sits idle at the prompt).
_msg_gate_lock = threading.Lock()
_msg_gate_last_pane: str = ""
_msg_gate_last_change_ts: float = 0.0

# Review fix (2026-07-21, twin of hermes-bridge.py's identical fix, live-
# confirmed there on the Qwen status line): a TUI can render VOLATILE status
# text (ticking timer, token/context counter, spinner frame) that changes
# every second even while genuinely idle between tasks. Compared raw, the
# pane never reads "unchanged" and the message gate never opens — messages
# hang forever, not just delayed. grok's idle pane currently looks static,
# but normalizing here costs nothing and closes the same class of bug if the
# TUI ever grows a ticking status line. Extendable via
# MSG_QUIET_VOLATILE_PATTERNS (newline-separated extra regexes).
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
    elsewhere in the line/pane still differs and still resets the clock. Only
    feeds the message-gate quiet clock (_msg_gate_pane_quiet) — the separate
    no-progress watchdog clock (_last_pane/_last_progress_ts, _watchdog_tick)
    is untouched, so a stuck task still nudges on real inactivity."""
    return _VOLATILE_RE.sub("•", pane)
# True while dispatch_task()/reset_tui_session() is actively driving the TUI —
# the message gate must stay closed so a queued message is never pasted mid-turn.
_dispatch_in_flight: bool = False

# /clear-on-done bugfix: reset_tui_session() fires once per task that finishes
# WITHOUT a follow-up task (idle/cancelled/stopped) so context never grows
# unbounded after done. Idempotency guard keyed by the finished task id, kept
# both in-memory and on disk (bridge-restart-safe, same pattern as
# LAST_TASK_FILE) so a repeated idle poll for the SAME finished task never
# re-fires /new.
_last_reset_task_id: Optional[str] = None
LAST_RESET_TASK_ID_FILE = LOG_DIR / "last-reset-task-id"


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
    HOME_DIR (so the grok TUI resolves ~/.grok/auth.json on the host).
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

    The grok agent's own `mc ack|comment|finish|blocked` read TASK_ID / BOARD_ID /
    X_DISPATCH_ATTEMPT_ID via mc_cli/config.py:from_env, which resolves this file
    FIRST (it wins over the previous dispatch's process env). Without it `mc ack`
    fails ("TASK_ID … müssen gesetzt sein") and status calls are rejected 409
    ("Missing X-Dispatch-Attempt-Id"). Best-effort: an unwritable file must never
    crash the serve loop.
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


# ── tmux session management (start / readiness / paste) ─────────────────────────


def _tmux(args: list[str], *, env: Optional[dict] = None) -> _sp.CompletedProcess:
    """Run one tmux command, capturing output. Never raises — a tmux hiccup must
    not crash the poll/watchdog loops (caller inspects returncode/stdout)."""
    try:
        return _sp.run(
            [TMUX_BIN, *args], capture_output=True, text=True, timeout=15,
            env=env or os.environ,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("tmux %s failed: %s: %s", args[:2], type(e).__name__, e)
        return _sp.CompletedProcess(args, returncode=1, stdout="", stderr=str(e))


def is_session_running() -> bool:
    return _tmux(["has-session", "-t", SESSION]).returncode == 0


def capture_pane() -> str:
    """Return the visible pane text of the grok TUI (empty on failure)."""
    r = _tmux(["capture-pane", "-p", "-t", SESSION])
    return r.stdout if r.returncode == 0 else ""


def _grok_launch_cmd() -> list[str]:
    """Argv for the interactive grok TUI (NOT headless — no -p / --prompt-file).

    `--no-alt-screen` runs inline (scrollback-native, mountable in xterm.js);
    `--permission-mode acceptEdits` lets an unattended run apply edits without
    stalling on an approval prompt. Matches the live host session.
    """
    cmd = [GROK_BIN, "--no-alt-screen", "--permission-mode", GROK_PERMISSION_MODE]
    if GROK_EXTRA_FLAGS.strip():
        cmd += shlex.split(GROK_EXTRA_FLAGS)
    return cmd


def _grok_launch_shell_cmd() -> str:
    """Shell line for `tmux new-session`: source agent.env in-shell, then exec grok.

    tmux windows inherit their environment from the tmux SERVER, not from the
    client that runs `new-session` — passing env= to the subprocess never
    reaches the TUI. A stale server-global (the hermes 13 KB token incident)
    once poisoned MC_AGENT_TOKEN for every new session this way. Sourcing the
    file inside the window shell (hermes entrypoint pattern) makes agent.env
    the single source of truth regardless of server state.
    """
    # tmux runs the window command through `sh -c` itself — return the compound
    # line directly instead of double-wrapping (nested quoting would break).
    # MC_API_URL: the mc CLI reads MC_API_URL (not agent.env's MC_BASE_URL) and
    # would silently fall back to its localhost default — correct on this host,
    # but only by accident. Export it explicitly (agent.env wins if it ever
    # carries the key) so the agent's own `mc inbox` calls are deterministic.
    grok = " ".join(shlex.quote(c) for c in _grok_launch_cmd())
    return (
        f"set -a; . {shlex.quote(str(ENV_FILE))}; set +a; "
        f': "${{MC_API_URL:=http://localhost:8000}}"; export MC_API_URL; '
        f"exec {grok}"
    )


def wait_for_agent_healthy(timeout: float = READY_TIMEOUT) -> bool:
    """Poll the pane until the grok prompt glyph appears (TUI ready for input)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if READY_GLYPH in capture_pane():
            return True
        time.sleep(0.5)
    return READY_GLYPH in capture_pane()


def start_grok_session() -> dict:
    """Start the persistent grok TUI in a detached tmux session (idempotent).

    Mirrors the live host: `tmux new-session -d -s grok -c <workspace> 'grok
    --no-alt-screen …'`, mouse on, then wait for the prompt glyph. Requires
    agent.env (the grok OAuth + MC_* control-plane); raises FileNotFoundError if
    it is missing (provisioning may run later — main() tolerates that).
    """
    if not ENV_FILE.exists():
        raise FileNotFoundError(f"agent.env missing at {ENV_FILE} — provision first")
    if is_session_running():
        return {"status": "already_running", "session": SESSION}
    env = load_env_from_file(ENV_FILE)
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    r = _tmux(
        ["new-session", "-d", "-s", SESSION, "-c", str(WORKSPACE),
         _grok_launch_shell_cmd()],
        env=env,
    )
    if r.returncode != 0:
        raise RuntimeError(f"tmux new-session failed: {r.stderr.strip()}")
    _tmux(["set-option", "-t", SESSION, "mouse", "on"], env=env)
    ready = wait_for_agent_healthy()
    log.info("started grok TUI in tmux session %s (ready=%s)", SESSION, ready)
    return {"status": "started", "session": SESSION, "ready": ready}


# Review fix (MAJOR, 2026-07-21): the watchdog thread (nudge on stall) and the
# poll-loop thread (dispatch + comm_v2 message flush) can both decide to paste
# at ~the same moment — precisely DURING a stall, the pane is quiet AND
# _dispatch_in_flight is False, which is exactly when the watchdog fires a
# nudge. Without serialization two concurrent tmux send-keys sequences would
# interleave keystrokes into the SAME pane. One lock around every actual paste
# op closes that race regardless of which caller (dispatch/nudge/msg-flush).
_paste_lock = threading.Lock()


def paste_and_submit(text: str) -> None:
    """Load `text` into the tmux paste-buffer, paste into the grok TUI, submit.

    Mirrors docker/shared/poll.sh:paste_and_submit exactly — a bracketed-paste-end
    marker (ESC [ 2 0 1 ~, hex `1b 5b 32 30 31 7e`) is sent BEFORE Enter because a
    TUI occasionally swallows the end marker, which would leave a subsequent bare
    Enter interpreted as a newline INSIDE the paste instead of a submit. Content
    goes through a file + load-buffer so arbitrary/multiline text is never
    misinterpreted as tmux key-names. `_paste_lock` serializes every caller
    (dispatch, watchdog nudge, message flush) so two threads can never
    interleave keystrokes into the same tmux pane.
    """
    with _paste_lock:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        buf = LOG_DIR / "dispatch.paste"
        buf.write_text(text, encoding="utf-8")
        _tmux(["load-buffer", str(buf)])
        _tmux(["paste-buffer", "-t", SESSION])
        time.sleep(0.3)
        _tmux(["send-keys", "-t", SESSION, "-H", "1b", "5b", "32", "30", "31", "7e"])
        time.sleep(0.2)
        _tmux(["send-keys", "-t", SESSION, "Enter"])


# ── comm_v2 message queue (crash-safe) + turn-gate + flush ──────────────────────
#
# Mirrors docker/shared/poll.sh's Interaction-Model-2.0 section (build_acked_seq_
# param / queue_or_deliver / msg_gate_open / flush_msg_queue / _record_ack /
# deliver_messages) so the grok host bridge gets the same crash-safe, at-least-
# once delivery contract the Docker fleet already has. Backend is unchanged: it
# only returns `new_messages` + accepts `acked_seq` when agent.comm_v2=true.


def build_acked_seq_param() -> str:
    """Serialize MSG_ACK_DIR (thread_id -> highest actually-pasted seq) as a
    URL-encoded JSON object for the `acked_seq` poll query param. Empty string
    when nothing has been acked yet (poll.sh: no param appended)."""
    if not MSG_ACK_DIR.exists():
        return ""
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
    import urllib.parse
    return urllib.parse.quote(json.dumps(acked))


def _record_ack(thread_id: str, seq: int) -> None:
    """Persist the high-water mark seq actually pasted for `thread_id` — never
    acked at queue time, only after a verified paste (at-least-once)."""
    MSG_ACK_DIR.mkdir(parents=True, exist_ok=True)
    f = MSG_ACK_DIR / thread_id
    cur = 0
    try:
        cur = int(f.read_text(encoding="utf-8").strip() or 0)
    except (FileNotFoundError, ValueError, OSError):
        cur = 0
    if seq > cur:
        f.write_text(str(seq), encoding="utf-8")


def _queue_file_name(seq: int, thread_id: str) -> str:
    return f"{seq:08d}__{thread_id}.msg"


def _parse_queue_file_name(name: str) -> tuple[int, str]:
    seq_str, _, rest = name.partition("__")
    tid = rest[:-4] if rest.endswith(".msg") else rest
    return int(seq_str), tid


def build_message_footer(thread_id: str, seq: int, sender: str, message_type: str) -> str:
    """The footer anchor line every queued message ends with — flush_msg_queue
    searches the post-paste pane tail for this exact prefix to verify delivery
    before acking. Same shape as poll.sh's queue_or_deliver footer."""
    return f"[thread {thread_id} · seq {seq} · von {sender} · typ {message_type}]"


def _msg_prefix(seq: int, thread_id: str) -> str:
    """Unique first-line prefix for a queued message's paste body.

    Live finding (2026-07-22, second measurement round): grok's TUI REDRAWS
    its transcript per turn instead of appending — the earlier count-increase
    verify (module note below) compares the turn-event count before/after a
    paste, but a redrawn transcript can show the SAME count both times (old
    turn's events cleared, new turn's events rendered) — count never
    increases, so that signal alone under-detects on this TUI. Also: grok
    shows a submitted multi-line paste as ONE truncated transcript line
    ending in `…`, and the "# Neue Nachricht" Markdown header is stripped —
    only the BODY's opening survives. Prepending this prefix to the body's
    FIRST line (not as a separate header line, which gets stripped) means it
    is exactly what a truncated echo keeps — grok-local formatting only, the
    backend's `new_messages` wire format / footer contract is untouched.
    Unique per (thread_id, seq) — the primary verify signal below therefore
    only needs bare PRESENCE (outside the composer box), no stale-false-pass
    is possible the way it was with the turn-event count."""
    return f"[msg {seq} · {thread_id[:8]}]"


def build_message_file_body(m: dict) -> str:
    """Paste-ready text for one queued message, matching poll.sh's format
    plus the grok-local unique-prefix-on-body (see _msg_prefix)."""
    tid = str(m.get("thread_id") or "")
    seq = int(m.get("seq") or 0)
    body = str(m.get("body") or "")
    footer = build_message_footer(tid, seq, str(m.get("sender") or "?"), str(m.get("message_type") or "?"))
    prefixed_body = f"{_msg_prefix(seq, tid)} {body}"
    return "\n".join(["# Neue Nachricht (Interaction 2.0)", "", prefixed_body, "", footer])


def queue_new_messages(messages: list) -> int:
    """Persist each new_messages entry as its own seq-named file in
    MSG_QUEUE_DIR BEFORE any paste is attempted (crash-safe). Idempotent:
    redelivery of the same (seq, thread_id) overwrites the identical file."""
    if not messages:
        return 0
    MSG_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    n = 0
    for m in messages:
        try:
            seq = int(m["seq"])
            tid = str(m["thread_id"])
        except (KeyError, TypeError, ValueError):
            log.warning("queue_new_messages: malformed message entry skipped: %r", m)
            continue
        path = MSG_QUEUE_DIR / _queue_file_name(seq, tid)
        path.write_text(build_message_file_body(m), encoding="utf-8")
        n += 1
    return n


def msg_queue_files() -> list[Path]:
    """Queued message files in seq order (zero-padded filenames sort correctly)."""
    if not MSG_QUEUE_DIR.exists():
        return []
    return sorted(MSG_QUEUE_DIR.glob("*.msg"))


def _msg_gate_pane_quiet(now: float, pane: str) -> bool:
    """Advance the pane-quiet clock; True once the pane has been unchanged for
    >= MSG_QUIET_SECONDS. Pure w.r.t. tmux (caller supplies now/pane) — same
    shape as _watchdog_tick so it is unit-testable without a real TUI.

    `pane` is normalized (volatile substrings stripped) BEFORE the compare —
    see _normalize_volatile.
    """
    global _msg_gate_last_pane, _msg_gate_last_change_ts
    pane = _normalize_volatile(pane)
    with _msg_gate_lock:
        if pane != _msg_gate_last_pane:
            _msg_gate_last_pane = pane
            _msg_gate_last_change_ts = now
            return False
        return (now - _msg_gate_last_change_ts) >= MSG_QUIET_SECONDS


def msg_gate_open(*, dispatch_in_flight: bool = False) -> bool:
    """Turn-gate for message delivery: pane quiet for MSG_QUIET_SECONDS, no
    dispatch/reset currently driving the TUI, AND no task currently active.

    Review fix (MAJOR, 2026-07-21): pane-quiet alone is NOT a turn boundary.
    poll.sh's real msg_gate_open checks `detect_turn_state == idle` — a
    genuine "claude is not working" signal. grok has no such signal (no
    OMP_TURN_SIGNAL_FILE-style hook) — a long silent tool call or a thinking
    pause INSIDE an active task also holds the pane still for >=
    MSG_QUIET_SECONDS, which previously opened the gate and would have pasted
    a message mid-task-turn, corrupting the agent's in-flight run. We close
    that gap the same way scripts/hermes-bridge.py's twin fix does: require
    `_active_task is None` (the exact state dispatch_task()/_clear_active()
    already track for the no-progress watchdog) as the honest proxy for
    "grok is between tasks, not mid-turn". More conservative than poll.sh
    (messages only flush between tasks, never at an in-task pause) but grok
    has no per-turn signal to do better — flushing on a false pane-quiet
    read inside an active task would be worse than a delayed flush.

    `dispatch_in_flight` (explicit param, default False) lets a caller in the
    SAME poll-loop tick that just dispatched/reset the TUI close the gate even
    though the module-global `_dispatch_in_flight` flag has already been reset
    to False by the time deliver_messages() runs later in that tick (minor
    review fix — mirrors hermes-bridge's `dispatch_happened_this_tick`)."""
    if dispatch_in_flight or _dispatch_in_flight:
        return False
    with _state_lock:
        if _active_task is not None:
            return False
    if not is_session_running():
        return False
    return _msg_gate_pane_quiet(time.monotonic(), capture_pane())


def _anchor_was_submitted(pane: str, anchor: str) -> bool:
    """True only if `anchor` is visible AND has scrolled OUT of the composer's
    trailing input line — i.e. it is genuinely part of the submitted
    transcript, not still sitting un-sent in the edit buffer.

    Review fix (MAJOR, 2026-07-21): grepping the whole pane for the anchor is
    a false-positive trap. paste_and_submit's own docstring already documents
    the failure mode it defends against — a TUI occasionally swallows the
    bracketed-paste-end marker, leaving the following bare Enter interpreted
    as a newline INSIDE the paste instead of a submit. When that happens the
    footer anchor is still visible on the pane — just un-submitted, sitting
    on the composer's trailing line — and the old `anchor in pane` check
    would ack a message that was never actually delivered.

    We don't have live access to grok's exact TUI rendering (box-drawing
    border characters etc. are unconfirmed for this specific CLI, unlike the
    empirically-verified omp composer-border check in
    docker/omp-bridge/bridge.py's `_composer_state`), so — matching
    scripts/hermes-bridge.py's identical fix for the identical problem — we
    use the most robust invariant available from a bare pane capture without
    assuming a specific border style: a submitted paste scrolls into the
    transcript and something else (an idle/reset composer row) renders below
    it; an un-submitted paste is still the LAST non-blank thing visible in
    the pane. Requiring "anchor present AND not on the trailing non-blank
    line" holds regardless of the exact prompt/composer rendering."""
    if anchor not in pane:
        return False
    lines = [ln for ln in pane.splitlines() if ln.strip()]
    if not lines:
        return False
    return anchor not in lines[-1]


# Live finding (2026-07-22): grok collapses a submitted multi-line paste into
# a SINGLE truncated transcript line ending in `…` — the Markdown header is
# stripped and only the body's opening is shown; the footer anchor line (the
# LAST line of what we pasted) never appears in the pane AT ALL. That means
# _anchor_was_submitted always returns False for a genuinely delivered
# message — the exact 18-redelivery pattern the claude-CLI collapse bug (#126)
# hit, just via a different rendering quirk. First fix attempt: a rendering-
# agnostic COUNT-INCREASE signal instead of anchor presence. Real pane
# observed after a successful submit+reply:
#   MARKER-GROK-9313: Zweiter Zustelltest. Bitte kurz bestaetigen. …
#   ◆ user_prompt_submit  [hooks: 1]
#   ◆ Thought for 0.1s
#   Bestätigt: MARKER-GROK-9313 erhalten.                              10:44 PM
#   Turn completed in 3.6s.
#   ╭──────────…──────╮
#   │ ❯                │
#   ╰──────…── Grok 4.5 (high) · always-approve ─╯
#
# Live finding round 2 (2026-07-22, 23:15): the count-increase signal STILL
# under-detected — grok's TUI REDRAWS its transcript per turn instead of
# appending. Before a paste: 1x hook-fire + 1x "Turn completed" from the
# PREVIOUS turn = 2. After paste+submit: previous turn's lines cleared away,
# new turn's lines rendered = 2 again. The count never grows even though the
# message genuinely delivered. Fixed with a THIRD, now-primary signal: a
# unique prefix (`_msg_prefix`) prepended to the body's first line survives
# both the truncated-echo collapse (it's the very first thing shown) and the
# per-turn redraw (uniqueness makes bare presence sufficient — no stale-line
# false-pass is possible the way it was for the turn-event count). See
# _prefix_was_submitted. The count-increase and anchor paths stay as
# additional OR fallbacks for any future grok rendering behavior.
_TURN_EVENT_RE = re.compile(r"◆\s*user_prompt_submit|Turn completed in")


def _count_turn_events(pane: str) -> int:
    """Count grok TUI turn-boundary event lines (`◆ user_prompt_submit` hook
    fire, `Turn completed in ...`) in a pane capture. A COUNT, never mere
    presence, is required by the caller — stale events from earlier turns
    stay in the tmux scrollback, so presence alone proves nothing."""
    return len(_TURN_EVENT_RE.findall(pane))


def _composer_has_leftover_text(pane: str) -> bool:
    """True if the composer's input row (`│ <glyph> ... │`) still shows text
    after the prompt glyph — i.e. a paste is still sitting there un-submitted
    (e.g. a genuinely swallowed Enter). Scans from the bottom so the LAST
    matching row wins — scrollback may hold a stale composer rendering from
    an earlier redraw. Returns False (can't positively confirm leftover text)
    when no composer row is found at all — never guess "leftover" from an
    absent capture."""
    for line in reversed(pane.splitlines()):
        stripped = line.strip()
        if READY_GLYPH not in stripped:
            continue
        if "│" not in stripped and "|" not in stripped:
            continue
        after = stripped.split(READY_GLYPH, 1)[1]
        after = after.strip(" │|")
        return bool(after)
    return False


def _prefix_was_submitted(pane: str, prefix: str) -> bool:
    """True iff the unique `_msg_prefix` appears on a NON-composer line — i.e.
    it has scrolled into the transcript, not sitting unsubmitted inside the
    composer box. Redraw-safe (no before/after baseline needed — see the
    module note above) and truncation-safe (the prefix is the very first
    thing pasted, so a single collapsed `…` echo line still shows it).

    `prefix` is unique per (thread_id, seq), so bare presence is sufficient
    proof — unlike the old footer anchor (shared literal `[thread`/`seq`
    shape across every message) a stale prefix from an EARLIER message can
    never collide with THIS message's prefix. The only thing still worth
    excluding is a swallowed-Enter case where the prefix is sitting un-sent
    inside the composer box — those lines start with the box border char
    (`│`/`|`, see the live pane in the module note: `│ ❯ ... │`)."""
    for line in pane.splitlines():
        if prefix not in line:
            continue
        if line.strip().startswith(("│", "|")):
            continue  # still inside the composer box — unsubmitted
        return True
    return False


def _verify_msg_delivered(tid: str, seq: int, *, events_before: int, timeout: float = 5.0) -> bool:
    """Poll capture_pane() up to `timeout`s for a submit signal — a single
    immediate capture can catch the TUI mid-redraw right after Enter (grok
    can take >2s to render its echo, hence the 5s default). Three
    independent signals, any one confirms delivery (OR — see the module note
    above for why no single signal alone is reliable across grok's rendering
    quirks):
      1. PRIMARY: the unique `_msg_prefix` appears outside the composer box
         (_prefix_was_submitted) — survives both the truncated-echo collapse
         and the per-turn transcript redraw.
      2. FALLBACK: the turn-event count grew past `events_before`
         (snapshotted BEFORE the paste) AND the composer shows no leftover
         text.
      3. FALLBACK: the footer anchor scrolled out of the composer's trailing
         line (_anchor_was_submitted) — kept for a future grok build that
         renders full (uncollapsed) transcript lines again."""
    deadline = time.monotonic() + timeout
    prefix = _msg_prefix(seq, tid)
    anchor = f"[thread {tid} · seq {seq} ·"
    while True:
        pane = capture_pane()
        if _prefix_was_submitted(pane, prefix):
            return True
        if _count_turn_events(pane) > events_before and not _composer_has_leftover_text(pane):
            return True
        if _anchor_was_submitted(pane, anchor):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)


def flush_msg_queue() -> None:
    """Paste all queued messages in seq order. Each paste is verified via
    _verify_msg_delivered (unique prefix outside the composer box, OR
    turn-event count increase + empty composer, OR the anchor having
    scrolled out of the composer) — only a verified paste acks (High-Water
    in MSG_ACK_DIR) and deletes the queue file. The first verify-fail (or
    gate closing mid-flush) stops the flush; the rest stays queued, unacked
    — at-least-once, the backend redelivers seq > last_acked_seq on the
    next poll."""
    global _dispatch_in_flight
    for path in msg_queue_files():
        if not msg_gate_open():
            log.info("flush_msg_queue: Gate zu waehrend Flush — Rest bleibt gequeued, kein Ack.")
            return
        try:
            seq, tid = _parse_queue_file_name(path.name)
        except (ValueError, IndexError):
            log.warning("flush_msg_queue: kann Dateiname nicht parsen, ueberspringe: %s", path.name)
            continue
        body = path.read_text(encoding="utf-8")
        # Snapshot BEFORE the paste — the verify needs a baseline to detect
        # an INCREASE, not just presence, of turn-boundary events.
        events_before = _count_turn_events(capture_pane())
        _dispatch_in_flight = True
        try:
            paste_and_submit(body)
            time.sleep(0.5)  # let the TUI render the submitted text into scrollback
            delivered = _verify_msg_delivered(tid, seq, events_before=events_before)
        finally:
            _dispatch_in_flight = False
        if delivered:
            _record_ack(tid, seq)
            path.unlink(missing_ok=True)
            log.info("flush_msg_queue: Message seq %s (thread %s) zugestellt, ack bis seq %s", seq, tid, seq)
        else:
            log.warning(
                "flush_msg_queue: Verify fuer seq %s (thread %s) FEHLGESCHLAGEN (weder Prefix ausserhalb "
                "des Composers, noch Event-Count gewachsen+Composer leer, noch Anker aus dem Composer "
                "gescrollt) — Flush gestoppt, Rest bleibt gequeued, kein Ack.", seq, tid,
            )
            return


# ── nudge+pull delivery (MSG_DELIVERY_MODE=nudge) ───────────────────────────────


def _nudge_thread_seqs(messages: list) -> dict[str, int]:
    """Per-thread max seq from a new_messages payload (seq is only unique
    WITHIN a thread — a global max would dedup across threads incorrectly,
    the exact poll.sh Phase-B review MAJOR)."""
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


def _nudge_state_read() -> dict[str, tuple[int, float]]:
    """NUDGE_STATE_FILE → {thread_id: (last_nudged_seq, epoch)}. Malformed
    lines are skipped (a corrupt state file must degrade to re-nudging, never
    to crashing the poll loop)."""
    state: dict[str, tuple[int, float]] = {}
    try:
        text = NUDGE_STATE_FILE.read_text(encoding="utf-8")
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


def _nudge_state_write(seqs: dict[str, int], now: float) -> None:
    """Overwrite NUDGE_STATE_FILE with the high-water for every currently
    pending thread — one nudge covers ALL of them (the agent reads everything
    via `mc inbox`)."""
    NUDGE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    NUDGE_STATE_FILE.write_text(
        "".join(f"{tid} {seq} {int(now)}\n" for tid, seq in seqs.items()),
        encoding="utf-8",
    )


def build_nudge_text(global_max: int, epoch: int) -> str:
    """Single-line wake-up, identical wording to poll.sh. The `(bis seq N,
    EPOCH)` token doubles as the unique verify signal and sits within the
    first ~40 chars so grok's truncated single-line echo still shows it."""
    return (
        f"📬 Neue Nachrichten (bis seq {global_max}, {epoch}) — "
        f"lies sie jetzt mit: mc inbox"
    )


def _nudge_token_visible(pane: str, token: str) -> bool:
    """True iff the unique nudge token appears on a NON-composer line — same
    submitted-vs-still-in-composer discrimination as _prefix_was_submitted
    (the token is unique per paste via the epoch, so bare presence outside
    the composer box is sufficient proof)."""
    for line in pane.splitlines():
        if token not in line:
            continue
        if line.strip().startswith(("│", "|")):
            continue
        return True
    return False


def _clear_stale_queue_files() -> None:
    """Paste-mode leftovers are dead weight in nudge mode — their bodies are
    never pasted, and the server keeps redelivering anything unacked until the
    agent pulls it via `mc inbox`. Deleting them loses nothing."""
    stale = msg_queue_files()
    if stale:
        for path in stale:
            path.unlink(missing_ok=True)
        log.info(
            "nudge: %d stale paste-mode queue file(s) entfernt (Inhalt kommt via mc inbox).",
            len(stale),
        )


def deliver_messages_nudge(messages: list, *, dispatch_in_flight: bool = False) -> None:
    """Nudge-mode delivery (port of poll.sh's deliver_messages_nudge): decide
    per thread whether a wake-up is due (new higher seq → immediately; no
    progress after NUDGE_REMIND_SECONDS → remind), paste ONE short line at
    the turn gate, verify via the unique seq+epoch token, then record the
    nudged high-water for every pending thread. NEVER acks — the server-side
    cursor only advances through the agent's own `mc inbox` call, and the
    backend redelivers `new_messages` until then (at-least-once)."""
    global _dispatch_in_flight
    _clear_stale_queue_files()
    seqs = _nudge_thread_seqs(messages)
    if not seqs:
        # Everything fetched+acked by the agent — reset so the next message
        # nudges immediately again.
        NUDGE_STATE_FILE.unlink(missing_ok=True)
        return

    now = time.time()
    state = _nudge_state_read()
    do_nudge = False
    for tid, mseq in seqs.items():
        last_seq, last_ts = state.get(tid, (0, 0.0))
        if mseq > last_seq:
            do_nudge = True   # new, higher seq in THIS thread → wake up now
        elif (now - last_ts) >= NUDGE_REMIND_SECONDS:
            do_nudge = True   # remind: still unacked after the grace window
    if not do_nudge:
        return

    global_max = max(seqs.values())
    if not msg_gate_open(dispatch_in_flight=dispatch_in_flight):
        log.info(
            "deliver_messages_nudge: Gate zu (Pane nicht ruhig / Dispatch in flight / "
            "Task aktiv) — Nudge aufgeschoben (bis seq=%s).", global_max,
        )
        return

    epoch = int(now)
    text = build_nudge_text(global_max, epoch)
    token = f"(bis seq {global_max}, {epoch})"
    _dispatch_in_flight = True
    try:
        paste_and_submit(text)
        time.sleep(0.5)
        deadline = time.monotonic() + 5.0
        delivered = False
        while True:
            if _nudge_token_visible(capture_pane(), token):
                delivered = True
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.2)
    finally:
        _dispatch_in_flight = False

    if delivered:
        _nudge_state_write(seqs, now)
        log.info(
            "Nudge gepastet (bis seq %s) — Agent holt Inhalt via 'mc inbox'.", global_max
        )
    else:
        log.warning(
            "Nudge-Verify fehlgeschlagen (bis seq %s) — Retry beim naechsten Poll "
            "(State unveraendert).", global_max,
        )


def deliver_messages(payload: dict, *, dispatch_in_flight: bool = False) -> None:
    """Entry point from the poll loop (comm_v2 path only — payload must carry
    `new_messages`, which the backend only sets for comm_v2 agents). nudge
    mode: short wake-up only, content via `mc inbox` (see
    deliver_messages_nudge). paste mode (default): persists new messages to
    the queue, then flushes only if the turn-gate is open.
    `dispatch_in_flight` — see msg_gate_open's docstring."""
    messages = payload.get("new_messages")
    if messages is None:
        return
    if MSG_DELIVERY_MODE == "nudge":
        deliver_messages_nudge(messages, dispatch_in_flight=dispatch_in_flight)
        return
    n = queue_new_messages(messages)
    if n:
        log.info("deliver_messages: %d message(s) queued", n)
    pending = msg_queue_files()
    if not pending:
        return
    if msg_gate_open(dispatch_in_flight=dispatch_in_flight):
        flush_msg_queue()
    else:
        log.info(
            "deliver_messages: Gate zu (Pane nicht ruhig / Dispatch in flight / Task aktiv) — "
            "%d Message(s) bleiben gequeued, kein Ack.", len(pending),
        )


# ── /clear-on-done (task finished without a follow-up) ──────────────────────────


def load_last_reset_task_id() -> Optional[str]:
    try:
        value = LAST_RESET_TASK_ID_FILE.read_text(encoding="utf-8").strip()
        return value or None
    except FileNotFoundError:
        return None
    except OSError as e:
        log.warning("load_last_reset_task_id: %s — treating as no prior reset", e)
        return None


def save_last_reset_task_id(task_id: str) -> None:
    try:
        LAST_RESET_TASK_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_RESET_TASK_ID_FILE.write_text(f"{task_id}\n", encoding="utf-8")
    except OSError as e:
        log.warning("save_last_reset_task_id: %s", e)


def maybe_reset_on_done(finished_task_id: Optional[str]) -> None:
    """BUGFIX: without this, a task finishing with no follow-up dispatch left
    the grok TUI's context growing forever — only a genuine task SWITCH
    (should_reset_session, in dispatch_task) ever fired /new. Fires /new once
    per finished task id (idempotency guard, memory + disk so a bridge restart
    can't cause a redundant re-reset for the same finished task). Harmless if
    dispatch_task's switch-reset ALSO fires later for the next task — resetting
    an already-fresh session is a no-op for the agent."""
    global _last_reset_task_id
    if not finished_task_id:
        return
    if _last_reset_task_id is None:
        _last_reset_task_id = load_last_reset_task_id()
    if _last_reset_task_id == finished_task_id:
        return
    if not is_session_running():
        return
    log.info(
        "dispatch_poll_loop: task %s finished with no follow-up — resetting grok TUI session",
        finished_task_id[:8],
    )
    reset_tui_session()
    _last_reset_task_id = finished_task_id
    save_last_reset_task_id(finished_task_id)


def load_last_task_id() -> Optional[str]:
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


def should_reset_session(new_task_id: str, last_task_id: Optional[str]) -> bool:
    """Reset ONLY on a genuine task switch (dispatch.py:8-18 semantics).

    - different task than the last dispatched one → True (fresh context)
    - same task (revision / request_changes / re-dispatch / bridge-restart
      redelivery) → False (the agent keeps its working context)
    - no known prior task → False (nothing to clear)
    """
    return bool(last_task_id) and new_task_id != last_task_id


def reset_tui_session() -> None:
    """Submit RESET_COMMAND (/new) into the grok TUI and wait until it is ready.

    The slash command goes in as LITERAL keys + raw CR (-H 0d) — NEVER through
    the bracketed-paste path: a TUI in paste-mode would render the command as
    text instead of executing it, and CR is the universal submit across the
    fleet's TUIs (poll.sh Bug 2026-05-15: `Enter` = LF is swallowed by raw-mode
    ptys). Afterwards poll for the prompt glyph so the subsequent task paste
    lands in the FRESH session, not mid-reset.
    """
    global _dispatch_in_flight
    log.info("task switch — resetting grok TUI session (%s)", RESET_COMMAND)
    _dispatch_in_flight = True
    try:
        _tmux(["send-keys", "-t", SESSION, "-l", RESET_COMMAND])
        time.sleep(0.4)
        _tmux(["send-keys", "-t", SESSION, "-H", "0d"])
        time.sleep(1.0)
        if not wait_for_agent_healthy(timeout=15):
            log.warning("reset_tui_session: no ready glyph after reset — pasting anyway (fail-open)")
    finally:
        _dispatch_in_flight = False


def deliver_task_context(task: dict) -> None:
    """Publish the task's MC context so the agent's own `mc` calls resolve it.

    Writes /tmp/mc-context.env (the `mc` CLI reads it first) AND sets the same
    3 keys in the tmux session env (belt-and-suspenders: new shells grok spawns
    for bash tools inherit them). Called BEFORE paste_and_submit so `mc ack` in
    the very first line of the agent's turn already has its context.
    """
    write_task_context_env(task)
    ctx = {
        "TASK_ID": str(task.get("id") or ""),
        "BOARD_ID": str(task.get("board_id") or ""),
        "X_DISPATCH_ATTEMPT_ID": str(task.get("dispatch_attempt_id") or ""),
    }
    for k, v in ctx.items():
        _tmux(["set-environment", "-t", SESSION, k, v])


# ── dispatch / comment prompts ──────────────────────────────────────────────────


def build_dispatch_prompt(task: dict) -> str:
    """Build the paste-ready dispatch text for the grok TUI.

    The AGENT owns its MC lifecycle (ADR-068): it acks, comments progress, and
    finishes/blocks the task itself via the copied `mc` CLI — the bridge does NOT
    close tasks. The task/board/attempt ids are surfaced in the header AND written
    to mc-context.env, so `mc` resolves them without grok threading env through
    subshells. `task.prompt` (built by the backend) already carries SOUL/TOOLS
    context; we render it verbatim and append the MC protocol footer.

    SECURITY: never materialize the literal MC_AGENT_TOKEN — only $MC_AGENT_TOKEN
    references (resolved from the tmux session env) are allowed.
    """
    task_id = str(task.get("id") or "")
    board_id = str(task.get("board_id") or "")
    attempt_id = str(task.get("dispatch_attempt_id") or "")
    title = str(task.get("title") or "")
    body = str(task.get("prompt") or task.get("description") or "")

    return (
        f"[MC DISPATCH] task_id={task_id} board_id={board_id} attempt_id={attempt_id}\n"
        f"Title: {title}\n"
        f"\n"
        f"{body}\n"
        f"\n"
        f"PROTOCOL (grok interactive worker via Mission Control):\n"
        f"- Your task context is in {MC_CONTEXT_ENV_PATH} — the `mc` CLI reads\n"
        f"  TASK_ID / BOARD_ID / X_DISPATCH_ATTEMPT_ID from it. Just call `mc`.\n"
        f"- ACK NOW, before you start: `mc ack {task_id}` (protects against the\n"
        f"  10-min ACK-timeout re-dispatch).\n"
        f"- Post progress as you work: `mc comment progress \"Update: ...\"`.\n"
        f"- Register every concrete artefact you produce: `mc deliverable <path-or-url>`.\n"
        f"- Do the work in the current directory (your task workspace).\n"
        f"- FINISH the task YOURSELF when done: `mc finish {task_id} --review`\n"
        f"  (or `mc blocked {task_id} ...` if you cannot proceed). The bridge does\n"
        f"  NOT close tasks — an unfinished task stays in_progress.\n"
    )


def build_comments_prompt(comments: list) -> str:
    """Build a paste-ready prompt for a batch of new_comments from /me/poll.

    Mirrors hermes-bridge._build_comments_prompt / poll.sh:deliver_comments —
    separates user vs system source, formats task header + content, ends with an
    action hint. Backend already filters the agent's own comments (author_type),
    so no client-side dedup. Returns "" when there is nothing to deliver.
    """
    user_c = [c for c in comments if c.get("source") == "user"]
    sys_c = [c for c in comments if c.get("source") == "system"]

    lines: list[str] = []
    if user_c:
        lines += [
            "[MC COMMENT] Neue User-Kommentare auf deinen aktiven Tasks",
            "",
            "Der Operator hat kommentiert. Lies, antworte im Task-Thread "
            "(`mc comment progress \"…\"`), arbeite am Task weiter.",
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
            "- subtask_completed: Subtask fertig. Deliverables prüfen, ggf. Parent auf review.",
            "- resolution: Agent hat Task abgeschlossen.",
            "- blocker: Task blockiert. Impact + Entscheidung prüfen.",
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
        "**Aktion:** Reagiere im Task-Thread per `mc comment progress \"Update: ...\"`. "
        "Abschluss wie immer per `mc finish <task> --review` / `mc blocked <task> ...`."
    )
    return "\n".join(lines)


def build_nudge_prompt(task_id: str, n: int) -> str:
    """Gentle no-progress reminder pasted into a silently stalled turn."""
    return (
        f"[MC WATCHDOG] Kein sichtbarer Fortschritt seit ~{int(NUDGE_IDLE_TIMEOUT)}s "
        f"auf Task {task_id} (Nudge {n}/{NUDGE_MAX}).\n"
        f"Falls du noch arbeitest, ignoriere das. Falls fertig: "
        f"`mc finish {task_id} --review`. Falls blockiert: "
        f"`mc blocked {task_id} --blocker-type technical_problem --question \"…\"`. "
        f"Die Bridge schliesst den Task nicht selbst."
    )


# ── dispatch driver (ensure session → context → paste → track) ──────────────────


def _mark_active(task: dict) -> None:
    global _active_task, _last_pane, _last_progress_ts, _nudges_sent
    with _state_lock:
        _active_task = task
        _last_pane = ""
        _last_progress_ts = time.monotonic()
        _nudges_sent = 0


def _clear_active() -> None:
    global _active_task
    with _state_lock:
        _active_task = None


def dispatch_task(task: dict, env: dict) -> bool:
    """Deliver one task to the grok TUI: ensure session → context → paste → track.

    Returns True on a paste, False if the session could not be started. The agent
    then drives its own lifecycle; this function never touches task status.
    """
    if not is_session_running():
        try:
            start_grok_session()
        except Exception as e:  # noqa: BLE001
            log.error("dispatch_task: session autostart failed: %s — skipping", e)
            return False
    # Fresh context per NEW task (dispatch.py:8-18); same-task re-dispatches
    # (revision / request_changes / restart redelivery) keep the context.
    global _dispatch_in_flight
    tid = str(task.get("id") or "")
    _dispatch_in_flight = True
    try:
        if should_reset_session(tid, load_last_task_id()):
            reset_tui_session()
        deliver_task_context(task)
        paste_and_submit(build_dispatch_prompt(task))
    finally:
        _dispatch_in_flight = False
    save_last_task_id(tid)
    _mark_active(task)
    log.info(
        "dispatched task %s (%s) into tmux session %s",
        str(task.get("id"))[:8], str(task.get("title") or "?")[:60], SESSION,
    )
    return True


def deliver_comments(comments: list) -> bool:
    """Paste a batch of new_comments into the running TUI. Returns True if sent."""
    if not comments or not is_session_running():
        return False
    prompt = build_comments_prompt(comments)
    if not prompt:
        return False
    paste_and_submit(prompt)
    log.info("delivered %d comment(s)/event(s) to tmux session %s", len(comments), SESSION)
    return True


# ── poll + watchdog + heartbeat loops ───────────────────────────────────────────


def dispatch_poll_loop() -> None:
    """Poll MC for the agent's active task; paste new ones into the grok TUI.

    Dedup via (task_id, attempt_id): /me/poll keeps returning state=new_task until
    the agent runs `mc ack`, so we only re-paste when the attempt id changes.
    Network/JSON errors are logged and swallowed — the loop never crashes the HTTP
    server. Endpoint GET /api/v1/agent/me/poll (a CLAIM endpoint). state=new_task →
    dispatch; idle/cancelled/stopped → clear dedup + active-task tracking.
    """
    global _last_dispatched_task_id, _last_dispatched_attempt_id
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
                # comm_v2: attach the acked_seq high-water marks so the backend's
                # two-stage cursor knows what was actually pasted (poll.sh
                # build_acked_seq_param). Empty when nothing acked yet — byte-
                # identical request for a non-comm_v2 agent (backend also just
                # ignores an unknown/empty param either way).
                enc = build_acked_seq_param()
                poll_url = f"{url}?acked_seq={enc}" if enc else url
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
                        finished_tid = _last_dispatched_task_id
                        if _last_dispatched_task_id is not None:
                            log.info("dispatch_poll_loop: agent %s, clearing dispatch cache", state)
                            _last_dispatched_task_id = None
                            _last_dispatched_attempt_id = None
                        _clear_active()
                        # BUGFIX /clear-on-done: a task finishing with no follow-up
                        # must not leave the TUI's context growing forever.
                        maybe_reset_on_done(finished_tid)

                # Also gates comm_v2 message delivery below: never let this same
                # tick's dispatch (which JUST drove the TUI) fall through to a
                # message flush — the module-global _dispatch_in_flight flag is
                # already back to False by the time we get here (minor review
                # fix, mirrors hermes-bridge's dispatch_happened_this_tick).
                dispatch_happened_this_tick = False
                if task and task.get("id"):
                    tid = str(task["id"])
                    aid = str(task.get("dispatch_attempt_id") or "")
                    # Re-paste only on a genuinely new task OR a fresh attempt id.
                    if tid != _last_dispatched_task_id or (
                        aid and aid != _last_dispatched_attempt_id
                    ):
                        if dispatch_task(task, env):
                            _last_dispatched_task_id = tid
                            _last_dispatched_attempt_id = aid
                            dispatch_happened_this_tick = True

                # Follow-up comments/events on active tasks → paste (any state).
                new_comments = (payload or {}).get("new_comments") or []
                if new_comments:
                    deliver_comments(new_comments)

                # comm_v2 Thread messages (any state) — only present when the
                # backend has agent.comm_v2=true; absent/None ⇒ no-op, so a
                # non-pilot agent's poll loop is byte-identical to before.
                if payload and "new_messages" in payload:
                    deliver_messages(payload, dispatch_in_flight=dispatch_happened_this_tick)
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    log.warning("dispatch_poll_loop: HTTP %s — %s", e.code, e.reason)
            except Exception as e:  # noqa: BLE001
                log.warning("dispatch_poll_loop: poll error: %s", type(e).__name__)
            time.sleep(DISPATCH_POLL_INTERVAL)
    except Exception as e:
        log.exception("[fatal] dispatch_poll_loop crashed: %s", e)
        raise


def should_nudge(*, active: bool, idle_seconds: float, idle_threshold: float,
                 nudges_sent: int, max_nudges: int) -> bool:
    """Pure decision: nudge only while a task is active, the pane has been idle
    past the threshold, and we have not exhausted the nudge budget."""
    return bool(active) and idle_seconds >= idle_threshold and nudges_sent < max_nudges


def _watchdog_tick(now: float, pane: str) -> Optional[str]:
    """Advance the no-progress watchdog one step against a captured pane.

    Returns a nudge prompt to paste (and increments the counter + resets the
    idle window), or None. Pane change = progress → reset the idle clock. Pure
    w.r.t. tmux: the caller supplies `now`/`pane` and pastes the return value, so
    this is unit-testable without a real TUI.
    """
    global _last_pane, _last_progress_ts, _nudges_sent
    with _state_lock:
        if _active_task is None:
            return None
        if pane != _last_pane:
            _last_pane = pane
            _last_progress_ts = now
            return None
        idle = now - _last_progress_ts
        if should_nudge(active=_active_task is not None, idle_seconds=idle,
                        idle_threshold=NUDGE_IDLE_TIMEOUT,
                        nudges_sent=_nudges_sent, max_nudges=NUDGE_MAX):
            _nudges_sent += 1
            n = _nudges_sent
            _last_progress_ts = now  # reset so we wait another full idle window
            task_id = str(_active_task.get("id") or "")
            return build_nudge_prompt(task_id, n)
        return None


def watchdog_loop() -> None:
    """Capture the pane on a timer; paste a no-progress nudge if a task stalls.

    The agent owns its terminal state — the watchdog never blocks/finishes a task,
    it only un-sticks a silently hung turn so nothing sits invisibly in_progress.
    """
    try:
        while True:
            time.sleep(WATCHDOG_INTERVAL)
            try:
                with _state_lock:
                    has_task = _active_task is not None
                if not has_task or not is_session_running():
                    continue
                nudge = _watchdog_tick(time.monotonic(), capture_pane())
                if nudge:
                    log.warning("watchdog: no progress — pasting nudge")
                    paste_and_submit(nudge)
            except Exception as e:  # noqa: BLE001 — a tick error must not kill the loop
                log.warning("watchdog_loop: tick error: %s", type(e).__name__)
    except Exception as e:
        log.exception("[fatal] watchdog_loop crashed: %s", e)
        raise


def heartbeat_loop() -> None:
    """Keep the grok agent's last_seen_at fresh so it stays on the Sessions page.

    Like hermes-bridge, POST an empty /agent/me/heartbeat every HEARTBEAT_INTERVAL
    WHILE the tmux session is running — the session is the liveness source, so a
    dead session correctly goes stale after 90s. /heartbeat only refreshes
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
                "session": SESSION,
                "tmux_running": is_session_running(),
                "agent_env_present": ENV_FILE.exists(),
            })
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/start":
            try:
                self._send_json(200, start_grok_session())
            except FileNotFoundError as e:
                self._send_json(412, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                self._send_json(500, {"error": f"start failed: {e}"})
            return
        if self.path == "/restart":
            # Kill + restart re-sources agent.env for the next turn (this IS the
            # reload for a session-less harness — ADR-066 §2).
            _tmux(["kill-session", "-t", SESSION])
            _clear_active()
            try:
                self._send_json(200, {"ok": True, "restart": start_grok_session()})
            except FileNotFoundError as e:
                self._send_json(412, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                self._send_json(500, {"error": f"restart failed: {e}"})
            return
        if self.path == "/stop":
            # Interrupt the current turn (Escape) — does NOT kill the session.
            _tmux(["send-keys", "-t", SESSION, "Escape"])
            self._send_json(200, {"ok": True, "stopped": "Escape sent"})
            return
        self._send_json(404, {"error": "not found"})

    def log_message(self, fmt, *args):  # noqa: A003
        log.info("%s - %s", self.address_string(), fmt % args)


def _handle_sigterm(signum, frame):  # noqa: ARG001
    log.info("[shutdown] received SIGTERM, exiting cleanly")
    sys.exit(0)


def main() -> None:
    try:
        signal.signal(signal.SIGTERM, _handle_sigterm)
        # Try start on bridge boot — non-fatal if env missing (provisioning may run later).
        try:
            start_grok_session()
        except FileNotFoundError as e:
            log.warning("grok session not started: %s", e)
        except Exception as e:  # noqa: BLE001
            log.warning("grok session start failed (will retry on dispatch): %s", e)
        threading.Thread(target=dispatch_poll_loop, name="grok-dispatcher", daemon=True).start()
        log.info("grok-dispatcher thread started (poll every %ss)", DISPATCH_POLL_INTERVAL)
        threading.Thread(target=watchdog_loop, name="grok-watchdog", daemon=True).start()
        log.info("grok-watchdog thread started (capture every %ss)", WATCHDOG_INTERVAL)
        threading.Thread(target=heartbeat_loop, name="grok-heartbeat", daemon=True).start()
        log.info("grok-heartbeat thread started (POST every %ss)", HEARTBEAT_INTERVAL)
        server = http.server.HTTPServer((HOST, PORT), Handler)
        log.info("grok-bridge listening on %s:%d (bin=%s, session=%s)", HOST, PORT, GROK_BIN, SESSION)
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
