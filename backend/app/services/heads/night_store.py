"""Night shift — where marks, reports and the settings live.

Data model (why files, not a table):
- **Marks** are one JSON file per task: ``<heads_root>/night/marks/<task_id>.json``.
  The task core is frozen (PRINCIPLES §8.4: no new task columns), the head
  launcher keeps all run state in files beside it (spec §6.1, no migration),
  and ROADMAP E3 lists "a jobs table without FK to tasks" only as a later,
  measured gap. A file per task needs no migration and no foreign key, so
  ``delete_task`` stays untouched: a mark whose task is gone is dropped by the
  next tick (and hidden from the list before that).
- **Reports** are ``<heads_root>/night/reports/<night>.json``: created with
  O_EXCL before sending (the dedup — one morning report per night, also
  across worker restarts), then filled with what was sent.
- **Settings** are five ``app_settings`` rows (``night_shift_*``) with env
  defaults in ``config.py``. They are read from the DB on every tick: the job
  runs in mc-worker, the Settings page saves through the API process.
"""
from __future__ import annotations

import json
import os
import uuid
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
}


def env_defaults() -> NightConfig:
    return NightConfig(
        enabled=bool(settings.night_shift_enabled),
        start=settings.night_shift_start,
        end=settings.night_shift_end,
        timezone=settings.night_shift_timezone,
        cloud_share=int(settings.night_shift_cloud_share),
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


def delete_mark(task_id: str) -> None:
    _mark_path(task_id).unlink(missing_ok=True)


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


# ── Reports ─────────────────────────────────────────────────────────────────


def reserve_report(night: str) -> bool:
    """Claim the morning report of ``night`` (O_EXCL). False = already claimed."""
    folder = reports_dir()
    folder.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(folder / f"{night}.json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False
    os.close(fd)
    return True


def release_report(night: str) -> None:
    (reports_dir() / f"{night}.json").unlink(missing_ok=True)


def write_report(night: str, data: dict) -> None:
    _atomic_write(reports_dir() / f"{night}.json", data)


def load_report(night: str) -> dict | None:
    try:
        data = json.loads((reports_dir() / f"{night}.json").read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def latest_report() -> dict | None:
    folder = reports_dir()
    if not folder.is_dir():
        return None
    names = sorted(p.stem for p in folder.glob("*.json"))
    for name in reversed(names):
        data = load_report(name)
        if data:
            return data
    return None
