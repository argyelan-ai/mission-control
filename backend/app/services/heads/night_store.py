"""Night shift — where marks, reports and the settings live.

Data model (why files, not a table):
- **Marks** are one JSON file per task: ``<heads_root>/night/marks/<task_id>.json``.
  The task core is frozen (PRINCIPLES §8.4: no new task columns), the head
  launcher keeps all run state in files beside it (spec §6.1, no migration),
  and ROADMAP E3 lists "a jobs table without FK to tasks" only as a later,
  measured gap. A file per task needs no migration and no foreign key, so
  ``delete_task`` stays untouched: a mark whose task is gone is dropped by the
  next tick (and hidden from the list before that).
- **Reports** are ``<heads_root>/night/reports/<night>.json``: claimed with
  O_EXCL before sending (``state: sending``), then filled with what was sent
  (``sent`` / ``undelivered`` + attempts). At most one report per night, and
  at least one: a claim left behind by a crash is taken over after
  ``REPORT_ORPHAN_S``, an undelivered report is sent again (stored text) up to
  ``REPORT_MAX_ATTEMPTS`` times. With the channels switched off (the
  default, ``send_to_channels``) a report is only ``stored``: MC shows it
  on Home and nothing is ever sent. A dismissed report gets a marker file
  ``<heads_root>/night/dismissed/<night>`` (the worker never writes it, so
  a retry cannot undo a dismiss).
- **Who writes a mark.** The API (mark, change pair, unmark) and the worker
  (start) hold a per-task lock (``mark_lock``) and re-read the file inside it.
  Every other worker write goes through ``update_mark_fields``: it re-reads
  the file, never re-creates a removed mark and only touches the fields the
  worker owns, so an operator's change during a tick is never overwritten.
- **Settings** are six ``app_settings`` rows (``night_shift_*``) with env
  defaults in ``config.py``. They are read from the DB on every tick: the job
  runs in mc-worker, the Settings page saves through the API process.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.app_setting import AppSetting
from app.services.heads import paths
from app.services.heads.night import NightConfig, validate_config

# ── Settings ────────────────────────────────────────────────────────────────

#: app_settings key → NightConfig field and type.
SETTING_FIELDS: dict[str, tuple[str, type]] = {
    "night_shift_enabled": ("enabled", bool),
    "night_shift_start": ("start", str),
    "night_shift_end": ("end", str),
    "night_shift_timezone": ("timezone", str),
    "night_shift_cloud_share": ("cloud_share", int),
    "night_shift_send_to_channels": ("send_to_channels", bool),
}


def env_defaults() -> NightConfig:
    return NightConfig(
        enabled=bool(settings.night_shift_enabled),
        start=settings.night_shift_start,
        end=settings.night_shift_end,
        timezone=settings.night_shift_timezone,
        cloud_share=int(settings.night_shift_cloud_share),
        send_to_channels=bool(settings.night_shift_send_to_channels),
    )


def _coerce(kind: type, raw: str):
    if kind is bool:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if kind is int:
        return int(raw)
    return raw


async def load_config(session: AsyncSession) -> NightConfig:
    """Env defaults overlaid with the operator's saved rows. A broken row
    falls back to the default for that field; an invalid combination falls
    back to the defaults as a whole (never a half-valid window)."""
    values = asdict(env_defaults())
    rows = (await session.exec(select(AppSetting).where(AppSetting.key.in_(list(SETTING_FIELDS))))).all()
    for row in rows:
        name, kind = SETTING_FIELDS[row.key]
        try:
            values[name] = _coerce(kind, row.value)
        except (TypeError, ValueError):
            continue
    cfg = NightConfig(**values)
    return cfg if not validate_config(cfg) else env_defaults()


async def save_config(session: AsyncSession, updates: dict) -> NightConfig:
    """Validate the merged result, then upsert. Raises ValueError(list of bad
    field names) — nothing is written in that case."""
    current = asdict(await load_config(session))
    by_name = {name: key for key, (name, _kind) in SETTING_FIELDS.items()}
    unknown = sorted(set(updates) - set(by_name))
    if unknown:
        raise ValueError(unknown)
    merged = NightConfig(**{**current, **updates})
    bad = validate_config(merged)
    if bad:
        raise ValueError(bad)
    for name, value in updates.items():
        key = by_name[name]
        raw = ("true" if value else "false") if isinstance(value, bool) else str(value)
        row = (await session.exec(select(AppSetting).where(AppSetting.key == key))).one_or_none()
        if row is None:
            session.add(AppSetting(key=key, value=raw))
        else:
            row.value = raw
            session.add(row)
    await session.commit()
    return merged


#: hold_reason a mark puts on an inbox card so nothing else picks it up
#: before the night (released again when the mark is removed).
HOLD_REASON = "night shift"

# ── Marks ───────────────────────────────────────────────────────────────────


@dataclass
class NightMark:
    task_id: str
    harness: str
    runtime_slug: str
    locality: str  # local | cloud, as the pair was when marked
    marked_at: float
    marked_by: str | None = None
    #: the night this mark belongs to once a window saw it (None = not yet)
    night: str | None = None
    run_id: str | None = None
    started_at: float | None = None
    #: last reason it could not start (engine_not_ready, lane_busy, …)
    last_error: str | None = None
    #: a permanent start error — no more tries this night
    gave_up: str | None = None
    #: the hold this mark put on an inbox card (released again on unmark)
    held: bool = False
    #: kinds already reported for the run ("needs_you", "silent")
    notified: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> NightMark:
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


def night_dir() -> Path:
    return paths.heads_root() / "night"


def marks_dir() -> Path:
    return night_dir() / "marks"


def reports_dir() -> Path:
    return night_dir() / "reports"


def _mark_path(task_id: str) -> Path:
    return marks_dir() / f"{uuid.UUID(str(task_id))}.json"


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(path)


def load_mark(task_id: str) -> NightMark | None:
    try:
        data = json.loads(_mark_path(task_id).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("task_id") != str(uuid.UUID(str(task_id))):
        return None
    try:
        return NightMark.from_dict(data)
    except TypeError:
        return None


def list_marks() -> list[NightMark]:
    folder = marks_dir()
    if not folder.is_dir():
        return []
    out = []
    for path in folder.glob("*.json"):
        mark = load_mark(path.stem) if _is_uuid(path.stem) else None
        if mark is not None:
            out.append(mark)
    out.sort(key=lambda m: (m.marked_at, m.task_id))
    return out


def save_mark(mark: NightMark) -> None:
    _atomic_write(_mark_path(mark.task_id), asdict(mark))


#: Fields the worker owns — everything else belongs to the operator (API).
WORKER_FIELDS = frozenset({"night", "run_id", "started_at", "last_error", "gave_up", "notified"})

#: A mark lock older than this belongs to a crashed process and is taken over.
MARK_LOCK_STALE_S = 60


class MarkBusy(Exception):
    """Another process changes this mark right now."""


def _lock_path(task_id: str) -> Path:
    return night_dir() / "locks" / str(uuid.UUID(str(task_id)))


def try_lock(task_id: str) -> Path | None:
    """Take the mark lock of ``task_id`` (O_EXCL marker) or return None."""
    marker = _lock_path(task_id)
    marker.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            os.close(os.open(str(marker), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            return marker
        except FileExistsError:
            try:
                age = time.time() - marker.stat().st_mtime
            except OSError:
                continue
            if age < MARK_LOCK_STALE_S:
                return None
            marker.unlink(missing_ok=True)
    return None


def release_lock(marker: Path) -> None:
    marker.unlink(missing_ok=True)


@contextlib.asynccontextmanager
async def mark_lock(task_id: str, timeout: float = 5.0) -> AsyncIterator[None]:
    """Hold the mark lock of ``task_id``; waits up to ``timeout`` s, then MarkBusy."""
    deadline = time.monotonic() + timeout
    while True:
        marker = try_lock(task_id)
        if marker is not None:
            break
        if time.monotonic() >= deadline:
            raise MarkBusy(task_id)
        await asyncio.sleep(0.05)
    try:
        yield
    finally:
        release_lock(marker)


def update_mark_fields(task_id: str, *, locked: bool = False, **changes) -> NightMark | None:
    """The worker's write: re-read the mark, change only ``changes`` (worker
    fields), save. Returns None — and writes nothing — when the mark is gone
    (removed by the operator) or the operator holds its lock right now (the
    worker notes it again next tick). ``locked=True``: the caller holds it."""
    bad = set(changes) - WORKER_FIELDS
    if bad:
        raise ValueError(sorted(bad))
    marker = None
    if not locked:
        marker = try_lock(task_id)
        if marker is None:
            return None
    try:
        mark = load_mark(task_id)
        if mark is None:
            return None
        for name, value in changes.items():
            setattr(mark, name, value)
        save_mark(mark)
        return mark
    finally:
        if marker is not None:
            release_lock(marker)


def delete_mark(task_id: str) -> None:
    _mark_path(task_id).unlink(missing_ok=True)


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


# ── Reports ─────────────────────────────────────────────────────────────────


#: A claim ("sending") older than this was left by a crash: send again.
REPORT_ORPHAN_S = 10 * 60
#: Wait this long before sending an undelivered report again …
REPORT_RETRY_S = 5 * 60
#: … and give up after this many attempts (the report stays in the UI).
REPORT_MAX_ATTEMPTS = 3


def _report_path(night: str) -> Path:
    return reports_dir() / f"{night}.json"


def claim_report(night: str, now: float | None = None) -> dict | None:
    """Claim the morning report of ``night`` for one send attempt.

    Returns the stored record (``attempts`` so far, ``text``/``entries`` of an
    earlier attempt, if any) or None when it must not be sent now: already
    sent, a fresh claim of another pass, too early for a retry, or given up.
    """
    now = time.time() if now is None else now
    folder = reports_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = _report_path(night)
    record: dict = {"night": night, "state": "sending", "attempts": 0, "at": now}
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        data = load_report(night)
        if data is None:  # empty: a crash right after the O_EXCL create
            try:
                at = path.stat().st_mtime
            except OSError:
                return None
            data = {"night": night, "state": "sending", "attempts": 0, "at": at}
        state = data.get("state")
        age = now - float(data.get("at") or 0)
        attempts = int(data.get("attempts") or 0)
        if state == "sending" and age >= REPORT_ORPHAN_S:
            pass  # orphaned claim
        elif state == "undelivered" and attempts < REPORT_MAX_ATTEMPTS and age >= REPORT_RETRY_S:
            pass  # retry
        else:
            return None
        record = {**data, "state": "sending", "at": now}
        _atomic_write(path, record)
        return record
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(record))
    return record


def reserve_report(night: str, now: float | None = None) -> bool:
    """Claim the morning report of ``night``. False = not to be sent now."""
    return claim_report(night, now) is not None


def release_report(night: str) -> None:
    _report_path(night).unlink(missing_ok=True)


def write_report(night: str, data: dict) -> None:
    _atomic_write(_report_path(night), data)


def report_nights() -> list[str]:
    folder = reports_dir()
    if not folder.is_dir():
        return []
    return sorted(p.stem for p in folder.glob("*.json"))


def load_report(night: str) -> dict | None:
    try:
        data = json.loads((reports_dir() / f"{night}.json").read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _dismissed_path(night: str) -> Path:
    return night_dir() / "dismissed" / night


def dismiss_report(night: str) -> None:
    marker = _dismissed_path(night)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()


def is_dismissed(night: str) -> bool:
    return _dismissed_path(night).exists()


def latest_report() -> dict | None:
    folder = reports_dir()
    if not folder.is_dir():
        return None
    names = sorted(p.stem for p in folder.glob("*.json"))
    for name in reversed(names):
        data = load_report(name)
        if data and "entries" in data:  # a bare claim is not a report yet
            return data
    return None
