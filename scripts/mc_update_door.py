"""mc_update_door.py — the cli-bridge's door to the update engine (ADR-094 decision 6).

The backend never runs the update itself. It asks the cli-bridge on the host,
and the bridge answers on exactly five routes and nothing else::

    POST /mc-update/preflight   {"expected_target"?}   read-only checks + bridge checks
    POST /mc-update/start       {"expected_target"}    start a run, detached     → 202
    GET  /mc-update/status      ?run=<id>              the journal, filtered
    POST /mc-update/rollback    {"run_id"?}            cancel a live run before the switch,
                                                       or go back after a finished one
    POST /mc-update/resume      {"run_id"?}            finish an interrupted run

Rules of the door:

* **Own token scope.** ``X-MC-Update-Token`` from its own 0600 file
  (``MC_UPDATE_TOKEN_FILE``, default ``~/.mc/secrets/mc-update.token``). The
  bridge's general token does not open it, and this token opens nothing else.
  There is deliberately no environment variable for it: ``.env`` is readable
  inside agent containers.
* **No free input.** The engine picks the target; the caller's
  ``expected_target`` is only compared (``--expect``, mismatch → 409). No tag,
  ref, URL, path or command is ever taken from a request.
* **409** managed install (``MC_UPDATE_MANAGED=deploy``) or target mismatch ·
  **423** a run is live or interrupted · **429** more than
  ``MC_UPDATE_MAX_STARTS_PER_DAY`` (default 3) starts from the door in 24 h —
  preflight, status, resume, rollback, terminal runs and starts that failed on
  the network before the switch do not count.
* **Detached.** The engine runs in its own unit/session, so a bridge restart
  (or crash, or ``launchctl kickstart -k``) never kills a run: Linux
  ``systemd-run --user`` (own cgroup, journald), else — and on macOS — a
  double fork in a new session. The job's exit code lands in
  ``<state>/bridge/<job>.exit``.
* **Owner of resumption.** ``boot()`` runs when the bridge starts: an
  interrupted run whose lock holder is dead is resumed once Docker answers
  (max. 10 min); without one, a drain no run owns any more is lifted.

Python standard library only, like the engine. The engine module is imported
from the same folder (same checkout, same version as this file).
"""

from __future__ import annotations

import datetime as _dt
import getpass
import importlib
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

API_VERSION = 1
TOKEN_HEADER = "X-MC-Update-Token"
MIN_TOKEN_LEN = 32
MAX_BODY = 4096
DEFAULT_MAX_STARTS = 3
LINGER_DIR = Path("/var/lib/systemd/linger")
JOB_FILE_MAX_AGE = 14 * 86400   # seconds a job's .out/.exit stay in <state>/bridge

# route → (method, allowed body keys)
ROUTES: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "/mc-update/preflight": ("POST", ("expected_target",)),
    "/mc-update/start": ("POST", ("expected_target",)),
    "/mc-update/status": ("GET", ()),
    "/mc-update/rollback": ("POST", ("run_id",)),
    "/mc-update/resume": ("POST", ("run_id",)),
}

# The job: cd into the install, run the engine with its output in a private
# file, then record the exit code atomically. Arguments come as positional
# parameters — nothing from a request is ever pasted into the script text.
JOB_SCRIPT = ('umask 077; cd "$1" || { printf "97\\n" > "$3"; exit 97; }; out=$2; ex=$3; shift 3; '
              '"$@" > "$out" 2>&1 < /dev/null; rc=$?; '
              'printf "%s\\n" "$rc" > "$ex.tmp" && mv "$ex.tmp" "$ex"')
# Double fork: Popen forks the shell (in a new session), the shell forks the
# job into the background and exits at once — the job is re-parented to init
# and lives in a session of its own, outside the bridge's process group.
FORK_SCRIPT = "( " + JOB_SCRIPT + " ) < /dev/null > /dev/null 2>&1 &"

# Environment handed to a systemd-run job (it does not inherit the caller's).
_PASS_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "DOCKER_HOST", "DOCKER_CONFIG",
             "DOCKER_CONTEXT", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
_PASS_PREFIXES = ("MC_", "COMPOSE_")


class WeakTokenError(ValueError):
    """The update token file holds a guessable value — the door stays shut."""


def engine_module():
    """The engine of this checkout (scripts/mc_update.py, next to this file)."""
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    return importlib.import_module("mc_update")


def current_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 — no passwd entry in odd containers
        return os.environ.get("USER", "")


def load_or_create_token(path: Path) -> str:
    """The door's token: read the 0600 file, create it once when missing.

    Never printed or logged. Too short → WeakTokenError (better a shut door
    than a guessable one)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        token = path.read_text().strip()
    except FileNotFoundError:
        token = ""
    if token:
        if len(token) < MIN_TOKEN_LEN:
            raise WeakTokenError(f"{path.name} holds fewer than {MIN_TOKEN_LEN} characters")
        if path.stat().st_mode & 0o077:
            os.chmod(path, 0o600)
        return token
    token = secrets.token_urlsafe(32)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (token + "\n").encode("ascii"))
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return token


def default_token_file() -> Path:
    return Path(os.environ.get("MC_UPDATE_TOKEN_FILE", "").strip()
                or (Path.home() / ".mc" / "secrets" / "mc-update.token"))


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _iso() -> str:
    return _now().strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_json(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    if not text:
        return {}
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else {}
    except ValueError:
        pass
    start = text.find("{")
    while start != -1:
        try:
            v = json.loads(text[start:])
            return v if isinstance(v, dict) else {}
        except ValueError:
            start = text.find("{", start + 1)
    return {}


class Door:
    def __init__(self, repo: Path, state: Path, token: Optional[str],
                 env: Optional[Mapping[str, str]] = None, python: str = sys.executable,
                 log: Optional[Callable[[str], None]] = None, start_wait: float = 90.0,
                 sync_timeout: float = 180.0, bridge_token: Optional[str] = None):
        self.repo = Path(repo)
        self.state = Path(state)
        self.env = dict(env if env is not None else os.environ)
        self.python = python
        self.log = log or (lambda m: print(m, file=sys.stderr, flush=True))
        self.start_wait = start_wait
        self.sync_timeout = sync_timeout
        # One token per scope: the general bridge token must never open this door.
        if token and bridge_token and token == bridge_token:
            self.log("mc-update door: its token equals the bridge token — door closed")
            token = None
        self.token = token
        self.detach: Optional[str] = None       # None = auto, or "systemd" / "fork"
        self.new_session = True                 # tests flip it to prove the detaching matters
        self.system: Callable[[], str] = platform.system
        self.linger_dir: Optional[Path] = LINGER_DIR
        self.docker_poll = 5.0
        self._job_lock = threading.Lock()
        self.boot_state: Dict[str, Any] = {"state": "not_started"}
        self.last_job: Optional[Dict[str, Any]] = None
        self.eng = engine_module()

    # ── settings ───────────────────────────────────────────────────────────

    def setting(self, name: str, default: str = "") -> str:
        v = self.env.get(name, "")
        if v:
            return v
        try:
            return self.eng.env_file_get(self.repo / ".env", name) or default
        except OSError:
            return default

    def managed(self) -> bool:
        return self.setting("MC_UPDATE_MANAGED") == "deploy"

    def max_starts(self) -> int:
        raw = self.setting("MC_UPDATE_MAX_STARTS_PER_DAY", str(DEFAULT_MAX_STARTS))
        try:
            n = int(raw)
        except ValueError:
            return DEFAULT_MAX_STARTS
        return n if n >= 0 else DEFAULT_MAX_STARTS

    @property
    def engine_path(self) -> Path:
        return self.repo / "scripts" / "mc_update.py"

    def _engine_cmd(self, *args: str) -> List[str]:
        return [self.python, str(self.engine_path), *args, "--repo", str(self.repo), "--json"]

    def job_env(self) -> Dict[str, str]:
        env = dict(self.env)
        env.pop("MC_UPDATE_LOCK_FD", None)
        env["MC_REPO_PATH"] = str(self.repo)
        env["MC_UPDATE_HOME"] = str(self.state)
        # launchd/systemd hand services a minimal PATH; docker lives here.
        parts = [p for p in env.get("PATH", "/usr/bin:/bin").split(":") if p]
        for extra in ("/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin"):
            if extra not in parts:
                parts.append(extra)
        env["PATH"] = ":".join(parts)
        return env

    def _clean(self, text: Any) -> Any:
        """No machine paths towards the caller (the backend shows it to users)."""
        if not isinstance(text, str):
            return text
        for p, short in ((str(self.state), "<update-state>"), (str(self.repo), "<install>"),
                         (str(Path.home()), "~")):
            if p and p != "/":
                text = text.replace(p, short)
        return text

    # ── state readers ──────────────────────────────────────────────────────

    def live_holder(self) -> Optional[Dict[str, Any]]:
        return self.eng.RunLock(self.state / "lock").live_holder()

    def open_run(self):
        return self.eng.find_open_run(self.state)

    def starts_last_24h(self) -> int:
        """Starts through the door in the last 24 h that count against the limit."""
        cutoff = _now() - _dt.timedelta(hours=24)
        n = 0
        for p in self.eng.list_runs(self.state):
            try:
                d = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(d, dict) or d.get("source") != "bridge":
                continue
            try:
                created = _dt.datetime.strptime(d.get("created_at", ""), "%Y-%m-%dT%H:%M:%SZ") \
                    .replace(tzinfo=_dt.timezone.utc)
            except ValueError:
                continue
            if created < cutoff:
                continue
            if d.get("phase") == "aborted" and d.get("failure_kind") == "network":
                continue   # a failed download is not a used attempt
            n += 1
        return n

    # ── bridge checks (added to the engine's preflight) ─────────────────────

    def bridge_checks(self) -> List[Dict[str, str]]:
        checks = [{"id": "bridge", "status": "ok",
                   "message": f"update door of the cli-bridge answers (api {API_VERSION})"}]
        if self.system() == "Linux":
            user = current_user()
            if self.linger_dir is not None:
                if (self.linger_dir / user).exists():
                    checks.append({"id": "linger", "status": "ok",
                                   "message": "the bridge and a running update survive logout and reboot (linger on)"})
                else:
                    checks.append({"id": "linger", "status": "fail",
                                   "message": ("without linger the bridge stops at logout and does not start after a "
                                               "reboot, so an interrupted update could not finish — run on the "
                                               f"machine: sudo loginctl enable-linger {user}")})
            if self._detach_method() != "systemd":
                checks.append({"id": "detach", "status": "warn",
                               "message": "systemd-run not found — the update runs in a separate session instead"})
        return checks

    # ── request handling ───────────────────────────────────────────────────

    def handle(self, method: str, raw_path: str, headers: Mapping[str, str],
               body: bytes) -> Tuple[int, Dict[str, Any]]:
        presented = headers.get(TOKEN_HEADER) if headers is not None else None
        if not self._token_ok(presented):
            return 401, {"error": "unauthorized"}
        path, _, query = raw_path.partition("?")
        route = ROUTES.get(path)
        if route is None:
            return 404, {"error": "unknown route"}
        want_method, allowed = route
        if method != want_method:
            return 405, {"error": f"{path} takes {want_method}"}
        if self.managed():
            return 409, {"error": "this install is updated through its deploy procedure (MC_UPDATE_MANAGED=deploy)",
                         "managed": True}
        if method == "GET":
            data, err = self._parse_query(query)
        else:
            data, err = self._parse_body(body, allowed)
        if err:
            return 400, {"error": err}
        try:
            if path == "/mc-update/preflight":
                return self.preflight(data.get("expected_target"))
            if path == "/mc-update/start":
                return self.start(data.get("expected_target"))
            if path == "/mc-update/status":
                return self.status(data.get("run"))
            if path == "/mc-update/rollback":
                return self.rollback(data.get("run_id"))
            return self.resume(data.get("run_id"))
        except self.eng.UpdateError as e:
            return 500, {"error": self._clean(str(e))}

    def _token_ok(self, presented: Optional[str]) -> bool:
        import hmac
        if not self.token or not presented:
            return False
        return hmac.compare_digest(presented.strip().encode("utf-8", "replace"),
                                   self.token.encode("utf-8", "replace"))

    def _parse_body(self, body: bytes, allowed: Tuple[str, ...]) -> Tuple[Dict[str, Any], Optional[str]]:
        if len(body or b"") > MAX_BODY:
            return {}, "body too large"
        if not body or not body.strip():
            data: Any = {}
        else:
            try:
                data = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return {}, "body is not JSON"
        if not isinstance(data, dict):
            return {}, "body must be a JSON object"
        unknown = sorted(set(data) - set(allowed))
        if unknown:
            return {}, f"unknown field(s): {', '.join(unknown)} — the engine chooses the target itself"
        for key in ("expected_target",):
            if key in data and not (isinstance(data[key], str) and self.eng.is_release_tag(data[key])):
                return {}, f"{key} must be a release tag vX.Y.Z"
        if "run_id" in data and not (isinstance(data["run_id"], str) and self.eng.RUN_ID_RE.match(data["run_id"])):
            return {}, "run_id is not a run id"
        return data, None

    def _parse_query(self, query: str) -> Tuple[Dict[str, Any], Optional[str]]:
        out: Dict[str, Any] = {}
        for part in filter(None, query.split("&")):
            key, _, value = part.partition("=")
            if key != "run":
                return {}, f"unknown parameter {key!r}"
            if not self.eng.RUN_ID_RE.match(value):
                return {}, "run is not a run id"
            out["run"] = value
        return out, None

    # ── engine, synchronous (read-only or instant) ─────────────────────────

    def _run_sync(self, *args: str) -> Tuple[int, Dict[str, Any], str]:
        try:
            cp = subprocess.run(self._engine_cmd(*args), cwd=str(self.repo), env=self.job_env(),
                                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                timeout=self.sync_timeout)
        except subprocess.TimeoutExpired:
            raise self.eng.UpdateError(f"the engine did not answer within {self.sync_timeout:.0f}s") from None
        except OSError as e:
            raise self.eng.UpdateError(f"the engine could not be started ({e})") from None
        return cp.returncode, _parse_json(cp.stdout), self._error_line(cp.stderr or cp.stdout)

    def _error_line(self, text: str) -> str:
        lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip() and not ln.strip().startswith(("{", "}", '"'))]
        msg = lines[-1] if lines else ""
        for prefix in ("✗ refused: ", "✗ "):
            if msg.startswith(prefix):
                msg = msg[len(prefix):]
        return self._clean(msg)

    def preflight(self, expected: Optional[str]) -> Tuple[int, Dict[str, Any]]:
        args = ["preflight"] + (["--expect", expected] if expected else [])
        rc, out, err = self._run_sync(*args)
        if rc == self.eng.EXIT_REFUSED:
            return 409, {"error": err or "refused"}
        if rc == self.eng.EXIT_USAGE:
            return 400, {"error": err or "bad request"}
        if rc not in (self.eng.EXIT_OK, self.eng.EXIT_ABORTED) or "checks" not in out:
            return 502, {"error": err or f"the engine's preflight failed (exit {rc})"}
        checks = [{k: self._clean(v) for k, v in c.items()} for c in out["checks"]] + self.bridge_checks()
        out["checks"] = checks
        out["ok"] = not any(c["status"] == "fail" for c in checks)
        out["api_version"] = API_VERSION
        out["starts"] = {"used": self.starts_last_24h(), "limit": self.max_starts()}
        return 200, out

    def status(self, run: Optional[str]) -> Tuple[int, Dict[str, Any]]:
        rc, out, err = self._run_sync("status", *(["--run", run] if run else []))
        if rc == self.eng.EXIT_USAGE:
            return 404, {"error": err or "no such run"}
        if rc != self.eng.EXIT_OK:
            return 502, {"error": err or f"status failed (exit {rc})"}
        out["bridge"] = {"api_version": API_VERSION, "boot": dict(self.boot_state),
                         "last_job": self._job_view(), "starts": {"used": self.starts_last_24h(),
                                                                  "limit": self.max_starts()}}
        return 200, out

    # ── detached jobs ──────────────────────────────────────────────────────

    def _detach_method(self) -> str:
        if self.detach:
            return self.detach
        if self.system() == "Linux" and shutil.which("systemd-run", path=self.job_env().get("PATH")):
            return "systemd"
        return "fork"

    def _jobs_dir(self) -> Path:
        d = self.state / "bridge"
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        return d

    @staticmethod
    def _prune_job_files(jobs: Path) -> None:
        """One .out/.exit pair per job: drop pairs older than two weeks (the
        journal keeps the run's history; these are only the raw job output)."""
        cutoff = time.time() - JOB_FILE_MAX_AGE
        for p in jobs.iterdir():
            if not p.name.endswith((".out", ".exit", ".exit.tmp")):
                continue
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass

    def launch(self, kind: str, key: str, engine_args: List[str]) -> Path:
        """Start the engine detached; return the path of its exit-code file."""
        jobs = self._jobs_dir()
        self._prune_job_files(jobs)
        out = jobs / f"{kind}-{key}.out"
        ex = jobs / f"{kind}-{key}.exit"
        for p in (out, ex, Path(str(ex) + ".tmp")):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        cmd = self._engine_cmd(*engine_args)
        env = self.job_env()
        method = self._detach_method()
        used = None
        if method == "systemd":
            unit = f"mc-update-{kind}-{key}"
            setenv = [f"--setenv={k}={v}" for k, v in sorted(env.items())
                      if k in _PASS_ENV or k.startswith(_PASS_PREFIXES)]
            argv = ["systemd-run", "--user", "--unit", unit, "--collect", "--quiet",
                    "-p", f"WorkingDirectory={self.repo}", *setenv,
                    "/bin/sh", "-c", JOB_SCRIPT, "mc-update-job", str(self.repo), str(out), str(ex), *cmd]
            try:
                cp = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                                    text=True, timeout=60)
                if cp.returncode == 0:
                    used = "systemd"
                else:
                    self.log(f"mc-update: systemd-run failed (exit {cp.returncode}: "
                             f"{(cp.stderr or '').strip()[:200]}) — falling back to a double fork")
            except (OSError, subprocess.TimeoutExpired) as e:
                self.log(f"mc-update: systemd-run not usable ({e}) — falling back to a double fork")
        if used is None:
            p = subprocess.Popen(["/bin/sh", "-c", FORK_SCRIPT, "mc-update-job", str(self.repo), str(out),
                                  str(ex), *cmd], cwd=str(self.repo), env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                                 start_new_session=self.new_session)
            p.wait(timeout=30)
            used = "fork"
        self.last_job = {"kind": kind, "key": key, "started_at": _iso(), "detach": used,
                         "exit_file": str(ex), "out_file": str(out)}
        self.log(f"mc-update: {kind} job {key} started ({used})")
        return ex

    def _job_view(self) -> Optional[Dict[str, Any]]:
        j = self.last_job
        if not j:
            return None
        return {"kind": j["kind"], "run_id": j.get("run_id"), "started_at": j["started_at"],
                "detach": j["detach"], "exit": self._read_exit(Path(j["exit_file"]))}

    @staticmethod
    def _read_exit(ex: Path) -> Optional[int]:
        try:
            return int(ex.read_text().strip())
        except (OSError, ValueError):
            return None

    def _await(self, ex: Path, ready: Callable[[], bool], ready_first: bool) -> Tuple[Optional[int], bool]:
        """Wait until the job is under way (ready) or already ended (exit code)."""
        deadline = time.monotonic() + self.start_wait
        while True:
            if ready_first and ready():
                return None, True
            rc = self._read_exit(ex)
            if rc is not None:
                return rc, False
            if not ready_first and ready():
                return None, True
            if time.monotonic() >= deadline:
                return None, False
            time.sleep(0.05)

    def _job_output(self, ex: Path) -> Tuple[Dict[str, Any], str]:
        out_file = ex.with_suffix(".out")
        try:
            text = out_file.read_text(errors="replace")
        except OSError:
            text = ""
        return _parse_json(text), self._error_line(text)

    # ── start / rollback / resume ──────────────────────────────────────────

    def _busy(self) -> Optional[Tuple[int, Dict[str, Any]]]:
        if self.live_holder() is not None:
            return 423, {"error": "an update run is active on this machine"}
        try:
            open_run = self.open_run()
        except self.eng.UpdateError as e:
            return 423, {"error": f"{self._clean(str(e))} — check the update journal on the machine"}
        if open_run is not None:
            return 423, {"error": f"run {open_run.run_id} was interrupted in phase {open_run.phase} — resume it first",
                         "run_id": open_run.run_id, "interrupted": True}
        return None

    def start(self, expected: Optional[str]) -> Tuple[int, Dict[str, Any]]:
        if not expected:
            return 400, {"error": "expected_target is required (the release the UI offered)"}
        if not self._job_lock.acquire(blocking=False):
            return 423, {"error": "another request is starting an update right now"}
        try:
            busy = self._busy()
            if busy:
                return busy
            # Defence in depth: a failing bridge check (Linux without linger —
            # a logout would kill the job mid-switch) refuses the start here,
            # whatever the caller made of preflight.ok.
            failed = [c for c in self.bridge_checks() if c["status"] == "fail"]
            if failed:
                return 422, {"error": "a check failed — nothing was changed", "checks": failed}
            used, limit = self.starts_last_24h(), self.max_starts()
            if used >= limit:
                return 429, {"error": f"{used} update starts in the last 24 hours — the limit is {limit} "
                                      "(MC_UPDATE_MAX_STARTS_PER_DAY)", "used": used, "limit": limit}
            run_id = self.eng.new_run_id()
            ex = self.launch("start", run_id, ["start", "--source", "bridge", "--expect", expected,
                                               "--run-id", run_id])
            self.last_job["run_id"] = run_id
            journal = self.state / "journal" / f"{run_id}.json"
            rc, ready = self._await(ex, journal.exists, ready_first=True)
            if ready:
                return 202, {"run_id": run_id, "state": "started"}
            if rc is None:
                return 202, {"run_id": run_id, "state": "starting"}
            out, err = self._job_output(ex)
            if rc == self.eng.EXIT_OK:
                return 200, {"started": False, "up_to_date": bool(out.get("up_to_date")),
                             "current": out.get("current")}
            if rc == self.eng.EXIT_ABORTED:
                checks = [{k: self._clean(v) for k, v in c.items()} for c in out.get("checks", [])]
                return 422, {"error": "a check failed — nothing was changed", "checks": checks}
            if rc == self.eng.EXIT_REFUSED:
                return 409, {"error": err or "refused"}
            if rc == self.eng.EXIT_LOCKED:
                return 423, {"error": err or "another update run holds the lock"}
            if rc == self.eng.EXIT_USAGE:
                return 400, {"error": err or "bad request"}
            return 500, {"error": err or f"the engine stopped with exit {rc}", "exit": rc}
        finally:
            self._job_lock.release()

    def rollback(self, run_id: Optional[str]) -> Tuple[int, Dict[str, Any]]:
        sel = ["--run", run_id] if run_id else []
        if self.live_holder() is not None:
            # A live run: before the switch this is a cancel, answered at once.
            rc, out, err = self._run_sync("rollback", *sel)
            return self._map_rollback(rc, out, err)
        if run_id and not (self.state / "journal" / f"{run_id}.json").exists():
            return 404, {"error": f"no run {run_id}"}
        if not self._job_lock.acquire(blocking=False):
            return 423, {"error": "another request is starting an update job right now"}
        try:
            if self.live_holder() is not None:
                return 423, {"error": "an update run is active on this machine"}
            key = (run_id or "latest") + "-" + _now().strftime("%H%M%S")
            ex = self.launch("rollback", key, ["rollback", *sel])
            self.last_job["run_id"] = run_id
            rc, ready = self._await(ex, lambda: self.live_holder() is not None, ready_first=False)
            if rc is None:
                return 202, {"run_id": run_id, "state": "rolling_back"}
            out, err = self._job_output(ex)
            return self._map_rollback(rc, out, err)
        finally:
            self._job_lock.release()

    def _map_rollback(self, rc: int, out: Dict[str, Any], err: str) -> Tuple[int, Dict[str, Any]]:
        e = self.eng
        if rc in (e.EXIT_OK, e.EXIT_ROLLED_BACK):
            return 200, {**{k: self._clean(v) for k, v in out.items()}, "exit": rc}
        if rc == e.EXIT_REFUSED:
            return 409, {"error": err or "refused"}
        if rc == e.EXIT_LOCKED:
            return 423, {"error": err or "too late to cancel"}
        if rc == e.EXIT_USAGE:
            return 404, {"error": err or "no such run"}
        return 500, {"error": err or f"the way back stopped with exit {rc}", "exit": rc}

    def resume(self, run_id: Optional[str]) -> Tuple[int, Dict[str, Any]]:
        if run_id and not (self.state / "journal" / f"{run_id}.json").exists():
            return 404, {"error": f"no run {run_id}"}
        if not self._job_lock.acquire(blocking=False):
            return 423, {"error": "another request is starting an update job right now"}
        try:
            return self._resume_locked(run_id)
        finally:
            self._job_lock.release()

    def _resume_locked(self, run_id: Optional[str]) -> Tuple[int, Dict[str, Any]]:
        if self.live_holder() is not None:
            return 423, {"error": "an update run is active on this machine"}
        try:
            open_run = self.eng.find_run(self.state, run_id) if run_id else self.open_run()
        except (self.eng.UpdateError, self.eng.UsageError) as e:
            return 404, {"error": self._clean(str(e))}
        if open_run is None or open_run.phase in self.eng.TERMINAL:
            return 200, {"resumed": False, "run_id": open_run.run_id if open_run else None,
                         "phase": open_run.phase if open_run else None}
        ex = self.launch("resume", open_run.run_id + "-" + _now().strftime("%H%M%S"),
                         ["resume", "--run", open_run.run_id])
        self.last_job["run_id"] = open_run.run_id
        rc, ready = self._await(ex, lambda: self.live_holder() is not None, ready_first=False)
        if rc is None:
            return 202, {"resumed": True, "run_id": open_run.run_id, "state": "resuming"}
        out, err = self._job_output(ex)
        if rc == self.eng.EXIT_LOCKED:
            return 423, {"error": err or "another update run holds the lock"}
        return 200, {"resumed": True, "run_id": open_run.run_id, "finished": True, "exit": rc,
                     "phase": out.get("phase")}

    # ── start routine ──────────────────────────────────────────────────────

    def docker_answers(self) -> bool:
        try:
            cp = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], cwd=str(self.repo),
                                env=self.job_env(), stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return cp.returncode == 0

    def wait_for_docker(self, limit: float) -> bool:
        deadline = time.monotonic() + limit
        while True:
            if self.docker_answers():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(self.docker_poll)

    def boot(self, docker_wait: float = 600.0, sweep_tries: int = 3, sweep_pause: float = 20.0) -> Dict[str, Any]:
        """The bridge's start routine (ADR-094 decision 6, resumption).

        Interrupted run + dead lock holder → wait for Docker (max.
        ``docker_wait``) → resume it, detached. No open run → lift a drain no
        run owns any more. Returns the state the status route shows."""
        self.boot_state = {"state": "checking", "at": _iso()}
        if self.managed():
            self.boot_state = {"state": "managed", "at": _iso()}
            return dict(self.boot_state)
        try:
            open_run = self.open_run()
        except self.eng.UpdateError as e:
            self.boot_state = {"state": "journal_unreadable", "error": self._clean(str(e)), "at": _iso()}
            self.log(f"mc-update boot: {e}")
            return dict(self.boot_state)
        if open_run is not None and self.live_holder() is not None:
            self.boot_state = {"state": "running", "run_id": open_run.run_id, "at": _iso()}
            return dict(self.boot_state)
        self.boot_state = {"state": "waiting_for_docker", "run_id": open_run.run_id if open_run else None,
                           "at": _iso()}
        if not self.wait_for_docker(docker_wait):
            self.boot_state = {"state": "docker_unavailable", "run_id": open_run.run_id if open_run else None,
                               "at": _iso()}
            self.log("mc-update boot: Docker did not answer — nothing resumed "
                     "(run ./install.sh --resume on the machine)" if open_run else
                     "mc-update boot: Docker did not answer — no drain sweep")
            return dict(self.boot_state)
        if open_run is not None:
            with self._job_lock:
                if self.live_holder() is not None:
                    self.boot_state = {"state": "running", "run_id": open_run.run_id, "at": _iso()}
                    return dict(self.boot_state)
                self.log(f"mc-update boot: run {open_run.run_id} was interrupted in phase {open_run.phase} — resuming")
                self.launch("resume", open_run.run_id + "-boot", ["resume", "--run", open_run.run_id])
                self.last_job["run_id"] = open_run.run_id
            self.boot_state = {"state": "resuming", "run_id": open_run.run_id, "phase": open_run.phase,
                               "at": _iso()}
            return dict(self.boot_state)
        # ADR-094 decision 7 speaks of lifting an *expired* drain recorded in
        # the history. Here no run is open and no live lock exists, so any
        # drain left behind has no owner that could ever lift it: the sweep
        # lifts it regardless of age (the history lands with PR 4c).
        sweep: Dict[str, Any] = {}
        for attempt in range(max(1, sweep_tries)):
            try:
                rc, out, err = self._run_sync("drain-sweep")
            except self.eng.UpdateError as e:
                rc, out, err = 1, {}, str(e)
            if rc == self.eng.EXIT_OK:
                sweep = {"lifted": out.get("lifted", []), "skipped": out.get("skipped")}
                if sweep["lifted"]:
                    self.log("mc-update boot: lifted a drain no run owned: "
                             + ", ".join(str(x.get("run_id")) for x in sweep["lifted"]))
                break
            sweep = {"error": err or f"exit {rc}"}
            if attempt + 1 < sweep_tries:
                time.sleep(sweep_pause)
        self.boot_state = {"state": "idle", "drain_sweep": sweep, "at": _iso()}
        return dict(self.boot_state)
