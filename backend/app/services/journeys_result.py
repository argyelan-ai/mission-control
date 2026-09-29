"""Last night's journey-test result for the Home line (docs/journeys.md).

The nightly runner (e2e/run-journeys.sh) writes ``~/.mc/journeys/last.json``
on the host; the backend sees the same file through the 1:1 ``~/.mc`` mount,
like the night-shift report. This module turns it into a small summary for
Home: status, counts, which journeys failed. Nothing path-like leaves here —
the runner's summary and report folder are host paths, so only a short
reason (text before " — ") is passed on for skipped / error runs.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from app.services.fs_roots import mc_home

STATUSES = ("green", "red", "skipped", "error")
_FAILED = ("failed", "timedOut", "interrupted")


def _journey_id(file: str | None) -> str | None:
    """``J-usage.spec.ts`` → ``J-usage`` (the id in landkarte.yaml)."""
    if not file:
        return None
    name = file.rsplit("/", 1)[-1]
    return name.split(".spec.", 1)[0] or None


def _utc(stamp: str | None) -> str | None:
    if not stamp:
        return None
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S%z").astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


def load() -> dict | None:
    """The summary, or None when no run has written a readable result yet."""
    path = mc_home() / "journeys" / "last.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("status") not in STATUSES:
        return None

    journeys = [j for j in data.get("journeys") or [] if isinstance(j, dict)]
    failed: list[str] = []
    for j in journeys:
        jid = _journey_id(j.get("file"))
        if j.get("status") in _FAILED and jid and jid not in failed:
            failed.append(jid)

    reason = None
    if data["status"] in ("skipped", "error"):
        reason = str(data.get("summary") or "").split(" — ", 1)[0].strip()[:120] or None

    return {
        "status": data["status"],
        "finished_at": _utc(data.get("finished_at")),
        "commit": data.get("commit"),
        "total": len(journeys),
        "passed": sum(1 for j in journeys if j.get("status") == "passed"),
        "failed": failed,
        "known_gaps": len(data.get("known_gaps") or []),
        "reason": reason,
    }
