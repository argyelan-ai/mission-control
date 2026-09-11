"""REX-Runde-2-Proben zu PR #511 (Branch feat/ack-recover-attempt-guard).

Runde 1 hat mit Unit-Stubs gearbeitet. Diese Proben fahren die VOLLE Kette:
ein echter `python3 -m mc_cli`-Prozess gegen einen HTTP-Server, der die
Guard-REIHENFOLGE des echten Backends nachbildet (agent_task_status.py:
run_control -> Attempt-Guard -> Ownership -> Status-Uebergang). Damit wird
nicht nur die Funktion geprueft, sondern Config.from_env() ->
/tmp/mc-context.env -> with_task_id -> gesetzter Header -> Serverantwort.

Warum die Reihenfolge zaehlt: _cmd_ack lehnt Fall (b) nicht selbst ab,
sondern laesst den PATCH mit dem eigenen Header rausgehen und uebersetzt
den Server-409. Antwortete der Server zuerst mit 400 "In Progress -> In
Progress", waere das in _cmd_ack der Idempotenz-ERFOLG — der Zombie-ACK
ginge durch. Serverseitiger Beleg dazu:
backend/tests/test_rex_probe_w5e_r2_backend.py.

Befund-Probe: test_probe_r2_coupling_hole_task_id_none — `with_task_id` auf
einer Config OHNE bekannten Kontext (task_id=None, aber gehaltener Header)
macht die ZIEL-Karte zur "eigenen" und kippt Fall (a) in Fall (b).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
CLI_DIR = os.path.join(HERE, "..")
sys.path.insert(0, CLI_DIR)

CTX_PATH = "/tmp/mc-context.env"
BOARD = "board-1"
TASK_A = "aaaaaaaa-1111-2222-3333-444444444444"
TASK_B = "bbbbbbbb-1111-2222-3333-444444444444"

STATE: dict[str, dict] = {}
ACTIVE: dict[str, str | None] = {"task_id": None}
LOG: list[tuple] = []


class _H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, payload):
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        attempt = self.headers.get("X-Dispatch-Attempt-Id")
        LOG.append(("GET", self.path, attempt, None))
        if self.path.endswith("/detail"):
            tid = self.path.split("/tasks/")[1].split("/")[0]
            t = STATE.get(tid)
            if not t:
                return self._json(404, {"detail": "Task not found"})
            return self._json(200, {"id": tid, "board_id": t["board_id"],
                                    "status": t["status"],
                                    "dispatch_attempt_id": t["dispatch_attempt_id"]})
        if self.path.endswith("/me/active-task-recovery"):
            tid = ACTIVE["task_id"]
            if not tid:
                return self._json(200, {"active": False})
            t = STATE[tid]
            return self._json(200, {"active": True, "task": {
                "id": tid, "board_id": t["board_id"], "title": t["title"],
                "status": t["status"], "dispatch_attempt_id": t["dispatch_attempt_id"],
                "prompt": t["prompt"]}})
        return self._json(404, {"detail": "no route"})

    def do_PATCH(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        recv = self.headers.get("X-Dispatch-Attempt-Id")
        LOG.append(("PATCH", self.path, recv, body))
        tid = self.path.rstrip("/").split("/tasks/")[1]
        t = STATE.get(tid)
        if not t:
            return self._json(404, {"detail": "Task not found"})
        # Attempt-Guard — VOR der Statuspruefung, wie im echten Backend
        if t["dispatch_attempt_id"] and recv != t["dispatch_attempt_id"]:
            if recv is None:
                return self._json(409, {"detail": "Fehlender X-Dispatch-Attempt-Id Header."})
            return self._json(409, {"detail":
                "Stale dispatch_attempt_id — dein Run ist veraltet. "
                f"Erwartet: {t['dispatch_attempt_id']}, gesendet: {recv}. "
                "Der Task wurde neu dispatcht — dein Update stammt von einem alten Run."})
        new = body.get("status")
        if new == t["status"] == "in_progress":
            return self._json(400, {"detail": "Ungueltiger Status-Uebergang: In Progress -> In Progress"})
        t["status"] = new or t["status"]
        return self._json(200, {"id": tid, "status": t["status"]})


@pytest.fixture(scope="module")
def url():
    srv = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(autouse=True)
def _clean():
    STATE.clear()
    LOG.clear()
    ACTIVE["task_id"] = None
    yield
    if os.path.exists(CTX_PATH):
        os.remove(CTX_PATH)


def _card(task_id, attempt, status="in_progress", title="Karte", prompt="TU DIES."):
    STATE[task_id] = dict(board_id=BOARD, dispatch_attempt_id=attempt,
                          status=status, title=title, prompt=prompt)


def _write_ctx(task_id, attempt):
    with open(CTX_PATH, "w", encoding="utf-8") as f:
        f.write(f"TASK_ID={task_id}\nBOARD_ID={BOARD}\nX_DISPATCH_ATTEMPT_ID={attempt}\n")


def _read_ctx():
    out = {}
    if os.path.exists(CTX_PATH):
        with open(CTX_PATH, encoding="utf-8") as f:
            for line in f:
                if "=" in line:
                    k, _, v = line.strip().partition("=")
                    out[k] = v
    return out


def _run_mc(url, args, extra_env=None):
    env = dict(os.environ)
    env["MC_API_URL"] = url
    env["MC_AGENT_TOKEN"] = "tok"
    for k in ("TASK_ID", "BOARD_ID", "X_DISPATCH_ATTEMPT_ID"):
        env.pop(k, None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run([sys.executable, "-m", "mc_cli"] + args, cwd=CLI_DIR,
                          env=env, capture_output=True, text=True, timeout=60)


def _patches():
    return [e for e in LOG if e[0] == "PATCH"]


# ── ack ───────────────────────────────────────────────────────────────────

def test_probe_r2_ack_case_a_heals_end_to_end(url):
    """Fall (a): Kontext zeigt auf A, `mc ack B` — der Live-Bug von W5-E.
    Muss glatt durchgehen, PATCH mit B's Attempt-ID, Kontext folgt auf B."""
    _card(TASK_A, "attempt-A")
    _card(TASK_B, "attempt-B", status="inbox")
    _write_ctx(TASK_A, "attempt-A")
    p = _run_mc(url, ["ack", TASK_B])
    assert p.returncode == 0, p.stdout + p.stderr
    assert _patches()[0][2] == "attempt-B"
    assert _patches()[0][1].endswith(TASK_B)
    assert _read_ctx() == {"TASK_ID": TASK_B, "BOARD_ID": BOARD,
                           "X_DISPATCH_ATTEMPT_ID": "attempt-B"}
    assert STATE[TASK_B]["status"] == "in_progress"


def test_probe_r2_ack_case_b_rejects_end_to_end(url):
    """Fall (b): eigene Karte wurde unter mir neu dispatcht (poll_orphan_run).
    Kein Adoptions-PATCH, Klartext mit beiden IDs, Kontext unangetastet."""
    _card(TASK_A, "attempt-NEW-RUN")
    _write_ctx(TASK_A, "attempt-OLD-RUN")
    p = _run_mc(url, ["ack"])
    out = p.stdout + p.stderr
    assert p.returncode != 0, out
    assert "neu dispatcht" in out and "attempt-OLD-RUN" in out and "attempt-NEW-RUN" in out
    assert _patches()[0][2] == "attempt-OLD-RUN"
    assert _read_ctx()["X_DISPATCH_ATTEMPT_ID"] == "attempt-OLD-RUN"


def test_probe_r2_ack_case_b_survives_explicit_task_id(url):
    """Fall (b) mit expliziter eigener ID (`mc ack A` aus Kontext A):
    with_task_id darf die Kopplung Header<->Karte nicht aufloesen."""
    _card(TASK_A, "attempt-NEW-RUN")
    _write_ctx(TASK_A, "attempt-OLD-RUN")
    p = _run_mc(url, ["ack", TASK_A])
    assert p.returncode != 0, p.stdout + p.stderr
    assert _patches()[0][2] == "attempt-OLD-RUN"
    assert _read_ctx()["X_DISPATCH_ATTEMPT_ID"] == "attempt-OLD-RUN"


def test_probe_r2_ack_double_ack_stays_idempotent(url):
    """Gleiche Karte, gleiche Attempt-ID: 400 In-Progress->In-Progress bleibt
    Erfolg — die Fallunterscheidung darf den Normalfall nicht anfassen."""
    _card(TASK_A, "attempt-A")
    _write_ctx(TASK_A, "attempt-A")
    p = _run_mc(url, ["ack"])
    assert p.returncode == 0, p.stdout + p.stderr
    assert _read_ctx()["X_DISPATCH_ATTEMPT_ID"] == "attempt-A"


def test_probe_r2_ack_target_never_dispatched(url):
    """Ziel ohne Attempt-ID: PATCH ohne Header, Kontext mit leerer ID."""
    _card(TASK_B, None, status="inbox")
    _write_ctx(TASK_A, "attempt-A")
    p = _run_mc(url, ["ack", TASK_B])
    assert p.returncode == 0, p.stdout + p.stderr
    assert _patches()[0][2] is None
    assert _read_ctx()["X_DISPATCH_ATTEMPT_ID"] == ""


# ── recover (Teil 2) ──────────────────────────────────────────────────────

def test_probe_r2_recover_case_a_heals_end_to_end(url):
    """Fall (a): aktiv ist B, Kontext zeigt auf A — Fortschreibung + Prompt."""
    _card(TASK_B, "attempt-B", title="Karte B")
    ACTIVE["task_id"] = TASK_B
    _write_ctx(TASK_A, "attempt-A")
    p = _run_mc(url, ["recover"])
    assert p.returncode == 0, p.stdout + p.stderr
    assert _read_ctx()["TASK_ID"] == TASK_B
    assert _read_ctx()["X_DISPATCH_ATTEMPT_ID"] == "attempt-B"
    assert "TU DIES." in p.stdout


def test_probe_r2_recover_case_b_zombie_cannot_rearm(url):
    """Fall (b): der Pfad, ueber den sich ein Zombie-Run heute OHNE jeden
    anderen Call neu scharf macht. Kein Kontext-Schreiben, kein Prompt."""
    _card(TASK_A, "attempt-NEW-RUN", title="Karte A")
    ACTIVE["task_id"] = TASK_A
    _write_ctx(TASK_A, "attempt-OLD-RUN")
    p = _run_mc(url, ["recover"])
    out = p.stdout + p.stderr
    assert p.returncode != 0, out
    assert "neu dispatcht" in out and "attempt-OLD-RUN" in out and "attempt-NEW-RUN" in out
    assert _read_ctx()["X_DISPATCH_ATTEMPT_ID"] == "attempt-OLD-RUN"
    assert "TU DIES." not in p.stdout


def test_probe_r2_recover_without_held_header_still_heals(url):
    """Gleiche Karte, aber kein gehaltener Header: kein Besitzanspruch,
    Adoption bleibt richtig — sonst waere der Restart-Fall erschlagen."""
    _card(TASK_A, "attempt-A", title="Karte A")
    ACTIVE["task_id"] = TASK_A
    _write_ctx(TASK_A, "")
    p = _run_mc(url, ["recover"])
    assert p.returncode == 0, p.stdout + p.stderr
    assert _read_ctx()["X_DISPATCH_ATTEMPT_ID"] == "attempt-A"


# ── Kopplung ──────────────────────────────────────────────────────────────

def test_probe_r2_with_task_id_keeps_context_task_id():
    """Die Kopplung, auf der alles steht: with_task_id verschiebt task_id,
    NICHT context_task_id. Bricht das, wird aus Fall (a) Fall (b)."""
    from mc_cli.config import Config
    c = Config(api_url="u", agent_token="t", task_id=TASK_A,
               board_id=BOARD, dispatch_attempt_id="attempt-A")
    assert c.context_task_id == TASK_A
    c2 = c.with_task_id(TASK_B)
    assert c2.task_id == TASK_B
    assert c2.context_task_id == TASK_A
    assert c2.with_task_id(TASK_A).context_task_id == TASK_A


@pytest.mark.xfail(reason="BEFUND R2: context_task_id=None wird von "
                          "with_task_id auf die ZIEL-Karte gesetzt — der "
                          "gehaltene Header bekommt eine Herkunft, die er "
                          "nie hatte, und Fall (a) kippt in Fall (b).",
                   strict=True)
def test_probe_r2_coupling_hole_task_id_none():
    """Config ohne bekannten Kontext (task_id=None), aber mit gehaltenem
    Header (z.B. X_DISPATCH_ATTEMPT_ID nur in der Prozess-Env). `with_task_id`
    laesst __post_init__ erneut ableiten -> context_task_id == Ziel."""
    from mc_cli.config import Config
    c = Config(api_url="u", agent_token="t", task_id=None,
               board_id=BOARD, dispatch_attempt_id="attempt-STALE")
    assert c.with_task_id(TASK_B).context_task_id != TASK_B


@pytest.mark.xfail(reason="BEFUND R2, Auswirkung: `mc ack B` wird mit "
                          "'dein Run ist veraltet' abgelehnt, obwohl der "
                          "Header nie zu B gehoerte (Runde 1 hat diesen "
                          "Fall geheilt).", strict=True)
def test_probe_r2_coupling_hole_breaks_case_a(url):
    """Kein Context-File, X_DISPATCH_ATTEMPT_ID + BOARD_ID nur in der Env."""
    _card(TASK_B, "attempt-B", status="inbox")
    if os.path.exists(CTX_PATH):
        os.remove(CTX_PATH)
    p = _run_mc(url, ["ack", TASK_B],
                extra_env={"BOARD_ID": BOARD,
                           "X_DISPATCH_ATTEMPT_ID": "attempt-STALE-FROM-ENV"})
    assert p.returncode == 0, p.stdout + p.stderr
