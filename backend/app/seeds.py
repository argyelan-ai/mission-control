"""DB seed helpers — the shared boot-seeding path, extracted from app.main.

Architektur E, Teil 2 (Rex-Review PR #500, Blocker B1): die acht Seed-Helfer
hingen als private Funktionen an ``app.main``. ``app.background.prepare_process()``
importierte sie von dort und zog damit den kompletten FastAPI-Rumpf samt aller
Router in den Worker-Prozess — die Prozesstrennung war verschoben, nicht
behoben. Dies ist ein reiner Move: die Helfer haengen nur an
``app.database.engine`` + ``app.models`` + ``app.services.*`` — keiner von
ihnen braucht ``app.main``.

Import-Vertrag: sowohol ``app.main.lifespan`` (API) als auch
``app.background.prepare_process()`` (Worker) seeden ueber dieses Modul.
Nicht kreisfaehig importieren — dieses Modul darf niemals ``app.main``
importieren (genau dagegen sichert ``test_worker_no_main_import.py`` ab).
"""

import logging

from app.database import engine

logger = logging.getLogger("mc.startup")


async def _seed_templates() -> None:
    """Create builtin agent templates at startup (idempotent)."""
    try:
        from sqlmodel.ext.asyncio.session import AsyncSession
        from app.services.template_seeder import seed_builtin_templates

        async with AsyncSession(engine, expire_on_commit=False) as session:
            await seed_builtin_templates(session)
    except Exception as e:
        logger.warning("Template seeding failed (non-critical): %s", e)


async def _seed_scheduled_jobs() -> None:
    """Create built-in scheduled jobs at startup (idempotent)."""
    try:
        from sqlmodel.ext.asyncio.session import AsyncSession
        from app.services.schedule_seeder import seed_builtin_jobs

        async with AsyncSession(engine, expire_on_commit=False) as session:
            await seed_builtin_jobs(session)
    except Exception as e:
        logger.warning("Schedule seeding failed (non-critical): %s", e)


async def _seed_runtimes() -> None:
    """Import runtimes.json into DB on first run (idempotent)."""
    try:
        from sqlmodel.ext.asyncio.session import AsyncSession
        from app.services.runtime_seeder import seed_runtimes

        async with AsyncSession(engine, expire_on_commit=False) as session:
            await seed_runtimes(session)
    except Exception as e:
        logger.warning("Runtime seeding failed (non-critical): %s", e)


async def _seed_local_recipes() -> None:
    """Import config/local-recipes.json into the DB on first run (idempotent)."""
    try:
        from sqlmodel.ext.asyncio.session import AsyncSession
        from app.services.local_registry import repair_legacy_sparkrun_rows, seed_local_recipes

        async with AsyncSession(engine, expire_on_commit=False) as session:
            await seed_local_recipes(session)
            # Rezept-Umschalter (02.09.2026): alte engine=sparkrun-Zeilen
            # umwandeln (Befehl bleibt), damit es nur EIN Rezept-Modell gibt.
            await repair_legacy_sparkrun_rows(session)
    except Exception as e:
        logger.warning("Local recipe seeding failed (non-critical): %s", e)


async def _seed_hosts() -> None:
    """Bootstrap the host registry from settings + legacy runtime fields (ADR-048).

    Runs AFTER _seed_runtimes — the porsche host is derived from the
    unsloth-porsche runtime row. Idempotent; fresh installs without a GPU
    box end up with 0 hosts and 0 errors (cloud runtimes need no host).
    """
    try:
        from sqlmodel.ext.asyncio.session import AsyncSession
        from app.services.host_seeder import seed_hosts

        async with AsyncSession(engine, expire_on_commit=False) as session:
            await seed_hosts(session)
    except Exception as e:
        logger.warning("Host seeding failed (non-critical): %s", e)


async def _ensure_slot_runtimes() -> None:
    """Slot-Zeile je Head-Box anlegen und Agenten umhängen (ADR-078).

    Läuft bei jedem Start und ist idempotent — genau wie
    ``repair_legacy_sparkrun_rows``. Die Migration legt bewusst KEINE Zeilen an
    (Regel 7, ADR-077): welche Boxen es gibt, weiss nur die Datenbank des
    Betreibers, nicht das Repo.

    Nicht-kritisch: schlägt es fehl, bootet MC wie vorher — die Agenten hängen
    dann eben weiter an ihren Rezept-Zeilen.
    """
    try:
        from sqlmodel.ext.asyncio.session import AsyncSession
        from app.services.slot_runtimes import ensure_slot_runtimes

        async with AsyncSession(engine, expire_on_commit=False) as session:
            summary = await ensure_slot_runtimes(session)
        if summary.get("created") or summary.get("rebound"):
            logger.info(
                "Slot-Runtimes: %s angelegt, %s Agenten umgehängt",
                len(summary.get("created") or []), len(summary.get("rebound") or []),
            )
    except Exception as e:
        logger.warning("Slot-Runtime-Abgleich fehlgeschlagen (nicht kritisch): %s", e)


async def _seed_github_token() -> None:
    """Seed Vault with GH_TOKEN + GITHUB_OWNER from backend env on first startup.

    Idempotent per key: only creates the vault Secret (github_token /
    github_owner) when it does not exist yet AND the corresponding env var
    is set. This is the only path these values travel from .env into the
    vault; subsequent edits happen via Settings → GitHub or /api/v1/secrets
    (ADR-055). Finally primes the github_config cache so sync consumers
    (template rendering) see the resolved owner right after boot.

    Non-fatal: a missing or invalid value is logged as warning — backend
    still boots, agents just can't push to GitHub autonomously.
    """
    import os
    try:
        from sqlmodel import select as _select
        from sqlmodel.ext.asyncio.session import AsyncSession
        from app.models.secret import Secret
        from app.services.encryption import encrypt

        seeds = [
            (
                "github_token",
                os.environ.get("GH_TOKEN"),
                "GitHub Personal Access Token",
                "Delivered to agents via bootstrap for autonomous git push + gh CLI operations.",
            ),
            (
                "github_owner",
                os.environ.get("GITHUB_OWNER"),
                "GitHub Owner",
                "GitHub user/org under which MC creates project repos (not secret, "
                "stored here so Settings → GitHub edits apply without restart).",
            ),
        ]
        async with AsyncSession(engine, expire_on_commit=False) as session:
            for key, value, label, description in seeds:
                if not value:
                    logger.info("_seed_github_token: %s env var not set — skip", key)
                    continue
                existing = (await session.exec(
                    _select(Secret).where(Secret.key == key)
                )).first()
                if existing:
                    logger.debug("_seed_github_token: %s already in vault", key)
                    continue
                session.add(Secret(
                    key=key,
                    encrypted_value=encrypt(value),
                    provider="github",
                    label=label,
                    description=description,
                ))
                await session.commit()
                logger.info("_seed_github_token: seeded Vault with %s from env", key)

        from app.services.github_config import resolve_github_config
        await resolve_github_config(fresh=True)
    except Exception as e:
        logger.warning("_seed_github_token failed (non-critical): %s", e)


async def _seed_playbook_assets() -> None:
    """Seed core skill packs for the legacy lead agent / Playbooks (idempotent)."""
    try:
        from sqlmodel.ext.asyncio.session import AsyncSession
        from app.services.playbook_seeder import seed_skill_packs

        async with AsyncSession(engine, expire_on_commit=False) as session:
            await seed_skill_packs(session)
    except Exception as e:
        logger.warning("Playbook asset seeding failed (non-critical): %s", e)
