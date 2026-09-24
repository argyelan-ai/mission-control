"""Night shift — the pure rules (ROADMAP E2, "Nachtschicht").

No I/O here: the time window, which marked task starts next, when a running
head counts as blocked, and the morning report text. The job that applies
them is ``night_shift.py``; the marks live in files (``night_store.py``).

Rules:
- **Window.** ``start``–``end`` in the operator's zone; ``end <= start`` means
  the window crosses midnight (default 22:00–06:00). A night is named after
  the local date it starts on.
- **One after another.** At most one start per tick, in marking order. Every
  GPU box is one lane (a duo recipe holds all its boxes); cloud pairs share
  one extra lane. A lane is busy while any head — day or night — holds it.
- **Local first.** A cloud pair starts only when the operator picked one, and
  only while the cloud starts of this night stay within ``cloud_share`` percent
  of the night's marked tasks (floor — one cloud task among three at 30 % does
  not start; 100 % switches the rule off).
- **Blocked is reported, never repaired.** A head waiting on a question, or
  without a sign of life for 15 minutes, is reported once. Nothing restarts it.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CLOUD_LANE = "cloud"
#: A running head without heartbeat or output for this long is "blocked".
BLOCKED_SILENT_S = 15 * 60

#: Start errors that will not heal by waiting — the mark is skipped for the
#: rest of the night (and reported). Everything else is retried next tick.
PERMANENT_START_ERRORS = frozenset({
    "pair_blocked", "repo_required", "task_not_found", "head_active", "pair_gone",
})

_HHMM = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")


# ── Config ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class NightConfig:
    enabled: bool = False
    start: str = "22:00"
    end: str = "06:00"
    timezone: str = "UTC"
    cloud_share: int = 30

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def parse_hhmm(value: str) -> dtime:
    m = _HHMM.match(value or "")
    if not m:
        raise ValueError("time must be HH:MM (00:00–23:59)")
    return dtime(int(m.group(1)), int(m.group(2)))


def validate_config(cfg: NightConfig) -> list[str]:
    """Field codes that are invalid (empty list = valid)."""
    bad: list[str] = []
    for name in ("start", "end"):
        try:
            parse_hhmm(getattr(cfg, name))
        except ValueError:
            bad.append(name)
    if not bad and parse_hhmm(cfg.start) == parse_hhmm(cfg.end):
        bad.append("end")  # an empty (or 24 h) window is not a night
    try:
        ZoneInfo(cfg.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        bad.append("timezone")
    if not (0 <= int(cfg.cloud_share) <= 100):
        bad.append("cloud_share")
    return bad


# ── Window ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Window:
    night: str  # local date the window starts on, "YYYY-MM-DD"
    starts_at: datetime  # aware, UTC
    ends_at: datetime  # aware, UTC

    def to_dict(self) -> dict:
        return {
            "night": self.night,
            "starts_at": self.starts_at.isoformat(),
            "ends_at": self.ends_at.isoformat(),
        }


def _local(day: date, t: dtime, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, t, tzinfo=zone).astimezone(UTC)


def window_of_night(night: str, cfg: NightConfig) -> Window:
    """The window of the night that starts on local date ``night``."""
    zone = cfg.zone
    start, end = parse_hhmm(cfg.start), parse_hhmm(cfg.end)
    day = date.fromisoformat(night)
    end_day = day + timedelta(days=1) if end <= start else day
    return Window(night, _local(day, start, zone), _local(end_day, end, zone))


def current_window(now: datetime, cfg: NightConfig) -> Window | None:
    """The window ``now`` lies in (start inclusive, end exclusive), else None."""
    local = now.astimezone(cfg.zone)
    for day in (local.date() - timedelta(days=1), local.date()):
        w = window_of_night(day.isoformat(), cfg)
        if w.starts_at <= now < w.ends_at:
            return w
    return None


def next_window(now: datetime, cfg: NightConfig) -> Window:
    """The current window, or the next one to come."""
    cur = current_window(now, cfg)
    if cur is not None:
        return cur
    local = now.astimezone(cfg.zone)
    for offset in (0, 1):
        w = window_of_night((local.date() + timedelta(days=offset)).isoformat(), cfg)
        if w.starts_at > now:
            return w
    return window_of_night((local.date() + timedelta(days=2)).isoformat(), cfg)  # pragma: no cover


# ── Picking the next start ──────────────────────────────────────────────────


@dataclass
class Candidate:
    """One queued mark as the picker sees it."""

    task_id: str
    locality: str  # local | cloud
    lanes: list[str]  # box keys, or [CLOUD_LANE]
    order: float  # marking time — earlier first


@dataclass
class Pick:
    #: startable now, in the order to try (the job starts the first that works)
    ready: list[str] = field(default_factory=list)
    #: task_id → why it waits this tick (lane_busy, cloud_share)
    waiting: dict[str, str] = field(default_factory=dict)

    @property
    def task_id(self) -> str | None:
        return self.ready[0] if self.ready else None


def cloud_allowed(night_total: int, cloud_started: int, share: int) -> bool:
    """May one more cloud start happen tonight? floor(total × share %)."""
    if share >= 100:
        return True
    return cloud_started + 1 <= math.floor(night_total * share / 100)


def pick_next(
    queue: list[Candidate],
    busy_lanes: set[str],
    *,
    night_total: int,
    cloud_started: int,
    cloud_share: int,
) -> Pick:
    """Queued marks (marking order) whose lanes are all free and which the
    cloud rule lets through. A later mark on a free lane may overtake an
    earlier mark whose lane is busy — each box stays one-at-a-time."""
    out = Pick()
    for c in sorted(queue, key=lambda c: (c.order, c.task_id)):
        if any(lane in busy_lanes for lane in c.lanes):
            out.waiting[c.task_id] = "lane_busy"
            continue
        if c.locality == "cloud" and not cloud_allowed(night_total, cloud_started, cloud_share):
            out.waiting[c.task_id] = "cloud_share"
            continue
        out.ready.append(c.task_id)
    return out


# ── Blocked detection ──────────────────────────────────────────────────────


def blocked_kind(state: str, *, silent_s: int | None, heartbeat_age_s: float | None) -> str | None:
    """``needs_you`` (waits on a question), ``silent`` (no heartbeat / no output
    for 15 min while it should be running) or None."""
    if state == "needs_you":
        return "needs_you"
    if state in ("starting", "running"):
        if heartbeat_age_s is not None and heartbeat_age_s >= BLOCKED_SILENT_S:
            return "silent"
        if silent_s is not None and silent_s >= BLOCKED_SILENT_S:
            return "silent"
    return None


# ── Morning report ─────────────────────────────────────────────────────────

#: Report order and headings. "blocked" = started but stuck, or never started.
CATEGORIES = ("passed", "failed", "needs_you", "blocked", "running")


def categorize(entry: dict) -> str:
    """Category of one night entry: ``{state, reason, started}`` → CATEGORIES."""
    if not entry.get("started"):
        return "blocked"
    state = entry.get("state")
    if state == "passed":
        return "passed"
    if state == "needs_you":
        return "needs_you"
    if state in ("failed", "stopped"):
        return "failed"
    if state in ("starting", "running"):
        return "blocked" if entry.get("blocked") else "running"
    return "failed"


_TEXT = {
    "en": {
        "title": "Night shift {night}: {counts}",
        "none": "Night shift {night}: nothing was marked for tonight.",
        "passed": "Passed",
        "failed": "Failed",
        "needs_you": "Needs you",
        "blocked": "Blocked",
        "running": "Still running",
        "not_started": "not started",
        "pr": "PR",
        "notice_needs_you": "Night shift: head needs you — {title}",
        "notice_silent": "Night shift: head silent for {minutes} min — {title}",
    },
    "de": {
        "title": "Nachtschicht {night}: {counts}",
        "none": "Nachtschicht {night}: für heute Nacht war nichts vorgemerkt.",
        "passed": "Bestanden",
        "failed": "Fehlgeschlagen",
        "needs_you": "Braucht dich",
        "blocked": "Blockiert",
        "running": "Läuft noch",
        "not_started": "nicht gestartet",
        "pr": "PR",
        "notice_needs_you": "Nachtschicht: Head braucht dich — {title}",
        "notice_silent": "Nachtschicht: Head seit {minutes} min still — {title}",
    },
}


def texts(lang: str | None) -> dict[str, str]:
    return _TEXT.get((lang or "en").lower()[:2], _TEXT["en"])


def task_link(base_url: str, task_id: str) -> str:
    return f"{base_url.rstrip('/')}/tasks?task={task_id}"


def format_report(night: str, entries: list[dict], *, base_url: str, lang: str | None = "en") -> str:
    """One message: counts per category, then one line per task with links.

    ``entries``: ``{task_id, title, started, state, reason, blocked, pr_url}``.
    Reason codes stay codes (``engine_not_ready``) — short, and searchable.
    """
    tx = texts(lang)
    if not entries:
        return tx["none"].format(night=night)
    groups: dict[str, list[dict]] = {c: [] for c in CATEGORIES}
    for e in entries:
        groups[categorize(e)].append(e)
    counts = " · ".join(f"{len(groups[c])} {tx[c].lower()}" for c in CATEGORIES if groups[c] or c != "running")
    lines = [tx["title"].format(night=night, counts=counts)]
    for cat in CATEGORIES:
        if not groups[cat]:
            continue
        lines.append("")
        lines.append(f"{tx[cat]} ({len(groups[cat])})")
        for e in groups[cat]:
            title = (e.get("title") or "?").strip().replace("\n", " ")[:80]
            parts = [f"- {title}"]
            detail = []
            if not e.get("started"):
                detail.append(tx["not_started"])
            reason = e.get("reason") or e.get("blocked")
            if reason and cat != "passed":
                detail.append(str(reason))
            if detail:
                parts.append(f"({', '.join(detail)})")
            parts.append(task_link(base_url, e["task_id"]))
            if e.get("pr_url"):
                parts.append(f"{tx['pr']}: {e['pr_url']}")
            lines.append(" ".join(parts))
    return "\n".join(lines)


def format_notice(kind: str, *, title: str, task_id: str, base_url: str, silent_s: int | None,
                  lang: str | None = "en") -> str:
    tx = texts(lang)
    title = (title or "?").strip().replace("\n", " ")[:80]
    if kind == "needs_you":
        head = tx["notice_needs_you"].format(title=title)
    else:
        head = tx["notice_silent"].format(title=title, minutes=max(15, (silent_s or 0) // 60))
    return f"{head}\n{task_link(base_url, task_id)}"
