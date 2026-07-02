from __future__ import annotations

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.playbook import SkillPack
from app.services.playbook_catalog import list_skill_pack_catalog
from app.utils import utcnow


async def seed_skill_packs(session: AsyncSession) -> None:
    existing_result = await session.exec(select(SkillPack))
    existing = {pack.key: pack for pack in existing_result.all()}

    changed = False
    for payload in list_skill_pack_catalog():
        key = payload["key"]
        pack = existing.get(key)
        if pack:
            pack.name = payload["name"]
            pack.description = payload.get("description")
            pack.category = payload.get("category", pack.category)
            pack.icon = payload.get("icon")
            pack.color = payload.get("color")
            pack.skill_keys = payload.get("skill_keys") or []
            pack.guidance = payload.get("guidance")
            pack.updated_at = utcnow()
            session.add(pack)
            changed = True
            continue

        session.add(
            SkillPack(
                key=key,
                name=payload["name"],
                description=payload.get("description"),
                category=payload.get("category", "general"),
                status="active",
                icon=payload.get("icon"),
                color=payload.get("color"),
                skill_keys=payload.get("skill_keys") or [],
                guidance=payload.get("guidance"),
                created_by="system",
            )
        )
        changed = True

    if changed:
        await session.commit()
