"""Runtime seeder — imports backend/config/runtimes.json into the DB on first run.

Called during app startup lifespan. Idempotent: existing runtimes (matched by
slug) are left untouched. New runtimes are inserted. The JSON file stays in the
repo as a seed template for open-source first-deploys.

For open-source users: runtimes.json can ship with placeholder hosts. On first
startup the seeder populates the DB; users then edit runtimes via UI.
"""
import json
import logging
from pathlib import Path

from sqlalchemy import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.runtime import Runtime

logger = logging.getLogger("mc.runtime_seeder")

_SEED_PATH = Path(__file__).parent.parent.parent / "config" / "runtimes.json"


async def seed_runtimes(session: AsyncSession) -> tuple[int, int]:
    """Seed runtimes from JSON into DB. Idempotent.

    Returns (inserted, skipped) counts.
    """
    if not _SEED_PATH.exists():
        logger.info("runtimes.json not found at %s — skipping seed", _SEED_PATH)
        return (0, 0)

    with open(_SEED_PATH, "r") as f:
        entries = json.load(f)

    existing = await session.exec(select(Runtime.slug))
    existing_slugs = {row for row in existing.scalars().all()}

    inserted = 0
    skipped = 0
    for entry in entries:
        slug = entry.get("id")
        if not slug:
            logger.warning("seed entry missing id: %r", entry)
            continue
        if slug in existing_slugs:
            skipped += 1
            continue

        runtime = Runtime(
            slug=slug,
            display_name=entry.get("display_name", slug),
            runtime_type=entry.get("runtime_type", "openai_compatible"),
            endpoint=entry.get("endpoint", ""),
            healthcheck_path=entry.get("healthcheck_path"),
            model_identifier=entry.get("model_identifier") or entry.get("lms_identifier"),
            container_name=entry.get("container_name"),
            lms_identifier=entry.get("lms_identifier"),
            lms_cli_path=entry.get("lms_cli_path"),
            launch_command=entry.get("launch_command"),
            host=entry.get("host"),
            control_url=entry.get("control_url"),
            wol_mac_address=entry.get("wol_mac_address"),
            power_managed=bool(entry.get("power_managed", False)),
            role_tags=entry.get("role_tags") or [],
            supports_tools=bool(entry.get("supports_tools", False)),
            supports_reasoning=bool(entry.get("supports_reasoning", False)),
            supports_streaming=bool(entry.get("supports_streaming", True)),
            preferred_context_len=entry.get("preferred_context_len"),
            max_context_len=entry.get("max_context_len"),
            gpu_profile=entry.get("gpu_profile"),
            memory_notes=entry.get("memory_notes") or None,
            startup_notes=entry.get("startup_notes") or None,
            ui_order=int(entry.get("ui_order", 999)),
            enabled=bool(entry.get("enabled", True)),
            single_instance=bool(entry.get("single_instance", False)),
        )
        session.add(runtime)
        inserted += 1

    if inserted:
        await session.commit()
        logger.info("runtime seed: inserted=%d skipped=%d", inserted, skipped)
    else:
        logger.debug("runtime seed: inserted=0 skipped=%d (already seeded)", skipped)

    return (inserted, skipped)
