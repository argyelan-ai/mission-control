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
    cmd_str = " ".join(shlex.quote(c) for c in _grok_launch_cmd())
    r = _tmux(
        ["new-session", "-d", "-s", SESSION, "-c", str(WORKSPACE), cmd_str], env=env,
    )
    if r.returncode != 0:
        raise RuntimeError(f"tmux new-session failed: {r.stderr.strip()}")
    _tmux(["set-option", "-t", SESSION, "mouse", "on"], env=env)
    ready = wait_for_agent_healthy()
    log.info("started grok TUI in tmux session %s (ready=%s)", SESSION, ready)
    return {"status": "started", "session": SESSION, "ready": ready}


def paste_and_submit(text: str) -> None:
    """Load `text` into the tmux paste-buffer, paste into the grok TUI, submit.

    Mirrors docker/shared/poll.sh:paste_and_submit exactly — a bracketed-paste-end
    marker (ESC [ 2 0 1 ~, hex `1b 5b 32 30 31 7e`) is sent BEFORE Enter because a
    TUI occasionally swallows the end marker, which would leave a subsequent bare
    Enter interpreted as a newline INSIDE the paste instead of a submit. Content
    goes through a file + load-buffer so arbitrary/multiline text is never
    misinterpreted as tmux key-names.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    buf = LOG_DIR / "dispatch.paste"
    buf.write_text(text, encoding="utf-8")
    _tmux(["load-buffer", str(buf)])
    _tmux(["paste-buffer", "-t", SESSION])
    time.sleep(0.3)
    _tmux(["send-keys", "-t", SESSION, "-H", "1b", "5b", "32", "30", "31", "7e"])
    time.sleep(0.2)
    _tmux(["send-keys", "-t", SESSION, "Enter"])


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
    deliver_task_context(task)
    paste_and_submit(build_dispatch_prompt(task))
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
                            _last_dispatched_attempt_id = None
                        _clear_active()

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

                # Follow-up comments/events on active tasks → paste (any state).
                new_comments = (payload or {}).get("new_comments") or []
                if new_comments:
                    deliver_comments(new_comments)
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
