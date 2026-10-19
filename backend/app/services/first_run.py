"""First-run wizard state: has the operator finished /setup yet?

Why this exists: the login page sent a fresh admin to /setup exactly once —
right after the registration request. Any other way in (a reload, a second
tab, logging in again after closing the browser, a phone opened later) went
to Home, which on a fresh install is an empty "No board selected" screen with
no hint that a wizard exists.

The state lives in the shared ``app_settings`` KV table under one key:

* ``"pending"`` — written by ``POST /auth/register`` in the same commit as
  the first admin. Only a NEW install ever reaches that line.
* ``"done"`` — written when the operator leaves the wizard via its final
  button (``POST /auth/first-run/complete``).
* absent — every install that existed before this key did. Those operators
  set their system up long ago and must never be pushed into the wizard, so
  "absent" means "not pending" — no backfill migration needed.
"""
from __future__ import annotations

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.app_setting import AppSetting

FIRST_RUN_KEY = "first_run_wizard"
PENDING = "pending"
DONE = "done"

#: What the first run created (services/first_run_starter.py, JSON of ids).
FIRST_RUN_STARTER_KEY = "first_run_starter"

#: Keys this module owns in app_settings (channel_config skips them quietly).
FIRST_RUN_SETTING_FIELDS = frozenset({FIRST_RUN_KEY, FIRST_RUN_STARTER_KEY})


async def _row(session: AsyncSession) -> AppSetting | None:
    return (
        await session.exec(select(AppSetting).where(AppSetting.key == FIRST_RUN_KEY))
    ).one_or_none()


async def wizard_pending(session: AsyncSession) -> bool:
    row = await _row(session)
    return row is not None and row.value == PENDING


async def set_state(session: AsyncSession, value: str, *, commit: bool = True) -> None:
    row = await _row(session)
    if row is None:
        session.add(AppSetting(key=FIRST_RUN_KEY, value=value))
    else:
        row.value = value
        session.add(row)
    if commit:
        await session.commit()
