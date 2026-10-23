#!/usr/bin/env python3
"""Host-side Wake-on-LAN watcher for power-managed runtimes (e.g. PORSCHE).

The Mission Control backend runs in Docker and cannot send an L2 broadcast magic
packet. Instead, runtime_manager.wake_runtime() drops a trigger file into
~/.mc/wake-requests/<slug>.request.json with shape:

    {"slug": "...", "mac": "00:11:22:33:44:55", "ip": "192.0.2.20",
     "broadcast": "192.0.2.255", "requested_at": "<iso8601>"}

This watcher (driven by a launchd LaunchAgent on WatchPaths + StartInterval) is a
ONE-SHOT processor: it scans the dir once, fires the real wake script for each
pending request via fire-and-forget (--wait 0), and retires the request so it is
not reprocessed. It never crashes the caller — malformed files are logged and
skipped. Idempotent: re-running with no pending requests only beats.

Heartbeat (round-7 re-check, L5): every run touches <wake dir>/watcher.alive,
also when nothing waits, and again before each request. The backend treats the
helper as down when that file is missing or older than ~90 s
(runtime_manager.WAKE_HELPER_HEARTBEAT_S) and answers 503 instead of
"Waking…". The file is updated IN PLACE (utime) — a new directory entry would
fire launchd's WatchPaths again and loop the job.

Old requests are not woken: a request older than MAX_REQUEST_AGE_S (default
5 min) is retired with a status "expired" and no magic packet. A helper that
was down (or is installed fresh while an old request still lies there) must
not wake the box minutes or weeks after the click.

stdlib only. Logs to stdout (launchd redirects to ~/.mc/logs/).
"""
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# --- Config (overridable via env for testing) ---
HOME_HOST = os.environ.get("HOME_HOST", str(Path.home()))
WAKE_DIR = Path(os.environ.get("MC_WAKE_REQUEST_DIR", str(Path(HOME_HOST) / ".mc" / "wake-requests")))
WAKE_SCRIPT = Path(
    os.environ.get(
        "MC_WAKE_SCRIPT",
        str(Path(HOME_HOST) / ".claude" / "skills" / "wake-porsche" / "wake_porsche.py"),
    )
)

# PORSCHE fallbacks if a field is missing/blank in the request.
DEFAULT_MAC = os.environ.get("PORSCHE_MAC", "")
DEFAULT_IP = os.environ.get("PORSCHE_LAN_IP", "")
# Limited broadcast — funktioniert in jedem LAN ohne Subnetz-Annahme.
DEFAULT_BROADCAST = os.environ.get("PORSCHE_BROADCAST", "255.255.255.255")

# Sign of life for the backend — must match runtime_manager.WAKE_HELPER_HEARTBEAT_NAME.
HEARTBEAT_NAME = "watcher.alive"
HEARTBEAT = WAKE_DIR / HEARTBEAT_NAME

# A request older than this is retired without waking. Above the backend's
# WAKE_HELPER_STALE_S (120 s): a request the backend still counts as "on its
# way" is always woken.
MAX_REQUEST_AGE_S = int(os.environ.get("MC_WAKE_REQUEST_MAX_AGE_S", "300"))


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def log(msg: str) -> None:
    print(f"[{_now_iso()}] porsche-wake-watcher: {msg}", flush=True)


def _write_status(req_path: Path, slug: str, ok: bool, detail: str) -> None:
    """Best-effort observability file next to the request."""
    status_path = req_path.with_name(f"{req_path.name[:-len('.request.json')]}.status.json")
    try:
        status_path.write_text(
            json.dumps(
                {"slug": slug, "ok": ok, "ran_at": _now_iso(), "detail": detail},
                indent=2,
            )
        )
    except OSError as exc:
        log(f"  ! could not write status {status_path.name}: {exc}")


def _retire(req_path: Path) -> None:
    """Move <slug>.request.json -> <slug>.done.json so it is not reprocessed."""
    base = req_path.name[: -len(".request.json")]
    done_path = req_path.with_name(f"{base}.done.json")
    try:
        # os.replace is atomic and overwrites an existing stale .done.json.
        os.replace(req_path, done_path)
    except OSError as exc:
        # If we cannot move it, delete it so it is not reprocessed forever.
        log(f"  ! could not move to {done_path.name}: {exc} — deleting request instead")
        try:
            req_path.unlink()
        except OSError as exc2:
            log(f"  !! could not delete {req_path.name}: {exc2}")


def _touch(path: Path) -> None:
    """Update the heartbeat's mtime in place; create it only when missing."""
    try:
        os.utime(path, None)
    except FileNotFoundError:
        path.write_text(json.dumps({"pid": os.getpid(), "since": _now_iso()}) + "\n")


def beat() -> None:
    """Tell the backend this helper runs. Never fails the run."""
    try:
        _touch(HEARTBEAT)
    except OSError as exc:
        log(f"  ! could not write heartbeat {HEARTBEAT}: {exc}")


def _request_age_s(req_path: Path) -> float | None:
    try:
        return time.time() - req_path.stat().st_mtime
    except OSError:
        return None


def process_request(req_path: Path) -> bool:
    """Process a single *.request.json. Returns True if the wake was fired."""
    slug = req_path.name[: -len(".request.json")]
    age = _request_age_s(req_path)
    if age is not None and age > MAX_REQUEST_AGE_S:
        detail = f"expired: waited {int(age)} s (limit {MAX_REQUEST_AGE_S} s) — not woken"
        log(f"  request {req_path.name} {detail}")
        _write_status(req_path, slug, ok=False, detail=detail)
        _retire(req_path)
        return False
    try:
        data = json.loads(req_path.read_text())
    except (OSError, ValueError) as exc:
        log(f"  malformed/unreadable request {req_path.name}: {exc} — skipping & retiring")
        _write_status(req_path, slug, ok=False, detail=f"malformed: {exc}")
        _retire(req_path)
        return False

    if not isinstance(data, dict):
        log(f"  request {req_path.name} is not a JSON object — skipping & retiring")
        _write_status(req_path, slug, ok=False, detail="not a JSON object")
        _retire(req_path)
        return False

    slug = str(data.get("slug") or slug)
    mac = str(data.get("mac") or DEFAULT_MAC)
    ip = str(data.get("ip") or DEFAULT_IP)
    broadcast = str(data.get("broadcast") or DEFAULT_BROADCAST)

    cmd = [
        sys.executable,
        str(WAKE_SCRIPT),
        "--mac", mac,
        "--ip", ip,
        "--broadcast", broadcast,
        "--wait", "0",  # fire-and-forget: the magic packet is what matters
    ]
    log(f"  waking '{slug}' (mac={mac} ip={ip} bcast={broadcast})")

    ok = False
    detail = ""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if out:
            log(f"    stdout: {out}")
        if err:
            log(f"    stderr: {err}")
        ok = proc.returncode == 0
        detail = f"exit={proc.returncode}"
    except FileNotFoundError as exc:
        detail = f"wake script not found: {exc}"
        log(f"    ! {detail}")
    except subprocess.TimeoutExpired:
        detail = "wake script timed out (>60s)"
        log(f"    ! {detail}")
    except Exception as exc:  # never crash the loop
        detail = f"unexpected error: {exc}"
        log(f"    ! {detail}")

    _write_status(req_path, slug, ok=ok, detail=detail)
    _retire(req_path)
    return ok


def main() -> int:
    if not WAKE_DIR.is_dir():
        # Created by the host user, so this helper can always retire requests
        # (a folder the root container created may not be writable for it).
        try:
            WAKE_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log(f"wake dir {WAKE_DIR} missing and not creatable: {exc}")
            return 0
    beat()

    requests = sorted(WAKE_DIR.glob("*.request.json"))
    if not requests:
        # Quiet no-op on the safety-net interval; keep it terse.
        return 0

    log(f"found {len(requests)} pending request(s)")
    for req_path in requests:
        # One wake script may take up to 60 s: beat before each, so a long
        # queue never reads as a dead helper.
        beat()
        try:
            process_request(req_path)
        except Exception as exc:  # belt-and-suspenders: one bad file never kills the run
            log(f"  !! unhandled error on {req_path.name}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
