#!/usr/bin/env python3
"""
FreeCode Agent Bridge — startet free-code auf dem Mac via HTTP.
Läuft als Service ausserhalb Docker, empfängt Requests vom MC Backend.

POST   /start               Body: {"task_id": "...", "workspace": "...", "prompt": "..."}
POST   /input/{task_id}     Body: {"text": "..."}
GET    /health
GET    /sessions
GET    /output/{task_id}
DELETE /sessions/{task_id}
"""
import json
import os
import subprocess
import shutil
import subprocess as _sp
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import time
from typing import Optional
import uuid

PORT = 18792
HOME_DIR = Path(os.environ.get("HOME_HOST", str(Path.home())))
LOG_DIR = HOME_DIR / "Workspace/Sandboxes/free-code-local/logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "free-code-bridge.log"

FREE_CODE_BIN = str(HOME_DIR / ".local/bin/free-code")
FREE_CODE_ENV = str(HOME_DIR / "Workspace/Sandboxes/free-code-local/free-code.local.env")
MODEL_PROFILES = str(HOME_DIR / "Workspace/Sandboxes/free-code-local/model-profiles.json")
SETTINGS_FILE = str(HOME_DIR / "Workspace/Sandboxes/free-code-local/claude-config/settings.json")


TMUX_BIN = (
    shutil.which("tmux")
    or "/opt/homebrew/bin/tmux"   # Apple Silicon brew default
    or "/usr/local/bin/tmux"      # Intel brew default
)


def _check_tmux():
    if not os.path.isfile(TMUX_BIN):
        raise RuntimeError("tmux nicht gefunden — bitte installieren: brew install tmux")
    result = _sp.run([TMUX_BIN, "-V"], capture_output=True, text=True)
    log(f"tmux version: {result.stdout.strip()}")


active_procs: dict[str, subprocess.Popen] = {}
active_sessions: dict[str, str] = {}      # task_id -> tmux session name


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def load_env():
    env = os.environ.copy()
    env["FREE_CODE_MODEL_PROFILES_FILE"] = MODEL_PROFILES
    env["HOME"] = str(HOME_DIR)
    if Path(FREE_CODE_ENV).exists():
        with open(FREE_CODE_ENV) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip("'\"")
    return env


def _session_name(task_id: str) -> str:
    return f"fc-{task_id[:8]}"


def start_free_code(task_id: str, workspace: str, prompt: str) -> str:
    session = _session_name(task_id)

    # Evtl. alte Session mit gleichem Namen bereinigen
    _sp.run([TMUX_BIN, "kill-session", "-t", session], capture_output=True)

    env = load_env()

    # Prompt in temporäre Datei schreiben (vermeidet Shell-Escaping-Probleme)
    prompt_file = str(LOG_DIR / f"prompt-{task_id[:8]}.txt")
    with open(prompt_file, "w") as f:
        f.write(prompt)

    # Kommando das in tmux gestartet wird
    cmd = (
        f"cd {shutil.quote(workspace)} && "
        f"FREE_CODE_MODEL_PROFILES_FILE={shutil.quote(env.get('FREE_CODE_MODEL_PROFILES_FILE', MODEL_PROFILES))} "
        f"{shutil.quote(FREE_CODE_BIN)} "
        f"--settings {shutil.quote(SETTINGS_FILE)} "
        f"-p $(cat {shutil.quote(prompt_file)})"
    )

    try:
        # Session erstellen (220 cols × 50 rows, detached)
        _sp.run(
            [TMUX_BIN, "new-session", "-d", "-s", session, "-x", "220", "-y", "50"],
            check=True, capture_output=True,
        )
        # Befehl in Session senden
        _sp.run(
            [TMUX_BIN, "send-keys", "-t", session, cmd, "Enter"],
            check=True, capture_output=True,
        )
        active_sessions[task_id] = session
        log(f"free-code tmux session started: task_id={task_id} session={session} workspace={workspace}")
        return f"started: session={session}"
    except _sp.CalledProcessError as e:
        log(f"ERROR starting tmux session: {e.stderr}")
        return f"error: {e}"


def get_output(task_id: str) -> Optional[str]:
    """Aktuellen Terminal-Inhalt via tmux capture-pane zurückgeben (mit ANSI)."""
    session = active_sessions.get(task_id) or _session_name(task_id)
    result = _sp.run(
        [TMUX_BIN, "capture-pane", "-t", session, "-p", "-e", "-S", "-"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def send_input(task_id: str, text: str) -> bool:
    """Text in tmux-Session schicken."""
    session = active_sessions.get(task_id) or _session_name(task_id)
    result = _sp.run(
        [TMUX_BIN, "send-keys", "-t", session, text, "Enter"],
        capture_output=True,
    )
    return result.returncode == 0


def kill_session(task_id: str) -> bool:
    """tmux-Session beenden."""
    session = active_sessions.get(task_id) or _session_name(task_id)
    result = _sp.run(
        [TMUX_BIN, "kill-session", "-t", session],
        capture_output=True,
    )
    active_sessions.pop(task_id, None)
    return result.returncode == 0


def list_sessions() -> list[dict]:
    """Alle fc-* tmux-Sessions auflisten."""
    result = _sp.run(
        [TMUX_BIN, "list-sessions", "-F",
         "#{session_name}:#{session_created}:#{session_windows}"],
        capture_output=True, text=True,
    )
    sessions = []
    for line in result.stdout.strip().splitlines():
        if not line.startswith("fc-"):
            continue
        parts = line.split(":")
        if len(parts) < 2:
            continue
        name = parts[0]
        task_prefix = name[3:]  # alles nach "fc-"
        task_id = next(
            (tid for tid, sname in active_sessions.items() if sname == name),
            task_prefix,  # fallback: prefix als task_id
        )
        created_ts = int(parts[1]) if parts[1].isdigit() else 0
        elapsed = int(time.time()) - created_ts
        sessions.append({
            "task_id": task_id,
            "session": name,
            "elapsed_seconds": elapsed,
        })
    return sessions


def get_status(task_id: str) -> dict:
    if task_id not in active_procs:
        return {"task_id": task_id, "status": "unknown"}
    proc = active_procs[task_id]
    if proc.poll() is None:
        return {"task_id": task_id, "status": "running", "pid": proc.pid}
    rc = proc.wait()
    del active_procs[task_id]
    return {"task_id": task_id, "status": "done", "returncode": rc}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log(f"{self.address_string()} {fmt % args}")

    def _json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._json({"status": "ok", "bridge": "free-code-agent"})

        elif self.path.startswith("/status/"):
            task_id = self.path.split("/status/")[1]
            self._json(get_status(task_id))

        elif self.path == "/sessions":
            self._json(list_sessions())

        elif self.path.startswith("/output/"):
            task_id = self.path.split("/output/")[1]
            output = get_output(task_id)
            if output is None:
                self.send_response(404)
                self.end_headers()
            else:
                self._json({"task_id": task_id, "output": output})

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/start":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                data = json.loads(body)
            except Exception as e:
                self.send_error(400, str(e))
                return

            task_id = data.get("task_id", str(uuid.uuid4())[:8])
            workspace = data.get("workspace", str(HOME_DIR / "Workspace"))
            prompt = data.get("prompt", "")

            result = start_free_code(task_id, workspace, prompt)
            self._json({"task_id": task_id, "result": result})

        elif self.path.startswith("/input/"):
            task_id = self.path.split("/input/")[1]
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                data = json.loads(body)
            except Exception as e:
                self.send_error(400, str(e))
                return
            text = data.get("text", "")
            ok = send_input(task_id, text)
            self._json({"ok": ok})

        else:
            self.send_response(404)
            self.end_headers()

    def do_DELETE(self):
        if self.path.startswith("/sessions/"):
            task_id = self.path.split("/sessions/")[1]
            ok = kill_session(task_id)
            self._json({"ok": ok})
        else:
            self.send_response(404)
            self.end_headers()


if __name__ == "__main__":
    log(f"FreeCode Agent Bridge starting on port {PORT}")
    _check_tmux()
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    log(f"Listening on http://0.0.0.0:{PORT}")
    server.serve_forever()
