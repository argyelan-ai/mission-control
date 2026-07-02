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
skipped. Idempotent: re-running with no pending requests is a no-op.

stdlib only. Logs to stdout (launchd redirects to ~/.mc/logs/).
"""
import datetime as dt
import json
import os
import subprocess
import sys
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


def process_request(req_path: Path) -> bool:
    """Process a single *.request.json. Returns True if the wake was fired."""
    slug = req_path.name[: -len(".request.json")]
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
        # Nothing to do — dir not created yet. Not an error.
        log(f"wake dir {WAKE_DIR} does not exist — nothing to do")
        return 0

    requests = sorted(WAKE_DIR.glob("*.request.json"))
    if not requests:
        # Quiet no-op on the safety-net interval; keep it terse.
        return 0

    log(f"found {len(requests)} pending request(s)")
    for req_path in requests:
        try:
            process_request(req_path)
        except Exception as exc:  # belt-and-suspenders: one bad file never kills the run
            log(f"  !! unhandled error on {req_path.name}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
