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
            env[k.strip()] = v.strip().strip("'\"")
    return env


def is_session_running() -> bool:
    r = _sp.run([TMUX_BIN, "has-session", "-t", SESSION], capture_output=True)
    return r.returncode == 0


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
        f"  Comment format: 3 lines exactly — Update / Evidence / Next.\n"
        f'  Comment via: mc_patch_task(task_id="{task_id}", board_id="{board_id}", comment="Update: ...\\nEvidence: ...\\nNext: ...")\n'
        f'  Hand off: mc_patch_task(task_id="{task_id}", board_id="{board_id}", status="review") when done.\n'
        f"  Skill: ~/.hermes/skills/mission-control/SKILL.md\n"
        f"  Workspace: cd ~/.mc/agents/hermes\n"
    )


def _send_to_tmux(prompt: str) -> None:
    """Paste prompt into the Hermes tmux session via two send-keys calls.

    Uses `-l` (literal) so prompt content is NOT interpreted as tmux key-names
    (e.g. embedded "Enter" text would otherwise submit prematurely). Errors
    are swallowed (check=False) — caller logs separately if needed.
    """
    _sp.run([TMUX_BIN, "send-keys", "-t", SESSION, "-l", prompt], check=False, env=os.environ)
    _sp.run([TMUX_BIN, "send-keys", "-t", SESSION, "Enter"], check=False, env=os.environ)


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
    """
    global _last_dispatched_task_id
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
                        # Agent has no active task — clear dedup cache so any
                        # re-opened or freshly assigned task can dispatch freely.
                        if _last_dispatched_task_id is not None:
                            log.info(
                                "dispatch_poll_loop: agent %s, clearing dispatch cache (was %s)",
                                state,
                                _last_dispatched_task_id[:8],
                            )
                            _last_dispatched_task_id = None
                if task and task.get("id") and task["id"] != _last_dispatched_task_id:
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
                    prompt = _build_dispatch_prompt(task)
                    _send_to_tmux(prompt)
                    _last_dispatched_task_id = task["id"]
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
