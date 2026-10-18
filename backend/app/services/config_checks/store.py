"""Run registry checks and keep their latest result in ``config_checks``.

Every caller — the generic ``/config-checks`` API and the old per-page test
buttons — goes through :func:`run_check`, so a result is stored the same way
no matter which button produced it, and the age the UI shows comes from the
database (it survives a restart).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.log_redaction import redact_secrets
from app.models.config_check import ConfigCheck
from app.services.config_checks.registry import (
    REASON_CODES,
    REGISTRY,
    CheckKind,
    Outcome,
    Subject,
    reason_for_exception,
)

logger = logging.getLogger("mc.config_checks")

MESSAGE_MAX = 300


class UnknownCheck(LookupError):
    """No such kind, or no such thing inside the kind (→ 404)."""


class CheckNotRunnable(Exception):
    """The thing exists but cannot be checked right now (→ 409).

    ``code`` is ``not_configured`` (no value set) or ``not_testable`` (no
    procedure exists for it — the UI says "cannot be tested", never green).
    """

    def __init__(self, code: str, item: dict):
        super().__init__(code)
        self.code = code
        self.item = item


@dataclass
class CheckRun:
    item: dict
    outcome: Outcome


# ── helpers ───────────────────────────────────────────────────────────────


def _strings(material: Any) -> list[str]:
    if isinstance(material, str):
        return [material]
    if isinstance(material, dict):
        material = list(material.values())
    if isinstance(material, (list, tuple)):
        return [s for m in material for s in _strings(m)]
    return []


def fingerprint(material: Any) -> str | None:
    """Keyed hash of the tested value: lets us notice a replaced value
    without storing anything that could be turned back into it."""
    if material is None:
        return None
    key = (settings.secrets_encryption_key or settings.jwt_secret_key or "mc").encode()
    return hmac.new(key, repr(material).encode(), hashlib.sha256).hexdigest()[:32]


def scrub(message: str, material: Any) -> str:
    """Short, secret-free text: drop the tested values, then the known
    token shapes (``log_redaction``), then cut to the column width."""
    text = str(message or "")
    for value in _strings(material):
        if len(value) >= 6:
            text = text.replace(value, "<REDACTED>")
    text = redact_secrets(text).strip()
    return text[: MESSAGE_MAX - 1] + "…" if len(text) > MESSAGE_MAX else text


def _reason(outcome: Outcome) -> str:
    if outcome.reason in REASON_CODES:
        return outcome.reason
    if outcome.reason is None and outcome.ok:
        return "ok"
    return "unknown"


def _params(params: dict, material: Any) -> dict:
    """Only short scalars, strings scrubbed like ``message``."""
    clean: dict = {}
    for key, value in (params or {}).items():
        if isinstance(value, bool) or isinstance(value, (int, float)):
            clean[str(key)] = value
        elif isinstance(value, str):
            clean[str(key)] = scrub(value, material)[:120]
    return clean


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _kind(kind_key: str) -> CheckKind:
    _ensure_loaded()
    kind = REGISTRY.get(kind_key)
    if kind is None:
        raise UnknownCheck(kind_key)
    return kind


def _ensure_loaded() -> None:
    from app.services.config_checks import kinds  # noqa: F401 — registers entries


async def _subject(session: AsyncSession, kind: CheckKind, subject_id: str) -> Subject:
    for subject in await kind.list_subjects(session):
        if subject.id == subject_id:
            return subject
    raise UnknownCheck(f"{kind.key}/{subject_id}")


async def _row(session: AsyncSession, kind_key: str, subject_id: str) -> ConfigCheck | None:
    return (
        await session.exec(
            select(ConfigCheck).where(
                ConfigCheck.kind == kind_key, ConfigCheck.subject_id == subject_id
            )
        )
    ).first()


def _item(kind: CheckKind, subject: Subject, row: ConfigCheck | None) -> dict:
    current = fingerprint(subject.material) if subject.configured else None
    value_changed = bool(row and row.value_fingerprint != current)
    fresh = row is not None and not value_changed
    checked_at = _aware(row.checked_at) if fresh else None
    return {
        "kind": kind.key,
        "subject_id": subject.id,
        "label": subject.label,
        "secret_key": subject.secret_key,
        "configured": subject.configured,
        "testable": subject.testable,
        "cost": subject.cost or kind.cost,
        "state": ("ok" if row.ok else "fail") if fresh else "never_tested",
        "reason": (row.reason or ("ok" if row.ok else "unknown")) if fresh else None,
        "reason_params": (row.reason_params or {}) if fresh else {},
        "message": row.message if fresh else None,
        "duration_ms": row.duration_ms if fresh else None,
        "checked_at": checked_at.isoformat() if checked_at else None,
        "age_seconds": (
            int((datetime.now(timezone.utc) - checked_at).total_seconds()) if checked_at else None
        ),
        "value_changed": value_changed,
    }


# ── public API ────────────────────────────────────────────────────────────


def kinds_catalog() -> list[dict]:
    _ensure_loaded()
    return [
        {"key": k.key, "label": k.label, "cost": k.cost} for k in REGISTRY.values()
    ]


async def status_items(session: AsyncSession, kind_key: str | None = None) -> list[dict]:
    """Every known thing with its latest result — "never tested" included."""
    _ensure_loaded()
    kinds = [_kind(kind_key)] if kind_key else list(REGISTRY.values())
    rows = {
        (r.kind, r.subject_id): r for r in (await session.exec(select(ConfigCheck))).all()
    }
    items: list[dict] = []
    for kind in kinds:
        for subject in await kind.list_subjects(session):
            items.append(_item(kind, subject, rows.get((kind.key, subject.id))))
    return items


async def status_item(session: AsyncSession, kind_key: str, subject_id: str) -> dict:
    kind = _kind(kind_key)
    subject = await _subject(session, kind, subject_id)
    return _item(kind, subject, await _row(session, kind.key, subject.id))


async def run_check(session: AsyncSession, kind_key: str, subject_id: str) -> CheckRun:
    """Run the registry procedure for one thing and store the result.

    A procedure that raises is a failed check, not a 500 — the exception
    text (scrubbed) becomes the message.
    """
    kind = _kind(kind_key)
    subject = await _subject(session, kind, subject_id)
    if not subject.configured:
        raise CheckNotRunnable("not_configured", _item(kind, subject, None))
    if not subject.testable:
        raise CheckNotRunnable("not_testable", _item(kind, subject, None))

    started = time.monotonic()
    try:
        outcome = await kind.run(session, subject)
    except Exception as exc:  # noqa: BLE001 — a reportable state
        outcome = Outcome(
            ok=False, message=f"{type(exc).__name__}: {exc}", reason=reason_for_exception(exc)
        )
    duration_ms = int((time.monotonic() - started) * 1000)

    row = await _record(
        session,
        kind.key,
        subject.id,
        ok=outcome.ok,
        message=scrub(outcome.message, subject.material),
        reason=_reason(outcome),
        reason_params=_params(outcome.params, subject.material),
        duration_ms=duration_ms,
        value_fingerprint=fingerprint(subject.material),
    )
    logger.info("config check %s/%s → %s", kind.key, subject.id, "ok" if outcome.ok else "fail")
    return CheckRun(item=_item(kind, subject, row), outcome=outcome)


async def _record(session: AsyncSession, kind_key: str, subject_id: str, **values) -> ConfigCheck:
    """Upsert the latest result (one row per thing)."""
    values["checked_at"] = datetime.now(timezone.utc)
    for attempt in range(2):
        row = await _row(session, kind_key, subject_id)
        if row is None:
            row = ConfigCheck(kind=kind_key, subject_id=subject_id)
        for name, value in values.items():
            setattr(row, name, value)
        session.add(row)
        try:
            await session.commit()
        except IntegrityError:
            # A parallel click inserted the same thing first — update that row.
            await session.rollback()
            if attempt:
                raise
            continue
        await session.refresh(row)
        return row
    raise RuntimeError("unreachable")
