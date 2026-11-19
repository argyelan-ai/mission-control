"""Fleet default languages for NEW agents (Settings → Language).

Each agent keeps its own ``operator_language`` / ``work_language`` (the source
of truth, migration 0201). This module only answers "what does an agent get
when it is created": two operator-chosen defaults stored as ``app_settings``
rows. Changing a default never touches existing agents — the operator changes
those one by one in the same settings section.

Without a saved row both defaults are ``en`` (PRINCIPLES §6: only the lead
agent talks in the operator's language; everything else works in English).

Every code path that persists a new ``Agent`` calls
:func:`apply_language_defaults` before the first commit — guarded by
``tests/test_agent_language_defaults.py``.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.app_setting import AppSetting

#: Languages the settings UI offers. A new language = one entry here plus its
#: i18n keys (frontend-v2/messages/*.json, ``settingsLanguage.name*``).
SUPPORTED_LANGUAGES: tuple[str, ...] = ("en", "de")

#: app_settings key -> field name
SETTING_FIELDS: dict[str, str] = {
    "agent_default_operator_language": "operator_language",
    "agent_default_work_language": "work_language",
}


@dataclass(frozen=True)
class LanguageDefaults:
    operator_language: str = "en"
    work_language: str = "en"

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


async def load_defaults(session: AsyncSession) -> LanguageDefaults:
    """Saved defaults; an unknown or broken row falls back to ``en``."""
    values: dict[str, str] = {}
    rows = (
        await session.exec(select(AppSetting).where(AppSetting.key.in_(list(SETTING_FIELDS))))
    ).all()
    for row in rows:
        value = (row.value or "").strip().lower()
        if value in SUPPORTED_LANGUAGES:
            values[SETTING_FIELDS[row.key]] = value
    return LanguageDefaults(**values)


async def save_defaults(session: AsyncSession, updates: dict[str, str]) -> LanguageDefaults:
    """Validate, then upsert. Raises ``ValueError(list of bad field names)`` —
    nothing is written in that case."""
    by_name = {name: key for key, name in SETTING_FIELDS.items()}
    bad = sorted(
        name for name, value in updates.items()
        if name not in by_name or str(value).strip().lower() not in SUPPORTED_LANGUAGES
    )
    if bad:
        raise ValueError(bad)
    for name, value in updates.items():
        key = by_name[name]
        raw = str(value).strip().lower()
        row = (await session.exec(select(AppSetting).where(AppSetting.key == key))).one_or_none()
        if row is None:
            session.add(AppSetting(key=key, value=raw))
        else:
            row.value = raw
            session.add(row)
    await session.commit()
    return await load_defaults(session)


async def apply_language_defaults(session: AsyncSession, agent: Agent) -> Agent:
    """Give a NEW agent the fleet default languages (mutates and returns it).

    Call it on the freshly constructed ``Agent`` before its first commit; the
    create paths do not accept a language of their own, so the default always
    applies there.
    """
    defaults = await load_defaults(session)
    agent.operator_language = defaults.operator_language
    agent.work_language = defaults.work_language
    return agent
