"""derive_head_state — one pure function from file evidence to a head state
(docs/specs/head-launcher.md §6.5).

States: starting · running · needs_you · passed · failed · stopped.
"passed" is never claimed by the head alone: the PR URL comes from the
wrapper-written status.json, and the run record must be valid (files.py).
"""
from __future__ import annotations

from typing import Any

HEARTBEAT_FRESH_S = 90
# The backend cannot see host pids. A heartbeat older than this means the
# supervisor is gone (it touches the file every 30 s while the head lives).
VANISHED_S = 300
# Spooled but never picked up by the host watcher (not installed / not loaded).
NOT_PICKED_UP_S = 300
SILENT_WARN_S = 15 * 60

FINAL_STATES = frozenset({"needs_you", "passed", "failed", "stopped"})
ACTIVE_STATES = frozenset({"starting", "running"})


def derive_head_state(
    *,
    status: dict | None,
    created_ts: float,
    heartbeat_mtime: float | None,
    last_output_ts: float | None,
    run_record_passed: bool,
    question_exists: bool,
    stop_requested: bool,
    now: float,
    pid_alive: bool | None = None,
) -> dict[str, Any]:
    status = status or {}
    phase = status.get("phase")
    out: dict[str, Any] = {"state": "starting", "reason": None, "silent_s": None, "heartbeat_stale": False}

    if phase is None:
        if stop_requested:
            out.update(state="stopped", reason="stopped")
        elif now - created_ts > NOT_PICKED_UP_S:
            out.update(state="failed", reason="not_picked_up")
        return out

    if phase == "starting":
        return out

    if phase == "running":
        hb_age = None if heartbeat_mtime is None else now - heartbeat_mtime
        if last_output_ts is not None:
            out["silent_s"] = max(0, int(now - last_output_ts))
        if hb_age is not None and hb_age < HEARTBEAT_FRESH_S:
            out["state"] = "running"
            return out
        if pid_alive is True:
            out.update(state="running", heartbeat_stale=True)
            return out
        if pid_alive is False or hb_age is None or hb_age >= VANISHED_S:
            out.update(state="failed", reason="process_vanished", silent_s=None)
            return out
        out.update(state="running", heartbeat_stale=True)
        return out

    # phase == "exited" (or anything unknown — treat as exited)
    reason = status.get("reason")
    pr_url = status.get("pr_url")
    # Only the wrapper's own reason counts once the run has exited: a stop
    # request that arrives after a successful end must not turn "passed"
    # into "stopped" (stop_requested only matters before pick-up, above).
    if reason == "stopped":
        out.update(state="stopped", reason="stopped")
    elif question_exists and not pr_url:
        out.update(state="needs_you", reason=None)
    elif pr_url and run_record_passed:
        out.update(state="passed", reason=None)
    else:
        if not reason:
            reason = "no_pr" if not pr_url else "run_record_missing"
        out.update(state="failed", reason=reason)
    return out


def derive_for_run(run, now: float, pid_alive: bool | None = None) -> dict[str, Any]:
    """Convenience wrapper over a files.HeadRun."""
    from app.services.heads.files import parse_ts

    return derive_head_state(
        status=run.status,
        created_ts=run.created_ts,
        heartbeat_mtime=run.heartbeat_mtime,
        last_output_ts=parse_ts(run.status.get("last_output_at")) or run.log_mtime,
        run_record_passed=run.run_record_passed,
        question_exists=bool(run.status.get("question")) or run.question is not None,
        stop_requested=run.stop_requested,
        now=now,
        pid_alive=pid_alive,
    )
