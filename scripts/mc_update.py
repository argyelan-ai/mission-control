#!/usr/bin/env python3
"""mc_update.py — the one update engine for a Mission Control install (ADR-094).

Terminal (``install.sh --update``) and the Settings button (through the
cli-bridge) both start exactly this program. It updates the MC stack of the
install it runs in to a newer *stable release* and either proves the result or
goes back. Python standard library only: the bridge already needs python3, so
the engine adds no dependency where the button exists.

Phases, in this order (ADR-094 decision 3)::

    preflight → drain → fetch → verify_release → pull → backup →
    switch → up → verify → done

    failure before switch       → aborted          (nothing changed)
    failure from switch on      → rolling_back → rolled_back | rollback_failed

The journal (``<state>/journal/<run-id>.json``) is the truth about a run. Every
phase writes its *intent* before it changes anything and its *result* after;
each write is atomic (temp file + fsync + rename + fsync of the folder). A
crash at any point leaves either the previous or the next journal, never a
torn one, and ``resume`` decides from it what to do:

* stopped before ``switch`` → abort (``aborted``, drain lifted) — never
  "carry on blindly" after a crash;
* stopped in ``switch``/``up``/``verify`` → finish the switch (idempotent),
  always put the fleet file back from the run's copy, ``up``, the four checks
  with a fresh limit → ``done`` or the way back;
* stopped in ``rolling_back`` → the way back again from the start.

One run at a time: ``<state>/lock`` is held with ``flock`` for the whole run
(the kernel drops it when the process dies, so a crashed run never blocks the
next one) and records PID + process start time, so ``status`` and the bridge
can tell a live run from a dead one.

From ``switch`` on the engine runs the copy of itself taken from the *target*
tag (``git show <tag>:scripts/mc_update.py``), so the checkout never replaces
the running script under itself, and the code that finishes the update is the
code of the version being installed.

This file is PR 2a of the update plan: the state machine, journal, lock,
resume rule, rollback R1 (code + ``.env`` + fleet file back, database
untouched), the four success checks and the drain interface. Not here yet,
and refused honestly instead of guessed at:

* restoring the database when the schema already changed (R2, PR 2b) — the
  run ends ``rollback_failed`` and names the backup and the command;
* release signature / published-release check (PR 6) — ``verify_release``
  checks format, direction, schema compatibility and the target commit;
* the bridge door, detaching and boot resume (PR 3) — ``start`` runs in the
  foreground here.

The button that would start this stays behind a feature switch, off, until
the release gate (ADR-094 decision 13).

Usage::

    mc_update.py plan      [--version vX.Y.Z] [--expect vX.Y.Z] [--json]
    mc_update.py preflight [--version vX.Y.Z] [--expect vX.Y.Z] [--json]
    mc_update.py start     [--version vX.Y.Z] [--expect vX.Y.Z] [--source terminal|bridge] [--json]
    mc_update.py status    [--run ID] [--json]
    mc_update.py resume    [--run ID] [--json]
    mc_update.py rollback  [--run ID] [--json]

Settings (environment; ``MC_*`` keys are also read from the install's .env):

    MC_REPO_PATH               the install (default: working directory)
    MC_UPDATE_HOME             engine state (default: ~/.mc/update)
    MC_UPDATE_MANAGED=deploy   this install is updated by a deploy procedure → refused (exit 9)
    MC_REPO_URL / MC_RELEASE_API   where releases come from
    MC_IMAGE_REGISTRY          registry of the core images
    MC_API_URL                 backend base URL for /health (default http://localhost:8000)
    MC_UPDATE_VERIFY_TIMEOUT   seconds for the four checks (default 720, 1500 on Linux arm)
    MC_UPDATE_START_PERIOD     backend start_period in seconds (default 300)
    MC_UPDATE_DRAIN_TIMEOUT    seconds to wait for running work to finish (default 1800)
    MC_UPDATE_DRAIN_TTL        expiry of the drain flag, refreshed per phase (default 3600)
    MC_UPDATE_POLL_INTERVAL    seconds between polls (default 5)
    MC_UPDATE_MIN_FREE_MB      free disk space the preflight wants (default 3072)

Exit codes: 0 done / nothing to do / cancel accepted · 1 failed, rolled back ·
2 usage · 3 aborted (nothing changed) · 4 rollback failed · 5 rolled back on
request (``rollback`` of a finished run) · 9 refused (managed install, target
mismatch, way back not possible yet — the bridge answers 409) · 23 another run
holds the lock, or a cancel came too late (423).

Cancel: ``rollback`` on a live run before the switch asks it to stop. The run
reads that at every phase boundary and while it waits for work or a download;
a running backup is finished first. The last point before the switch is one
atomic decision (``run-<id>/gate``, created with ``link``): either the cancel
or the switch claims it, never both — a cancel that loses is answered "too
late" (exit 23), not "ok".
"""

from __future__ import annotations

import argparse
import datetime as _dt
import errno
import fcntl
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── constants ───────────────────────────────────────────────────────────────

JOURNAL_SCHEMA = 1
PHASES = ("preflight", "drain", "fetch", "verify_release", "pull", "backup",
          "switch", "up", "verify")
PRE_SWITCH = frozenset(PHASES[:PHASES.index("switch")])
POST_SWITCH = frozenset(("switch", "up", "verify"))
TERMINAL = frozenset(("done", "aborted", "rolled_back", "rollback_failed"))

# Only MC's own images are pulled. mc-worker runs the backend image, so the
# two services below cover all three containers. Third-party images (db,
# redis, qdrant, livekit, caddy, …) keep whatever version the install has —
# the way back could not turn them back (ADR-094 decision 8).
PULL_SERVICES = ("backend", "frontend")
# The containers the update replaces and the four checks look at.
MC_SERVICES = ("backend", "mc-worker", "frontend")
SERVICE_IMAGE = {"backend": "mc-backend", "mc-worker": "mc-backend", "frontend": "mc-frontend"}

FLEET_FILE = "docker/docker-compose.agents.yml"
ENGINE_PATH_IN_REPO = "scripts/mc_update.py"
ALEMBIC_DIR = "backend/alembic/versions"
DB_USER = "mc"
DB_NAME = "mission_control"

RELEASE_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")

# Running work, counted before the switch (ADR-094 decision 7). PR 4c adds
# group-chat rounds, loops and due schedules next to the tasks.
RUNNING_WORK_SQL = "select count(*) from tasks where status = 'in_progress'"

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_ABORTED, EXIT_ROLLBACK_FAILED = 0, 1, 2, 3, 4
EXIT_ROLLED_BACK = 5
EXIT_REFUSED, EXIT_LOCKED = 9, 23

# Crash points for the crash matrix (PR 2c) and the tests: with
# MC_UPDATE_TEST_FAULT=<point> the engine dies right there with exit 137, the
# way a power cut would stop it. "<phase>.intent" fires after the intent was
# journaled, "<phase>.done" after the result was.
INNER_FAULT_POINTS = ("switch.worker_stopped", "switch.parked", "switch.checked_out",
                      "switch.fleet_back", "switch.pins", "rolling_back.checked_out")
FAULT_POINTS = tuple(f"{p}.{s}" for p in PHASES for s in ("intent", "done")) + INNER_FAULT_POINTS


def _fault(point: str) -> None:
    if os.environ.get("MC_UPDATE_TEST_FAULT") == point:
        sys.stdout.flush()
        os._exit(137)


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(t: Optional[_dt.datetime] = None) -> str:
    return (t or _now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> _dt.datetime:
    return _dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)


def _int_setting(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    if raw == "":
        return default
    if not re.fullmatch(r"\d+", raw):
        raise UsageError(f"{name} must be a whole number (got {raw!r}).")
    return int(raw)


def _float_setting(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    if raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise UsageError(f"{name} must be a number of seconds (got {raw!r}).") from None
    if value < 0:
        raise UsageError(f"{name} must not be negative (got {raw!r}).")
    return value


def is_release_tag(tag: Optional[str]) -> bool:
    return bool(tag) and bool(RELEASE_TAG_RE.match(tag or ""))


def version_tuple(tag: str) -> Tuple[int, int, int]:
    m = RELEASE_TAG_RE.match(tag)
    if not m:
        raise ValueError(f"not a release tag: {tag}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def image_tag(tag: str) -> str:
    """release.yml publishes semver without the "v" (0.9.3)."""
    return tag[1:] if tag.startswith("v") else tag


def normalize_version(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    v = v.strip()
    return v[1:] if v.startswith("v") else v


# ── errors ──────────────────────────────────────────────────────────────────

class UpdateError(Exception):
    """A step failed. ``kind`` tells the bridge whether it counts against the
    daily start limit: "network" failures before the switch do not."""

    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind


class UsageError(Exception):
    pass


class Refused(Exception):
    """Refused on purpose (exit 9 / HTTP 409)."""


class Locked(Exception):
    """Another run holds the lock (exit 23 / HTTP 423)."""


class CancelRequested(Exception):
    pass


class RollbackUnavailable(Exception):
    pass


# ── small file helpers ──────────────────────────────────────────────────────

def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    """Temp file in the same folder + fsync + rename + fsync of the folder.

    The journal is no bind-mounted file, so ``rename`` is right here (unlike
    .env, see ``write_in_place``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    payload = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))
    _fsync_dir(path.parent)


def write_in_place(path: Path, data: bytes) -> None:
    """Overwrite a file's content keeping its inode.

    .env is bind-mounted into backend and mc-worker as a single file; a
    rename would leave the containers on the old inode (ADR-094 decision 4)."""
    if not path.exists():
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)
    with open(path, "r+b") as f:
        f.seek(0)
        f.write(data)
        f.truncate()
        f.flush()
        os.fsync(f.fileno())


def copy_private(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    data = src.read_bytes()
    fd = os.open(str(dst), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def env_file_get(path: Path, key: str) -> str:
    if not path.is_file():
        return ""
    value = ""
    for line in path.read_text(errors="replace").splitlines():
        if line.startswith(key + "="):
            value = line[len(key) + 1:]
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def env_file_set(path: Path, updates: Dict[str, str]) -> None:
    """Replace-or-append KEY=VALUE lines, in place (same as mc_env_set)."""
    lines = path.read_text().splitlines() if path.exists() else []
    seen = set()
    out = []
    for line in lines:
        key = line.split("=", 1)[0] if "=" in line else None
        if key in updates:
            if key in seen:
                continue
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
    write_in_place(path, ("\n".join(out) + "\n").encode())


# ── process start time (dead-PID detection) ─────────────────────────────────

def process_start(pid: int) -> Optional[str]:
    """A stable fingerprint of when ``pid`` started, or None when it is gone.

    Linux: field 22 of /proc/<pid>/stat; elsewhere ``ps -o lstart=``. A PID
    the kernel has handed to another process has another start time."""
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        try:
            raw = stat.read_text()
            return "proc:" + raw.rsplit(")", 1)[1].split()[19]
        except (OSError, IndexError):
            return None
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    s = out.stdout.strip()
    return ("ps:" + s) if out.returncode == 0 and s else None


def pid_alive(pid: int, start: Optional[str] = None) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    if start:
        now = process_start(pid)
        if now is not None and now != start:
            return False   # the PID was reused by another process
    return True


# ── lock ────────────────────────────────────────────────────────────────────

class RunLock:
    """``<state>/lock``: flock for exclusion, PID + start time for humans.

    The flock is the real guard — the kernel releases it when the holder dies,
    so a crashed run (power cut, kill -9) never needs cleaning up. The record
    inside says who holds it; ``holder()`` tells a live holder from a dead one
    for ``status`` and the bridge's boot routine. The descriptor survives the
    exec into the target engine (MC_UPDATE_LOCK_FD), so the lock is never
    dropped between the two engines."""

    def __init__(self, path: Path):
        self.path = path
        self.fd: Optional[int] = None

    def holder(self) -> Optional[Dict[str, Any]]:
        try:
            rec = json.loads(self.path.read_text() or "{}")
        except (OSError, ValueError):
            return None
        if not isinstance(rec, dict) or not rec.get("pid"):
            return None
        rec["alive"] = pid_alive(int(rec["pid"]), rec.get("start"))
        return rec

    def live_holder(self) -> Optional[Dict[str, Any]]:
        """The record of a live holder, or None.

        Read-only on purpose: status and preflight are polled by the UI, and a
        probe that took the flock for a moment could make a start that
        happens at that instant fail with "locked"."""
        if self.fd is not None:
            return {"pid": os.getpid(), "alive": True}
        h = self.holder()
        return h if h and h.get("alive") else None

    def acquire(self, run_id: Optional[str]) -> None:
        inherited = os.environ.get("MC_UPDATE_LOCK_FD")
        if inherited and self._adopt(int(inherited)):
            self._record(run_id)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(fd)
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                h = self.holder() or {}
                raise Locked(f"another update run holds the lock (pid {h.get('pid', '?')}, run {h.get('run_id', '?')})") from None
            raise
        self.fd = fd
        self._record(run_id)

    def _adopt(self, fd: int) -> bool:
        try:
            if os.fstat(fd).st_ino != os.stat(str(self.path)).st_ino:
                return False
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        self.fd = fd
        return True

    def _record(self, run_id: Optional[str]) -> None:
        assert self.fd is not None
        rec = {"pid": os.getpid(), "start": process_start(os.getpid()), "run_id": run_id,
               "acquired_at": _iso()}
        data = (json.dumps(rec) + "\n").encode()
        os.ftruncate(self.fd, 0)
        os.pwrite(self.fd, data, 0)
        os.fsync(self.fd)

    def set_run(self, run_id: str) -> None:
        self._record(run_id)

    def keep_across_exec(self) -> None:
        if self.fd is not None:
            os.set_inheritable(self.fd, True)
            os.environ["MC_UPDATE_LOCK_FD"] = str(self.fd)

    def release(self) -> None:
        if self.fd is None:
            return
        try:
            os.ftruncate(self.fd, 0)
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.fd = None
            os.environ.pop("MC_UPDATE_LOCK_FD", None)


# ── cancel-or-switch gate ───────────────────────────────────────────────────

GATE_FILE = "gate"


def claim_gate(run_dir: Path, decision: str) -> str:
    """Claim the run's last decision before the switch; return the winner.

    ``os.link`` of a complete temp file is atomic and fails when the gate
    exists, so exactly one of "cancel" and "switch" wins, and a reader never
    sees a half-written gate."""
    run_dir.mkdir(parents=True, exist_ok=True)
    gate = run_dir / GATE_FILE
    tmp = run_dir / f".gate.{os.getpid()}.{uuid.uuid4().hex[:8]}"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, (decision + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.link(str(tmp), str(gate))
        won = True
    except FileExistsError:
        won = False
    finally:
        tmp.unlink()
    if won:
        _fsync_dir(run_dir)
        return decision
    return gate_holder(run_dir) or "unknown"


def gate_holder(run_dir: Path) -> Optional[str]:
    try:
        return (run_dir / GATE_FILE).read_text().strip() or None
    except OSError:
        return None


# ── journal ─────────────────────────────────────────────────────────────────

class Journal:
    def __init__(self, path: Path, data: Dict[str, Any]):
        self.path = path
        self.data = data

    @property
    def run_id(self) -> str:
        return self.data["run_id"]

    @property
    def phase(self) -> str:
        return self.data["phase"]

    @classmethod
    def create(cls, state: Path, source: str) -> "Journal":
        run_id = _now().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
        data = {
            "schema": JOURNAL_SCHEMA, "run_id": run_id, "source": source,
            "phase": "preflight", "phase_state": "intent", "status": "running",
            "created_at": _iso(), "updated_at": _iso(),
            "from": {}, "target": {}, "images": {}, "rollback": {}, "drain": {},
            "backup": {}, "engine": {}, "error": None, "failure_kind": None,
            "events": [],
        }
        j = cls(state / "journal" / f"{run_id}.json", data)
        j.save()
        return j

    @classmethod
    def load(cls, path: Path) -> "Journal":
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as e:
            raise UpdateError(f"journal {path.name} cannot be read ({type(e).__name__})") from None
        if not isinstance(data, dict) or data.get("schema") != JOURNAL_SCHEMA:
            data = data if isinstance(data, dict) else {}
            raise UpdateError(f"journal {path.name} has schema {data.get('schema')}, this engine reads {JOURNAL_SCHEMA}")
        return cls(path, data)

    def save(self) -> None:
        self.data["updated_at"] = _iso()
        atomic_write_json(self.path, self.data)

    def event(self, phase: str, event: str, detail: str = "") -> None:
        self.data["events"].append({"at": _iso(), "phase": phase, "event": event,
                                    "engine": os.path.realpath(__file__), "detail": detail})

    def begin(self, phase: str, detail: str = "") -> None:
        self.data["phase"] = phase
        self.data["phase_state"] = "intent"
        self.data["status"] = "running" if phase not in TERMINAL else phase
        self.event(phase, "intent", detail)
        self.save()

    def finish(self, phase: str, detail: str = "") -> None:
        self.data["phase"] = phase
        self.data["phase_state"] = "done"
        self.data["status"] = phase if phase in TERMINAL else "running"
        self.event(phase, "done", detail)
        self.save()

    def end(self, terminal: str, detail: str = "", error: Optional[str] = None,
            kind: Optional[str] = None) -> None:
        assert terminal in TERMINAL
        if error is not None:
            self.data["error"] = error
        if kind is not None:
            self.data["failure_kind"] = kind
        self.data["finished_at"] = _iso()
        self.finish(terminal, detail)


def journal_dir(state: Path) -> Path:
    return state / "journal"


def list_runs(state: Path) -> List[Path]:
    d = journal_dir(state)
    if not d.is_dir():
        return []
    return sorted((p for p in d.glob("*.json") if RUN_ID_RE.match(p.stem)), key=lambda p: p.stem)


def find_run(state: Path, run_id: Optional[str]) -> Optional[Journal]:
    if run_id:
        if not RUN_ID_RE.match(run_id):
            raise UsageError(f"not a run id: {run_id!r}")
        p = journal_dir(state) / f"{run_id}.json"
        if not p.exists():
            raise UsageError(f"no run {run_id}")
        return Journal.load(p)
    runs = list_runs(state)
    return Journal.load(runs[-1]) if runs else None


def find_open_run(state: Path) -> Optional[Journal]:
    for p in reversed(list_runs(state)):
        j = Journal.load(p)
        if j.phase not in TERMINAL:
            return j
    return None


# ── the engine ──────────────────────────────────────────────────────────────

class Engine:
    def __init__(self, repo: Path, state: Path, out_json: bool = False):
        self.repo = repo
        self.state = state
        self.out_json = out_json
        self.env_path = repo / ".env"
        self.fleet_path = repo / FLEET_FILE
        self.lock = RunLock(state / "lock")
        self.journal: Optional[Journal] = None
        self.emitted = False
        self._project: Optional[str] = None
        self.poll = _float_setting("MC_UPDATE_POLL_INTERVAL", 5.0)
        self.start_period = _float_setting("MC_UPDATE_START_PERIOD", 300.0)
        # ADR-094 decision 4: 25 min on arm64 (Pi, Spark, Apple silicon), else 12.
        arm = platform.machine().lower() in ("aarch64", "arm64", "armv7l")
        self.verify_timeout = _float_setting("MC_UPDATE_VERIFY_TIMEOUT", 1500.0 if arm else 720.0)
        self.drain_timeout = _float_setting("MC_UPDATE_DRAIN_TIMEOUT", 1800.0)
        self.drain_ttl = _int_setting("MC_UPDATE_DRAIN_TTL", 3600)
        self.min_free_mb = _int_setting("MC_UPDATE_MIN_FREE_MB", 3072)
        # Start of the current `up` → the four checks share one limit with it.
        self._clock: Optional[float] = None

    # ── settings ────────────────────────────────────────────────────────────

    def setting(self, name: str, default: str = "") -> str:
        v = os.environ.get(name, "")
        if v:
            return v
        return env_file_get(self.env_path, name) or default

    @property
    def repo_url(self) -> str:
        return self.setting("MC_REPO_URL", "https://github.com/argyelan-ai/mission-control.git")

    @property
    def release_api(self) -> str:
        return self.setting("MC_RELEASE_API",
                            "https://api.github.com/repos/argyelan-ai/mission-control/releases/latest")

    @property
    def registry(self) -> str:
        return self.setting("MC_IMAGE_REGISTRY", "ghcr.io/argyelan-ai")

    @property
    def api_url(self) -> str:
        return os.environ.get("MC_API_URL", "http://localhost:8000").rstrip("/")

    def image_ref(self, service: str, tag: str) -> str:
        return f"{self.registry}/{SERVICE_IMAGE[service]}:{image_tag(tag)}"

    # ── output ──────────────────────────────────────────────────────────────

    def say(self, msg: str) -> None:
        if not self.out_json:
            print(msg, flush=True)
        if self.journal is not None:
            log = self.run_dir() / "engine.log"
            try:
                log.parent.mkdir(parents=True, exist_ok=True)
                with open(log, "a") as f:
                    f.write(f"{_iso()} {msg}\n")
            except OSError:
                pass

    def run_dir(self) -> Path:
        assert self.journal is not None
        return self.state / f"run-{self.journal.run_id}"

    # ── subprocess helpers ─────────────────────────────────────────────────

    def sh(self, args: List[str], check: bool = True, env: Optional[Dict[str, str]] = None,
           timeout: Optional[float] = None, cancellable: bool = False) -> subprocess.CompletedProcess:
        full_env = dict(os.environ)
        full_env.pop("MC_UPDATE_LOCK_FD", None)
        if env:
            full_env.update(env)
        try:
            if cancellable:
                cp = self._run_cancellable(args, full_env, timeout)
            else:
                cp = subprocess.run(args, cwd=str(self.repo), env=full_env, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            raise UpdateError(f"{args[0]} is not installed or not in PATH") from None
        except subprocess.TimeoutExpired:
            raise UpdateError(f"{' '.join(args[:3])} … did not finish within {timeout:.0f}s") from None
        if check and cp.returncode != 0:
            tail = (cp.stderr or cp.stdout or "").strip().splitlines()[-3:]
            raise UpdateError(f"`{' '.join(args)}` failed (exit {cp.returncode}): {' | '.join(tail)}")
        return cp

    def _run_cancellable(self, args: List[str], env: Dict[str, str],
                         timeout: Optional[float]) -> subprocess.CompletedProcess:
        """subprocess.run that also stops when the run is cancelled (a
        download can take an hour; the operator should not wait it out)."""
        proc = subprocess.Popen(args, cwd=str(self.repo), env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        end = None if timeout is None else time.monotonic() + timeout
        step = min(max(self.poll, 0.05), 1.0)
        while True:
            try:
                out, err = proc.communicate(timeout=step)
                return subprocess.CompletedProcess(args, proc.returncode, out, err)
            except subprocess.TimeoutExpired:
                pass
            cancelled = self.cancel_requested()
            if cancelled or (end is not None and time.monotonic() >= end):
                proc.terminate()
                try:
                    proc.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
                if cancelled:
                    raise CancelRequested("cancelled during the download — nothing was changed")
                raise subprocess.TimeoutExpired(args, timeout or 0)

    def git(self, *args: str, check: bool = True) -> str:
        return self.sh(["git", *args], check=check).stdout.strip()

    def compose(self, *args: str, check: bool = True, env: Optional[Dict[str, str]] = None,
                timeout: Optional[float] = None, cancellable: bool = False) -> subprocess.CompletedProcess:
        return self.sh(["docker", "compose", "-p", self.project(), *args], check=check, env=env,
                       timeout=timeout, cancellable=cancellable)

    def psql(self, sql: str, check: bool = True) -> str:
        cp = self.compose("exec", "-T", "db", "psql", "-U", DB_USER, "-d", DB_NAME,
                          "-v", "ON_ERROR_STOP=1", "-tAc", sql, check=check, timeout=60)
        return cp.stdout.strip() if cp.returncode == 0 else ""

    def db_value(self, sql: str) -> Optional[str]:
        """One value, or None when the database cannot be asked."""
        try:
            cp = self.compose("exec", "-T", "db", "psql", "-U", DB_USER, "-d", DB_NAME,
                              "-tAc", sql, check=False, timeout=60)
        except UpdateError:
            return None
        if cp.returncode != 0:
            return None
        return cp.stdout.strip()

    def schema_now(self) -> Optional[str]:
        v = self.db_value("select version_num from alembic_version")
        return v or None

    # ── compose project ────────────────────────────────────────────────────

    def project(self) -> str:
        """The real compose project of the running stack in this folder.

        Running stack first (its labels, the same source mc_guard_single_stack
        reads), then COMPOSE_PROJECT_NAME, then compose's own default."""
        if self._project:
            return self._project
        here = {str(self.repo), os.path.realpath(str(self.repo))}
        found = None
        try:
            cp = self.sh(["docker", "ps", "--filter", "label=com.docker.compose.service=backend",
                          "--format", '{{.Label "com.docker.compose.project"}}|{{.Label "com.docker.compose.project.working_dir"}}|{{.Image}}'],
                         check=False, timeout=30)
            for line in cp.stdout.splitlines():
                parts = line.split("|")
                if len(parts) != 3 or "mc-backend" not in parts[2]:
                    continue
                if parts[1] in here or os.path.realpath(parts[1]) in here:
                    found = parts[0]
                    break
        except UpdateError:
            pass
        if not found:
            found = self.setting("COMPOSE_PROJECT_NAME") or self.repo.resolve().name
            found = re.sub(r"[^a-z0-9_-]", "", found.lower())
        self._project = found
        return found

    def foreign_stacks(self) -> List[str]:
        here = {str(self.repo), os.path.realpath(str(self.repo))}
        cp = self.sh(["docker", "ps", "--filter", "label=com.docker.compose.service=backend",
                      "--format", '{{.Label "com.docker.compose.project"}}|{{.Label "com.docker.compose.project.working_dir"}}|{{.Image}}'],
                     check=False, timeout=30)
        out = []
        for line in cp.stdout.splitlines():
            parts = line.split("|")
            if len(parts) == 3 and "mc-backend" in parts[2] and parts[1] not in here \
                    and os.path.realpath(parts[1]) not in here:
                out.append(f"{parts[0]} ({parts[1] or 'unknown folder'})")
        return out

    # ── git / release helpers ──────────────────────────────────────────────

    def current_tag(self) -> Optional[str]:
        t = self.git("describe", "--tags", "--exact-match", "HEAD", check=False)
        return t if is_release_tag(t) else None

    def latest_release(self) -> str:
        """Latest published release from the GitHub API — fail-closed.

        No fallback to git tags (ADR-094 decision 5): a tag that was never
        published as a release is not an update target."""
        req = urllib.request.Request(self.release_api, headers={"Accept": "application/vnd.github+json",
                                                                "User-Agent": "mc-update"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                body = json.loads(r.read().decode() or "{}")
        except Exception as e:  # noqa: BLE001 — any failure means "not known"
            raise UpdateError(f"release list not available ({type(e).__name__}) — try again later", kind="network") from None
        tag = body.get("tag_name") if isinstance(body, dict) else None
        if body.get("draft") or body.get("prerelease"):
            raise UpdateError(f"the latest release entry {tag!r} is a draft or pre-release")
        if not is_release_tag(tag):
            raise UpdateError(f"the release list names no stable release (got {tag!r})")
        return tag

    def resolve_target(self, version: Optional[str], expect: Optional[str]) -> str:
        if version is not None and not is_release_tag(version):
            raise UsageError(f"--version must be a release tag vX.Y.Z (got {version!r})")
        if expect is not None and not is_release_tag(expect):
            raise UsageError(f"--expect must be a release tag vX.Y.Z (got {expect!r})")
        target = version or self.latest_release()
        if expect is not None and expect != target:
            raise Refused(f"expected target {expect} differs from the release this engine chose ({target})")
        return target

    def fetch_tag(self, tag: str) -> str:
        args = ["fetch", "-q"]
        if self.git("rev-parse", "--is-shallow-repository", check=False) == "true":
            args += ["--depth", "1"]
        try:
            self.git(*args, self.repo_url, f"+refs/tags/{tag}:refs/tags/{tag}")
        except UpdateError as e:
            raise UpdateError(f"could not fetch {tag}: {e}", kind="network") from None
        return self.git("rev-parse", f"refs/tags/{tag}^{{commit}}")

    def alembic_chain(self, ref: str) -> Tuple[set, set]:
        """(revisions, down_revisions) of the migration files at ``ref``."""
        cp = self.sh(["git", "grep", "-E", r"^(revision|down_revision)[[:space:]:=]", ref, "--", ALEMBIC_DIR],
                     check=False)
        per_file: Dict[str, Dict[str, List[str]]] = {}
        for line in cp.stdout.splitlines():
            m = re.match(r"^[^:]+:([^:]+):(revision|down_revision)\s*(?::[^=]*)?=\s*(.*)$", line)
            if not m:
                continue
            per_file.setdefault(m.group(1), {})[m.group(2)] = re.findall(r"['\"]([^'\"]+)['\"]", m.group(3))
        revs, downs = set(), set()
        for d in per_file.values():
            revs.update(d.get("revision", [])[:1])
            downs.update(d.get("down_revision", []))
        return revs, downs

    def alembic_head(self, ref: str) -> str:
        revs, downs = self.alembic_chain(ref)
        heads = sorted(revs - downs)
        if len(heads) != 1:
            raise UpdateError(f"{ref} does not have exactly one migration head ({', '.join(heads) or 'none'})")
        return heads[0]

    # ── drain (Postgres, with expiry) ──────────────────────────────────────
    # Contract with the backend (PR 4c): table system_state(key text primary
    # key, value jsonb, updated_at timestamptz); the drain is the row
    # key='update_drain' with value {"run_id", "expires_at", "set_by"}. The
    # backend gives no new work while a non-expired row exists. A schema older
    # than that table has no drain: the engine then only waits for running
    # work to reach zero and says so in the journal.

    def drain_supported(self) -> bool:
        return self.db_value("select to_regclass('public.system_state') is not null") == "t"

    def drain_set(self) -> None:
        assert self.journal is not None
        j = self.journal
        expires = _iso(_now() + _dt.timedelta(seconds=self.drain_ttl))
        value = json.dumps({"run_id": j.run_id, "expires_at": expires, "set_by": "mc_update"})
        self.psql("insert into system_state (key, value, updated_at) values "
                  f"('update_drain', '{value}'::jsonb, now()) on conflict (key) do update "
                  "set value = excluded.value, updated_at = now()")
        j.data["drain"] = {"active": True, "supported": True, "expires_at": expires}

    def drain_refresh(self) -> None:
        """Push the expiry out; best effort (the database may be restarting)."""
        if not (self.journal and self.journal.data.get("drain", {}).get("active")):
            return
        try:
            self.drain_set()
        except UpdateError:
            pass

    def drain_clear(self) -> None:
        assert self.journal is not None
        d = self.journal.data.get("drain") or {}
        if not d.get("active"):
            return
        try:
            self.psql("delete from system_state where key = 'update_drain' and value->>'run_id' = "
                      f"'{self.journal.run_id}'")
            d["active"] = False
            d["cleared_at"] = _iso()
        except UpdateError as e:
            self.say(f"! could not lift the drain ({e}) — it expires at {d.get('expires_at')}")
        self.journal.data["drain"] = d

    def running_work(self) -> Optional[int]:
        v = self.db_value(RUNNING_WORK_SQL)
        return int(v) if v and v.isdigit() else None

    # ── container inspection ───────────────────────────────────────────────

    def container_state(self, service: str) -> Tuple[Optional[str], Optional[str]]:
        """(health-or-state, image id) of the service's container."""
        cid = self.compose("ps", "-q", service, check=False, timeout=30).stdout.strip().splitlines()
        if not cid:
            return None, None
        cp = self.sh(["docker", "inspect", "--format",
                      "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}|{{.Image}}",
                      cid[0]], check=False, timeout=30)
        if cp.returncode != 0 or "|" not in cp.stdout:
            return None, None
        status, image = cp.stdout.strip().split("|", 1)
        return status, image

    def image_id(self, ref: str) -> Optional[str]:
        cp = self.sh(["docker", "image", "inspect", "--format", "{{.Id}}", ref], check=False, timeout=30)
        return cp.stdout.strip() or None if cp.returncode == 0 else None

    def health_version(self) -> Optional[str]:
        try:
            with urllib.request.urlopen(f"{self.api_url}/health", timeout=5) as r:
                body = json.loads(r.read().decode() or "{}")
        except Exception:  # noqa: BLE001
            return None
        v = body.get("version") if isinstance(body, dict) else None
        return str(v) if v is not None else None

    # ── the four success checks (ADR-094 decision 4) ───────────────────────

    def wait_for_success(self, expect: Dict[str, Any], label: str) -> None:
        """1 healthy · 2 the expected image runs · 3 version · 4 schema.

        Failures count only after the backend's start_period: until then an
        "unhealthy" or a container that is not up yet is the slow first boot
        (migrations on an SD card), not a verdict."""
        started = self._clock if self._clock is not None else time.monotonic()
        deadline = started + self.verify_timeout
        last = "nothing checked yet"
        while True:
            elapsed = time.monotonic() - started
            grace_over = elapsed >= self.start_period
            states = {s: self.container_state(s) for s in MC_SERVICES}
            not_healthy = {s: st for s, (st, _img) in states.items() if st != "healthy"}
            if not not_healthy:
                problems = self._final_checks(states, expect)
                if not problems:
                    self.say(f"✓ {label}: healthy, expected images, version {expect.get('version') or '(not checked)'}, schema {expect.get('schema')}")
                    return
                raise UpdateError(f"{label}: " + "; ".join(problems))
            last = ", ".join(f"{s}={st or 'missing'}" for s, st in not_healthy.items())
            if grace_over:
                dead = {s: st for s, st in not_healthy.items() if st in (None, "unhealthy", "exited", "dead")}
                if dead:
                    raise UpdateError(f"{label}: not healthy after the start period — "
                                      + ", ".join(f"{s}={st or 'missing'}" for s, st in dead.items()))
            if time.monotonic() >= deadline:
                raise UpdateError(f"{label}: not healthy within {self.verify_timeout:.0f}s ({last})")
            time.sleep(self.poll)

    def _final_checks(self, states: Dict[str, Tuple[Optional[str], Optional[str]]],
                      expect: Dict[str, Any]) -> List[str]:
        problems = []
        for svc in MC_SERVICES:
            want = (expect.get("images") or {}).get(svc)
            got = states[svc][1]
            if want and got != want:
                problems.append(f"{svc} runs image {short(got)}, expected {short(want)}")
        want_v = normalize_version(expect.get("version"))
        if want_v:
            got_v = None
            end = time.monotonic() + max(self.poll * 3, 1.0)
            while True:
                got_v = normalize_version(self.health_version())
                if got_v is not None or time.monotonic() >= end:
                    break
                time.sleep(min(self.poll, 1.0) or 0.05)
            if got_v != want_v:
                problems.append(f"/health reports version {got_v or 'nothing'}, expected {want_v}")
        want_s = expect.get("schema")
        if want_s:
            got_s = self.schema_now()
            if got_s != want_s:
                problems.append(f"database schema is {got_s or 'unreadable'}, expected {want_s}")
        return problems

    # ── preflight ──────────────────────────────────────────────────────────

    def preflight_checks(self, version: Optional[str], expect: Optional[str]) -> Dict[str, Any]:
        """Read-only. Every check: id, status ok|warn|fail, message."""
        checks: List[Dict[str, str]] = []

        def add(cid: str, status: str, msg: str) -> None:
            checks.append({"id": cid, "status": status, "message": msg})

        result: Dict[str, Any] = {"checks": checks, "target": None, "current": None, "up_to_date": False}
        if self.setting("MC_UPDATE_MANAGED") == "deploy":
            raise Refused("this install is updated through its deploy procedure (MC_UPDATE_MANAGED=deploy)")
        if not (self.repo / "docker-compose.yml").is_file() or not self.env_path.is_file():
            add("install", "fail", f"{self.repo} is not an installed Mission Control folder (docker-compose.yml + .env)")
            return result
        add("install", "ok", "install folder found")

        channel = self.setting("MC_CHANNEL", "stable")
        if channel != "stable":
            add("channel", "fail", f"channel is {channel!r} — edge installs are updated in the terminal: ./install.sh --update")
        else:
            add("channel", "ok", "channel stable")

        busy = self.lock.fd is None and self.lock.live_holder() is not None
        try:
            open_run = find_open_run(self.state)
            unreadable = None
        except UpdateError as e:
            open_run, unreadable = None, str(e)
        if busy:
            add("lock", "fail", "another update is running")
        elif unreadable:
            add("lock", "fail", f"{unreadable} — check the update journal before updating")
        elif open_run is not None:
            add("lock", "fail", f"run {open_run.run_id} was interrupted in phase {open_run.phase} — resume it first (mc_update.py resume)")
        else:
            add("lock", "ok", "no other update running")

        try:
            self.sh(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=30)
            add("docker", "ok", "Docker answers")
        except UpdateError as e:
            add("docker", "fail", f"Docker does not answer: {e}")
            return result

        proj = self.project()
        foreign = self.foreign_stacks()
        if foreign:
            add("project", "fail", "another Mission Control stack runs on this machine: " + ", ".join(foreign))
        elif proj != "mission-control":
            add("project", "warn", f"compose project is {proj!r}; the backend's agent control assumes 'mission-control'")
        else:
            add("project", "ok", f"compose project {proj}")

        # -z: NUL-separated "XY path" records, no quoting, no stripped spaces.
        porcelain = self.sh(["git", "status", "--porcelain", "-z", "--untracked-files=no"], check=False).stdout
        dirty = [rec for rec in porcelain.split("\0") if rec and rec[3:] != FLEET_FILE]
        if dirty:
            add("checkout", "fail", f"local changes in tracked files ({len(dirty)}) — the code switch would refuse; see git status")
        else:
            add("checkout", "ok", "no local changes in tracked files")

        current = self.current_tag()
        result["current"] = current
        try:
            target = self.resolve_target(version, expect)
        except UpdateError as e:
            add("release", "fail", str(e))
            return result
        result["target"] = target
        if current and version_tuple(target) < version_tuple(current):
            add("release", "fail", f"{target} is older than the installed {current} — never backwards (migrations are forward-only)")
        elif current == target:
            result["up_to_date"] = True
            add("release", "ok", f"{current} is the latest release — nothing to do")
        else:
            add("release", "ok", f"{current or 'this checkout'} → {target}")

        schema = self.schema_now()
        if schema is None:
            add("database", "fail", "the database does not answer — the update backs it up first, so it must run")
        else:
            add("database", "ok", f"schema {schema}")
            work = self.running_work()
            if work is None:
                add("work", "fail", "running work could not be counted")
            elif work:
                add("work", "warn", f"{work} task(s) in progress — the update waits until they are done")
            else:
                add("work", "ok", "no running work")

        try:
            free_mb = shutil.disk_usage(str(self.repo)).free // (1024 * 1024)
            if free_mb < self.min_free_mb:
                add("disk", "fail", f"{free_mb} MB free, {self.min_free_mb} MB needed for images and the backup")
            else:
                add("disk", "ok", f"{free_mb} MB free")
        except OSError as e:
            add("disk", "warn", f"free space unknown ({e})")
        return result

    # ── commands ───────────────────────────────────────────────────────────

    def cmd_plan(self, version: Optional[str], expect: Optional[str]) -> int:
        pf = self.preflight_checks(version, expect)
        target = pf.get("target")
        plan = {
            "current": pf.get("current"), "target": target, "up_to_date": pf.get("up_to_date"),
            "project": self._project, "phases": list(PHASES),
            "pull": [self.image_ref(s, target) for s in PULL_SERVICES] if target else [],
            "replaces": list(MC_SERVICES), "agents": "MC_AGENT_IMAGE_TAG pinned for the next recreate; running agents untouched",
            "checks": pf["checks"],
        }
        self.emit(plan, human=self._human_plan(plan))
        return EXIT_OK

    def _human_plan(self, plan: Dict[str, Any]) -> str:
        lines = [f"Update plan: {plan['current'] or 'this checkout'} → {plan['target'] or '?'}"]
        lines += [f"  phases:   {' → '.join(plan['phases'])}",
                  f"  pull:     {', '.join(plan['pull']) or '-'}",
                  f"  replaces: {', '.join(plan['replaces'])}  (third-party images unchanged)",
                  f"  agents:   {plan['agents']}"]
        lines += [self._check_line(c) for c in plan["checks"]]
        return "\n".join(lines)

    @staticmethod
    def _check_line(c: Dict[str, str]) -> str:
        mark = {"ok": "✓", "warn": "!", "fail": "✗"}[c["status"]]
        return f"  {mark} {c['id']}: {c['message']}"

    def cmd_preflight(self, version: Optional[str], expect: Optional[str]) -> int:
        pf = self.preflight_checks(version, expect)
        ok = not any(c["status"] == "fail" for c in pf["checks"])
        pf["ok"] = ok
        self.emit(pf, human="\n".join(["Preflight:"] + [self._check_line(c) for c in pf["checks"]]))
        return EXIT_OK if ok else EXIT_ABORTED

    def cmd_start(self, version: Optional[str], expect: Optional[str], source: str) -> int:
        self.lock.acquire(None)
        try:
            self.prune_private_copies()
            pf = self.preflight_checks(version, expect)
            fails = [c for c in pf["checks"] if c["status"] == "fail"]
            if fails:
                self.emit({"ok": False, "checks": pf["checks"]},
                          human="\n".join(["✗ Update not started — nothing was changed:"] + [self._check_line(c) for c in fails]))
                return EXIT_ABORTED
            if pf["up_to_date"]:
                self.emit({"ok": True, "up_to_date": True, "current": pf["current"]},
                          human=f"✓ already on {pf['current']} — nothing to do.")
                return EXIT_OK
            self.journal = Journal.create(self.state, source)
            self.lock.set_run(self.journal.run_id)
            j = self.journal
            j.data["project"] = self.project()
            j.data["from"] = {"tag": pf["current"], "commit": self.git("rev-parse", "HEAD"),
                              "branch": self.git("symbolic-ref", "-q", "--short", "HEAD", check=False) or None,
                              "version": normalize_version(pf["current"])}
            j.data["target"] = {"tag": pf["target"], "version": normalize_version(pf["target"])}
            j.data["preflight"] = pf["checks"]
            j.finish("preflight", f"{pf['current'] or 'checkout'} → {pf['target']}")
            _fault("preflight.done")
            self.say(f"→ update {pf['current'] or 'this checkout'} → {pf['target']} (run {j.run_id})")
            return self.run_forward()
        finally:
            self.lock.release()

    def run_forward(self) -> int:
        """drain … backup in this engine, then hand over to the target's engine."""
        j = self.journal
        assert j is not None
        try:
            self.phase_drain()
            self.phase_fetch()
            self.phase_verify_release()
            self.phase_pull()
            self.phase_backup()
            self.claim_switch()
        except (UpdateError, CancelRequested, OSError) as e:
            return self.abort(str(e), getattr(e, "kind", "cancelled"))
        self.handoff("_continue")
        return self.run_from_switch()

    def run_from_switch(self, resumed: bool = False) -> int:
        try:
            self.phase_switch(resumed=resumed)
            self.phase_up()
            self.phase_verify()
        except (UpdateError, OSError) as e:
            self.say(f"✗ {e}")
            if not self.journal.data["rollback"].get("prepared"):
                # Failed while recording the way back — before any change.
                return self.abort(str(e), getattr(e, "kind", "error"))
            return self.rollback(str(e))
        j = self.journal
        assert j is not None
        self.drain_clear()
        j.end("done", f"{j.data['target']['tag']} runs and passed the four checks")
        self.say(f"✅ Update complete — release {j.data['target']['tag']} is running and verified.")
        if j.data.get("backup", {}).get("file"):
            self.say(f"  Backup from before the update: {j.data['backup']['file']}")
        return EXIT_OK

    def abort(self, reason: str, kind: str) -> int:
        j = self.journal
        assert j is not None
        self.drain_clear()
        j.end("aborted", reason, error=reason, kind=kind)
        self.drop_private_copies(j)
        self.say(f"✗ Update aborted — nothing was changed: {reason}")
        return EXIT_ABORTED

    # ── phases before the switch (nothing changes on disk or in containers) ──

    def cancel_requested(self) -> bool:
        return self.journal is not None and gate_holder(self.run_dir()) == "cancel"

    def _check_cancel(self) -> None:
        if self.cancel_requested():
            raise CancelRequested("cancelled before the switch — nothing was changed")

    def claim_switch(self) -> None:
        """The point of no return, decided atomically against a cancel."""
        j = self.journal
        assert j is not None
        if claim_gate(self.run_dir(), "switch") != "switch":
            raise CancelRequested("cancelled before the switch — nothing was changed")
        j.event("backup", "claimed", "switch")
        j.save()

    def drain_and_wait(self) -> None:
        """Drain on, then wait until no work runs (ADR-094 decision 7).

        Used before every code switch: the update's own and a requested way
        back. Raises UpdateError on timeout; the caller lifts the drain."""
        if self.drain_supported():
            self.drain_set()
            self.say("→ no new work is handed out while the update runs")
        else:
            assert self.journal is not None
            self.journal.data["drain"] = {"active": False, "supported": False}
            self.say("! this version has no drain switch — only waiting for running work to finish")
        assert self.journal is not None
        self.journal.save()
        deadline = time.monotonic() + self.drain_timeout
        while True:
            self._check_cancel()
            n = self.running_work()
            if n == 0:
                return
            if n is None:
                raise UpdateError("running work could not be counted (database not reachable)")
            if time.monotonic() >= deadline:
                raise UpdateError(f"{n} task(s) still running after {self.drain_timeout:.0f}s")
            self.say(f"… waiting for {n} running task(s) to finish")
            time.sleep(self.poll)

    def phase_drain(self) -> None:
        j = self.journal
        assert j is not None
        j.begin("drain")
        _fault("drain.intent")
        self._check_cancel()
        try:
            self.drain_and_wait()
        except UpdateError as e:
            if "still running" in str(e):
                raise UpdateError(f"{e} — the update did not start", kind=e.kind) from None
            raise
        j.finish("drain", "quiet")
        _fault("drain.done")

    def phase_fetch(self) -> None:
        j = self.journal
        assert j is not None
        tag = j.data["target"]["tag"]
        j.begin("fetch", tag)
        _fault("fetch.intent")
        self._check_cancel()
        self.drain_refresh()
        j.data["target"]["commit"] = self.fetch_tag(tag)
        engine = self.run_dir() / "mc_update.py"
        cp = self.sh(["git", "show", f"refs/tags/{tag}:{ENGINE_PATH_IN_REPO}"], check=False)
        if cp.returncode == 0 and cp.stdout:
            copy_private_bytes(engine, cp.stdout.encode())
            j.data["engine"]["target_copy"] = str(engine)
        else:
            j.data["engine"]["target_copy"] = None
        j.finish("fetch", j.data["target"]["commit"])
        _fault("fetch.done")

    def phase_verify_release(self) -> None:
        """Structural checks. Signature + published-release proof: PR 6."""
        j = self.journal
        assert j is not None
        tag = j.data["target"]["tag"]
        j.begin("verify_release", tag)
        _fault("verify_release.intent")
        self._check_cancel()
        cur = j.data["from"].get("tag")
        if not is_release_tag(tag):
            raise UpdateError(f"{tag} is not a release tag")
        if cur and version_tuple(tag) <= version_tuple(cur):
            raise UpdateError(f"{tag} is not newer than {cur}")
        head = self.alembic_head(f"refs/tags/{tag}")
        revs, _ = self.alembic_chain(f"refs/tags/{tag}")
        schema = self.schema_now()
        if schema is None:
            raise UpdateError("the database does not answer — cannot check that the release knows its schema")
        if schema not in revs:
            raise UpdateError(f"the database schema {schema} is not part of {tag} — switching would move the code below the data")
        j.data["target"]["schema_head"] = head
        j.finish("verify_release", f"head {head}")
        _fault("verify_release.done")

    def phase_pull(self) -> None:
        j = self.journal
        assert j is not None
        tag = j.data["target"]["tag"]
        j.begin("pull", " ".join(PULL_SERVICES))
        _fault("pull.intent")
        self._check_cancel()
        self.drain_refresh()
        self.say(f"→ downloading {', '.join(self.image_ref(s, tag) for s in PULL_SERVICES)} …")
        try:
            self.compose("pull", *PULL_SERVICES, env={"MC_IMAGE_TAG": image_tag(tag)}, timeout=3600,
                         cancellable=True)
        except UpdateError as e:
            raise UpdateError(f"download failed — the running version is unchanged ({e})", kind="network") from None
        target_images = {}
        for svc in MC_SERVICES:
            iid = self.image_id(self.image_ref(svc, tag))
            if not iid:
                raise UpdateError(f"image {self.image_ref(svc, tag)} is not on this machine after the pull")
            target_images[svc] = iid
        j.data["images"]["target"] = target_images
        j.finish("pull", "images present")
        _fault("pull.done")

    def phase_backup(self) -> None:
        """Right before the switch, so data written after it is seconds, not minutes.

        PR 2b moves this to backup.sh's own pre-update prefix; here it is the
        normal backup."""
        j = self.journal
        assert j is not None
        j.begin("backup")
        _fault("backup.intent")
        self._check_cancel()
        self.drain_refresh()
        script = self.repo / "backup.sh"
        if not os.access(str(script), os.X_OK):
            raise UpdateError("./backup.sh not found — the update does not run without a backup")
        marker = time.time() - 1
        self.say("→ backing up before the update (./backup.sh) …")
        cp = self.sh([str(script)], check=False, env={"COMPOSE_PROJECT_NAME": self.project()}, timeout=3600)
        if cp.returncode != 0:
            tail = " | ".join((cp.stderr or cp.stdout or "").strip().splitlines()[-3:])
            raise UpdateError(f"the backup failed — nothing was changed ({tail})")
        j.data["backup"] = {"file": self.newest_backup(marker), "taken_at": _iso()}
        j.finish("backup", j.data["backup"]["file"] or "written by backup.sh")
        _fault("backup.done")

    def newest_backup(self, since: float) -> Optional[str]:
        d = self.setting("MC_BACKUP_DIR", str(Path.home() / "mc-backups"))
        if d.startswith("~/"):
            d = str(Path.home() / d[2:])
        base = Path(d) if os.path.isabs(d) else self.repo / d
        best = None
        for folder in (base, self.repo / "backups"):
            if not folder.is_dir():
                continue
            for f in folder.glob("mc_backup_*.sql.gz"):
                if f.stat().st_mtime >= since and (best is None or f.stat().st_mtime > best.stat().st_mtime):
                    best = f
        return str(best) if best else None

    # ── hand-over to the target's engine ───────────────────────────────────

    def handoff(self, command: str) -> None:
        """exec the target tag's copy of this engine; same PID, same lock.

        Skipped (logged) when the target has no engine or speaks another
        journal schema — then this engine finishes the run itself."""
        j = self.journal
        assert j is not None
        copy = j.data.get("engine", {}).get("target_copy")
        if not copy or not Path(copy).is_file():
            j.event(j.phase, "note", "target has no engine — the installed engine continues")
            j.save()
            return
        if os.path.realpath(copy) == os.path.realpath(__file__):
            return
        cp = subprocess.run([sys.executable, copy, "--contract"], capture_output=True, text=True,
                            stdin=subprocess.DEVNULL, timeout=60)
        try:
            contract = json.loads(cp.stdout or "{}")
        except ValueError:
            contract = {}
        if cp.returncode != 0 or contract.get("journal_schema") != JOURNAL_SCHEMA:
            j.event(j.phase, "note", f"target engine speaks journal schema {contract.get('journal_schema')} — the installed engine continues")
            j.save()
            return
        j.data["engine"]["handoff_at"] = _iso()
        j.event(j.phase, "handoff", copy)
        j.save()
        self.lock.keep_across_exec()
        sys.stdout.flush()
        sys.stderr.flush()
        args = [sys.executable, copy, command, "--run", j.run_id, "--repo", str(self.repo)]
        if self.out_json:
            args.append("--json")
        os.execv(sys.executable, args)

    # ── switch, up, verify ─────────────────────────────────────────────────

    def phase_switch(self, resumed: bool = False) -> None:
        j = self.journal
        assert j is not None
        tag = j.data["target"]["tag"]
        rb = j.data["rollback"]
        if not rb.get("prepared"):
            self.prepare_rollback()
        j.begin("switch", f"{j.data['from'].get('tag') or j.data['from'].get('commit')} → {tag}")
        _fault("switch.intent")
        self.drain_refresh()
        # Second belt next to the backend's docker guard (ADR-094 decision 7):
        # no background loop may run its own `compose up` while we switch.
        self.compose("stop", "mc-worker", check=False, timeout=180)
        _fault("switch.worker_stopped")
        self.checkout_carrying_fleet(f"refs/tags/{tag}", point="switch")
        self.restore_env_if_missing()
        env_file_set(self.env_path, {"MC_CHANNEL": "stable", "MC_IMAGE_TAG": image_tag(tag),
                                     "MC_AGENT_IMAGE_TAG": image_tag(tag)})
        _fault("switch.pins")
        setup = self.repo / "setup.sh"
        if os.access(str(setup), os.X_OK):
            # The new version may need .env keys the old one did not know;
            # setup.sh only appends missing keys to an existing .env.
            self.sh([str(setup)], timeout=600)
        j.finish("switch", self.git("rev-parse", "HEAD"))
        _fault("switch.done")

    def prepare_rollback(self) -> None:
        """Everything the way back needs, journaled before the first change."""
        j = self.journal
        assert j is not None
        rd = self.run_dir()
        rb: Dict[str, Any] = {"prepared": False, "schema_before": self.schema_now(),
                              "ref": j.data["from"].get("tag") or j.data["from"].get("commit"),
                              "commit": j.data["from"].get("commit"), "branch": j.data["from"].get("branch"),
                              "version": j.data["from"].get("version")}
        copy_private(self.env_path, rd / "env.before")
        rb["env_copy"] = str(rd / "env.before")
        if self.fleet_path.is_file():
            copy_private(self.fleet_path, rd / "fleet.before.yml")
            rb["fleet_copy"] = str(rd / "fleet.before.yml")
        else:
            rb["fleet_copy"] = None
        rb["images"] = {s: self.container_state(s)[1] for s in MC_SERVICES}
        rb["prepared"] = True
        j.data["rollback"] = rb
        j.event("switch", "prepared", f"schema_before={rb['schema_before']}")
        j.save()

    def checkout_carrying_fleet(self, ref: str, point: str) -> None:
        """git checkout that never loses the operator's fleet file.

        The fleet file rewrites itself at runtime and left version control,
        but older releases still track it; git would refuse the switch or
        delete it. Its copy lives in the run folder (journaled), and it is
        always written back — also by resume after a crash in between."""
        j = self.journal
        assert j is not None
        has_fleet = bool(j.data["rollback"].get("fleet_copy"))
        if has_fleet and self.fleet_path.exists():
            tracked = self.sh(["git", "ls-files", "--error-unmatch", FLEET_FILE], check=False).returncode == 0
            if tracked:
                self.git("checkout", "-q", "--", FLEET_FILE)
            else:
                self.fleet_path.unlink()
        _fault(f"{point}.parked")
        self.git("-c", "advice.detachedHead=false", "checkout", "-q", ref)
        _fault(f"{point}.checked_out")
        self.put_fleet_back()
        _fault(f"{point}.fleet_back")

    def put_fleet_back(self) -> None:
        j = self.journal
        assert j is not None
        copy = j.data["rollback"].get("fleet_copy")
        if not copy:
            return
        src = Path(copy)
        if not src.is_file():
            raise UpdateError(f"the fleet file copy {src} is missing")
        self.fleet_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.fleet_path.exists() or self.fleet_path.read_bytes() != src.read_bytes():
            write_in_place(self.fleet_path, src.read_bytes())

    def restore_env_if_missing(self) -> None:
        j = self.journal
        assert j is not None
        copy = j.data["rollback"].get("env_copy")
        if not self.env_path.exists() and copy and Path(copy).is_file():
            write_in_place(self.env_path, Path(copy).read_bytes())

    def phase_up(self) -> None:
        j = self.journal
        assert j is not None
        j.begin("up", " ".join(MC_SERVICES))
        _fault("up.intent")
        self.drain_refresh()
        self.compose_up()
        j.finish("up")
        _fault("up.done")

    def compose_up(self) -> None:
        """`up` of the MC services; its time counts against the check limit.

        --pull never: up uses exactly the images pulled and checked before;
        nothing else is downloaded behind the engine's back. Through
        depends_on (service_healthy) compose itself blocks until the backend
        is healthy, so `up` and the four checks share one limit (ADR-094
        decision 4), counted from here."""
        self._clock = time.monotonic()
        self.compose("up", "-d", "--pull", "never", *MC_SERVICES, timeout=max(self.verify_timeout, 1.0))

    def phase_verify(self) -> None:
        j = self.journal
        assert j is not None
        j.begin("verify")
        _fault("verify.intent")
        self.drain_refresh()
        t = j.data["target"]
        self.say(f"→ checking the new version (up to {self.verify_timeout / 60:.0f} min, the first {self.start_period:.0f}s do not count) …")
        self.wait_for_success({"images": j.data["images"].get("target"), "version": t.get("version"),
                               "schema": t.get("schema_head")}, "new version")
        j.finish("verify")
        _fault("verify.done")

    # ── the way back ───────────────────────────────────────────────────────

    def rollback(self, reason: str, requested: bool = False) -> int:
        """R1: old code, old .env (in place), old fleet file, up, four checks.

        Only when the database schema is still ``schema_before``. Otherwise
        the database must come back from the pre-update backup first (R2,
        PR 2b) — until then the run ends ``rollback_failed`` with the backup
        and the command, instead of starting old code on a newer schema."""
        j = self.journal
        assert j is not None
        rb = j.data["rollback"]
        j.data["error"] = j.data.get("error") or reason
        j.begin("rolling_back", reason)
        _fault("rolling_back.intent")
        self.say(f"→ going back to {rb.get('ref')} …")
        try:
            if not rb.get("prepared"):
                raise RollbackUnavailable("the way back was never prepared")
            now = self.schema_now()
            if rb.get("schema_before") is None or now != rb.get("schema_before"):
                self.restore_database(now)
            self.compose("stop", "mc-worker", check=False, timeout=180)
            self.checkout_back()
            env_copy = Path(rb["env_copy"])
            if not env_copy.is_file():
                raise RollbackUnavailable(f"the .env copy {env_copy} is missing")
            write_in_place(self.env_path, env_copy.read_bytes())
            self.compose_up()
            self.wait_for_success({"images": rb.get("images"), "version": rb.get("version"),
                                   "schema": rb.get("schema_before")}, "previous version")
        except (UpdateError, RollbackUnavailable, OSError) as e:
            msg = f"the way back failed: {e}"
            j.end("rollback_failed", msg, error=f"{reason} — {msg}")
            self.say(f"✗ Update failed and {msg}")
            backup = (j.data.get("backup") or {}).get("file")
            self.say("  Mission Control stays in no-new-work mode. On this machine, in the install folder:")
            if backup:
                self.say(f"    1. ./backup.sh restore {backup} --yes   (the backup from right before the switch)")
            else:
                self.say("    1. ./backup.sh restore <the backup from right before the update> --yes")
            self.say("    2. python3 scripts/mc_update.py rollback")
            return EXIT_ROLLBACK_FAILED
        self.drain_clear()
        j.end("rolled_back", f"back on {rb.get('ref')}", error=reason)
        self.drop_private_copies(j)
        if requested:
            self.say(f"✓ Mission Control was set back to {rb.get('ref')} as requested; the database was not changed.")
            return EXIT_ROLLED_BACK
        self.say(f"✗ Update failed ({reason}). Mission Control was set back to {rb.get('ref')}; the database was not changed.")
        return EXIT_FAILED

    # ── the run's private copies (.env holds secrets) ──────────────────────

    ROLLBACK_WINDOW = _dt.timedelta(days=14)

    def drop_private_copies(self, j: "Journal") -> None:
        """ADR-094 decision 4: the run's copy of .env and the fleet file is
        removed with the run — once nothing can need it any more."""
        rd = self.state / f"run-{j.run_id}"
        for name in ("env.before", "fleet.before.yml"):
            try:
                (rd / name).unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                self.say(f"! could not remove the run's private copy {name} ({e})")

    def prune_private_copies(self) -> None:
        """aborted / rolled_back: right away; done: after its 14-day way back.
        rollback_failed keeps them — the way back still needs them."""
        for path in list_runs(self.state):
            try:
                j = Journal.load(path)
            except UpdateError:
                continue
            if j.phase in ("aborted", "rolled_back"):
                self.drop_private_copies(j)
            elif j.phase == "done":
                try:
                    finished = _parse_iso(j.data.get("finished_at") or j.data["updated_at"])
                except (KeyError, ValueError):
                    continue
                if _now() - finished > self.ROLLBACK_WINDOW:
                    self.drop_private_copies(j)

    def restore_database(self, schema_now: Optional[str]) -> None:
        j = self.journal
        assert j is not None
        backup = (j.data.get("backup") or {}).get("file") or "<the backup named in the journal>"
        raise RollbackUnavailable(
            f"the database changed (schema {schema_now or 'unreadable'}, before {j.data['rollback'].get('schema_before')}) "
            f"and must be restored first: ./backup.sh restore {backup} --yes — this engine version does not restore automatically yet")

    def checkout_back(self) -> None:
        j = self.journal
        assert j is not None
        rb = j.data["rollback"]
        ref = rb.get("commit")
        if not ref:
            raise RollbackUnavailable("the previous commit is not known")
        self.checkout_carrying_fleet(ref, point="rolling_back")
        branch = rb.get("branch")
        if branch and self.git("rev-parse", "-q", "--verify", f"refs/heads/{branch}", check=False) == ref:
            self.git("checkout", "-q", branch)

    # ── resume / rollback / status commands ────────────────────────────────

    def cmd_continue(self, run_id: str) -> int:
        """Entry of the target engine after the hand-over (same PID, same lock)."""
        j = find_run(self.state, run_id)
        assert j is not None
        self.journal = j
        self.lock.acquire(j.run_id)
        try:
            if j.phase != "backup" or j.data.get("phase_state") != "done":
                raise UsageError(f"_continue expects a run right after its backup (run is in {j.phase})")
            j.event("switch", "engine", "target engine took over")
            j.save()
            return self.run_from_switch()
        finally:
            self.lock.release()

    def cmd_resume(self, run_id: Optional[str]) -> int:
        j = find_run(self.state, run_id) if run_id else find_open_run(self.state)
        if j is None or j.phase in TERMINAL:
            self.emit({"resumed": False, "run_id": j.run_id if j else None, "phase": j.phase if j else None},
                      human="✓ no interrupted update — nothing to resume.")
            return EXIT_OK
        self.journal = j
        self.lock.acquire(j.run_id)
        try:
            if j.phase in POST_SWITCH:
                # From the switch on, the target's engine finishes the run.
                self.handoff("resume")
            j.data.setdefault("resumes", []).append({"at": _iso(), "phase": j.phase,
                                                     "phase_state": j.data.get("phase_state")})
            j.event(j.phase, "resume", f"interrupted in {j.phase} ({j.data.get('phase_state')})")
            j.save()
            self.say(f"→ resuming run {j.run_id}, interrupted in phase {j.phase}")
            if j.phase in PRE_SWITCH:
                # Nothing was changed yet: stop cleanly, never carry on blindly.
                return self.abort(f"interrupted in phase {j.phase} before anything was changed", "interrupted")
            if j.phase in POST_SWITCH:
                return self.resume_after_switch()
            if j.phase == "rolling_back":
                return self.rollback(j.data.get("error") or "resumed the way back")
            raise UpdateError(f"unknown phase {j.phase!r} in the journal")
        finally:
            self.lock.release()

    def resume_after_switch(self) -> int:
        j = self.journal
        assert j is not None
        try:
            if not j.data["rollback"].get("prepared"):
                raise UpdateError("the switch never started — nothing to resume")
            self.restore_env_if_missing()
            self.put_fleet_back()
        except (UpdateError, OSError) as e:
            return self.rollback(str(e))
        redo_switch = j.phase == "switch" and j.data.get("phase_state") != "done"
        if redo_switch:
            return self.run_from_switch(resumed=True)
        try:
            self.phase_up()
            self.phase_verify()
        except (UpdateError, OSError) as e:
            self.say(f"✗ {e}")
            return self.rollback(str(e))
        self.drain_clear()
        j.end("done", f"{j.data['target']['tag']} runs and passed the four checks (after resume)")
        self.say(f"✅ Update complete after resume — release {j.data['target']['tag']} is running and verified.")
        return EXIT_OK

    def cmd_rollback(self, run_id: Optional[str]) -> int:
        j = find_run(self.state, run_id)
        if j is None:
            self.emit({"ok": False}, human="✗ no update run on this machine — nothing to go back to.")
            return EXIT_USAGE
        self.journal = j
        if self.lock.live_holder() is not None:
            if j.phase in PRE_SWITCH and claim_gate(self.run_dir(), "cancel") == "cancel":
                self.emit({"ok": True, "cancel_requested": True, "run_id": j.run_id},
                          human=(f"→ cancel requested for run {j.run_id}: it stops before the switch "
                                 "(a running download is stopped, a running backup is finished first)."))
                return EXIT_OK
            raise Locked(f"too late to cancel run {j.run_id}: it is past the point of no return "
                         f"(phase {j.phase}) — it finishes, or rolls back on its own if it fails")
        self.lock.acquire(j.run_id)
        try:
            if j.phase in ("aborted", "rolled_back"):
                self.emit({"ok": True, "run_id": j.run_id, "phase": j.phase},
                          human=f"✓ run {j.run_id} already ended {j.phase} — nothing to undo.")
                return EXIT_OK
            if j.phase in PRE_SWITCH:
                return self.abort(f"cancelled in phase {j.phase} before anything was changed", "cancelled")
            if j.phase == "done":
                age = _now() - _parse_iso(j.data.get("finished_at") or j.data["updated_at"])
                if age > _dt.timedelta(days=14):
                    raise Refused(f"run {j.run_id} finished {age.days} days ago — its backup is older than 14 days")
                # The new version runs fine. Refuse what this engine cannot do
                # before touching anything, instead of marking a healthy run
                # "rollback failed".
                before, now = j.data["rollback"].get("schema_before"), self.schema_now()
                if before is None or now != before:
                    try:
                        self.restore_database(now)
                    except RollbackUnavailable as e:
                        raise Refused(str(e)) from None
                # A code switch like any other: no new work, running work
                # finishes first, nothing is cut off (ADR-094 decision 7).
                try:
                    self.drain_and_wait()
                except UpdateError as e:
                    self.drain_clear()
                    j.save()
                    raise Refused(f"{e} — the way back did not start, nothing was changed") from None
            return self.rollback("rollback requested", requested=True)
        finally:
            self.lock.release()

    def cmd_status(self, run_id: Optional[str]) -> int:
        j = find_run(self.state, run_id)
        holder = self.lock.holder()
        lock = {"held": bool(holder and holder.get("alive")), "pid": holder.get("pid") if holder else None,
                "pid_alive": holder.get("alive") if holder else None}
        if j is None:
            self.emit({"run_id": None, "lock": lock}, human="No update run on this machine.")
            return EXIT_OK
        d = j.data
        home = str(Path.home())

        def clean(text: Any) -> Any:
            return text.replace(home, "~") if isinstance(text, str) else text

        out = {
            "run_id": d["run_id"], "phase": d["phase"], "phase_state": d.get("phase_state"),
            "status": d.get("status"), "source": d.get("source"),
            "from": {k: d.get("from", {}).get(k) for k in ("tag", "version")},
            "target": {k: d.get("target", {}).get(k) for k in ("tag", "version")},
            "created_at": d.get("created_at"), "updated_at": d.get("updated_at"),
            "finished_at": d.get("finished_at"), "error": clean(d.get("error")),
            "failure_kind": d.get("failure_kind"),
            "backup": Path(d["backup"]["file"]).name if d.get("backup", {}).get("file") else None,
            "interrupted": d["phase"] not in TERMINAL and not lock["held"],
            "lock": lock,
            "events": [{k: clean(e.get(k)) for k in ("at", "phase", "event", "detail")} for e in d.get("events", [])[-20:]],
        }
        self.emit(out, human=(f"Run {out['run_id']}: {out['phase']} ({out['phase_state']}) "
                              f"{out['from'].get('tag') or '?'} → {out['target'].get('tag') or '?'}"
                              + (f"\n  error: {out['error']}" if out["error"] else "")
                              + ("\n  interrupted — run: mc_update.py resume" if out["interrupted"] else "")))
        return EXIT_OK

    def emit(self, data: Dict[str, Any], human: str) -> None:
        self.emitted = True
        if self.out_json:
            print(json.dumps(data, indent=2, sort_keys=True), flush=True)
        else:
            print(human, flush=True)


def copy_private_bytes(dst: Path, data: bytes) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(dst), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def short(image_id: Optional[str]) -> str:
    if not image_id:
        return "none"
    return image_id.split(":", 1)[-1][:12]


# ── CLI ─────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mc_update.py", description="Mission Control update engine (ADR-094).")
    p.add_argument("--contract", action="store_true", help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd")

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--repo", default=None, help="install folder (default: MC_REPO_PATH or the working directory)")
        sp.add_argument("--json", action="store_true", help="machine-readable output (for the bridge)")

    for name in ("plan", "preflight", "start"):
        sp = sub.add_parser(name)
        common(sp)
        sp.add_argument("--version", dest="version", default=None, help="a specific release vX.Y.Z (default: latest published)")
        sp.add_argument("--expect", default=None, help="refuse (exit 9) unless the engine's target is this tag")
        if name == "start":
            sp.add_argument("--source", choices=("terminal", "bridge"), default="terminal")
    for name in ("status", "resume", "rollback", "_continue"):
        sp = sub.add_parser(name)
        common(sp)
        sp.add_argument("--run", default=None, help="run id (default: the latest / the interrupted one)")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.contract:
        print(json.dumps({"journal_schema": JOURNAL_SCHEMA, "phases": list(PHASES)}))
        return EXIT_OK
    if not args.cmd:
        parser.print_help()
        return EXIT_USAGE
    repo = Path(args.repo or os.environ.get("MC_REPO_PATH") or os.getcwd()).absolute()
    state = Path(os.environ.get("MC_UPDATE_HOME") or (Path.home() / ".mc" / "update")).expanduser()
    try:
        eng = Engine(repo, state, out_json=args.json)
        rc = _dispatch(eng, args)
        if args.json and not eng.emitted and eng.journal is not None:
            # One closing line for the bridge; the details are in `status --json`.
            print(json.dumps({"run_id": eng.journal.run_id, "phase": eng.journal.phase, "exit": rc}), flush=True)
        return rc
    except UsageError as e:
        print(f"✗ {e}", file=sys.stderr)
        return EXIT_USAGE
    except Refused as e:
        print(f"✗ refused: {e}", file=sys.stderr)
        return EXIT_REFUSED
    except Locked as e:
        print(f"✗ {e}", file=sys.stderr)
        return EXIT_LOCKED
    except UpdateError as e:
        print(f"✗ {e}", file=sys.stderr)
        return EXIT_FAILED


def _dispatch(eng: Engine, args: argparse.Namespace) -> int:
    if args.cmd == "plan":
        return eng.cmd_plan(args.version, args.expect)
    if args.cmd == "preflight":
        return eng.cmd_preflight(args.version, args.expect)
    if args.cmd == "start":
        return eng.cmd_start(args.version, args.expect, args.source)
    if args.cmd == "status":
        return eng.cmd_status(args.run)
    if args.cmd == "resume":
        return eng.cmd_resume(args.run)
    if args.cmd == "rollback":
        return eng.cmd_rollback(args.run)
    if args.cmd == "_continue":
        if not args.run:
            raise UsageError("_continue needs --run")
        return eng.cmd_continue(args.run)
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
