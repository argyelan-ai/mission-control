"""Taegliches Jarvis-Morgenbriefing (ADR-062).

Ein Hintergrund-Loop (gleiche Konvention wie ``_vault_lint_loop`` /
``_vault_decay_loop`` in ``app.main``) generiert einmal taeglich um
``JARVIS_BRIEFING_HOUR`` (Europe/Zurich) ein kompaktes deutsches Morgenbriefing:

1. Aggregierte Vault-/Board-Daten holen — ueber den geteilten
   ``jarvis_core.mc_client`` (Self-Call gegen ``/agent/vault/briefing`` mit dem
   Jarvis-Token, exakt der V1.5-Briefing-Pfad: age_days, staleness, dedup).
2. Das Frontier-Modell (C1-Codepfad, ``jarvis_core.frontier``) schreibt daraus
   einen kurzen Briefing-Text (was offen ist, was neu ist, was Marks Entscheidung
   braucht).
3. Der Text wird als Vault-Note ``Morgenbriefing YYYY-MM-DD`` abgelegt (durable)
   und zusaetzlich in Redis gecacht (schneller, lag-freier Read-Path fuer das
   ``briefing``-Tool + Idempotenz-Guard).

Idempotent pro Tag: der Redis-Key wird per ``SET NX`` gesetzt; ein zweiter Lauf
am selben Tag ist ein No-Op. Schlaegt die Generierung fehl, wird der Guard wieder
freigegeben, damit ein spaeterer Lauf es erneut versuchen kann.

Feature-Gate: aktiv nur bei ``JARVIS_BRIEFING_ENABLED=true`` UND gesetztem
``OPENAI_API_KEY``. Sonst startet der Loop gar nicht erst.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.config import settings

logger = logging.getLogger("mc.jarvis_briefing")


def _resolve_zurich():
    """Europe/Zurich tz, mit defensivem UTC-Fallback.

    Fehlt tzdata im Image (ZoneInfoNotFoundError), soll der Briefing-Loop NICHT
    dauerhaft crashen — dann laeuft er auf UTC-Basis weiter (die Uhrzeit ist dann
    UTC statt lokal, aber das Feature bleibt funktionsfaehig) und warnt einmal.
    """
    try:
        return ZoneInfo("Europe/Zurich")
    except Exception as e:  # noqa: BLE001 — tzdata missing → degrade to UTC
        logger.warning("tzdata Europe/Zurich unavailable (%s) — falling back to UTC", e)
        return timezone.utc


ZURICH = _resolve_zurich()
# 36h TTL: laenger als ein Tag, sodass der Read-Path das heutige Briefing sicher
# noch findet, aber alte Tage von selbst verfallen.
BRIEFING_TTL_SECONDS = 36 * 3600
# Kurzes TTL fuer den "__generating__"-Platzhalter: schlaegt die Generierung
# hart fehl (Prozess-Crash, bevor der finally-Release greift), soll der Guard
# nicht 36h blockieren — nach 15 Min ist ein Retry am selben Tag wieder moeglich.
PLACEHOLDER_TTL_SECONDS = 15 * 60

# jarvis_core liegt im Repo-Root (Live-Mount im Backend-Image, ADR-061). Weicher
# Import: fehlt das Package, bleibt das Feature still inaktiv statt den Start zu
# gefaehrden — identisch zur jarvis_telegram-Konvention.
try:
    from jarvis_core import frontier
    from jarvis_core import mc_client
    from jarvis_core import tools as jtools

    _JARVIS_CORE_OK = True
except Exception as _exc:  # noqa: BLE001 — degrade gracefully if package absent
    logger.error(
        "jarvis_core import failed — morning briefing DISABLED. Expected the "
        "jarvis_core package (repo-root live-mount at /app/jarvis_core, ADR-061). "
        "Error: %s",
        _exc,
    )
    _JARVIS_CORE_OK = False


def parse_hhmm(value: str, *, default: tuple[int, int] = (6, 30)) -> tuple[int, int]:
    """Parst 'HH:MM' → (hour, minute); faellt bei Unsinn auf ``default`` zurueck."""
    try:
        h_str, m_str = str(value).split(":", 1)
        h, m = int(h_str), int(m_str)
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except (ValueError, AttributeError):
        pass
    logger.warning("Invalid JARVIS_BRIEFING_HOUR %r — using default %02d:%02d", value, *default)
    return default


def seconds_until_next(hour: int, minute: int, now: datetime) -> float:
    """Sekunden bis zur naechsten Uhrzeit hour:minute (tz-aware ``now``).

    Faellt der Zeitpunkt heute schon in die Vergangenheit, ist es der Zeitpunkt
    morgen. Reine Funktion — im Test ohne echte Zeit pruefbar.
    """
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _briefing_prompt(context_md: str, date_iso: str) -> tuple[str, str]:
    """System- + User-Prompt fuer das Frontier-Modell."""
    system = (
        "Du schreibst das taegliche Morgenbriefing fuer Mark, den Operator eines "
        "AI-Agent-Command-Centers (Mission Control). Fasse die gelieferten Board-/"
        "Vault-Daten zu einem KURZEN, klaren deutschen Briefing zusammen. Struktur: "
        "1) was gerade offen/in Arbeit ist, 2) was neu ist, 3) was Marks Entscheidung "
        "braucht. Sei ehrlich zur Aktualitaet: die Daten tragen Altersangaben (z.B. "
        "'(vor 3 Tagen)') — uebernimm diese, praesentiere Altes nie als frisch. Wenn "
        "wenig los ist, sag das knapp statt zu fuellen. Keine Anrede-Floskeln, kein "
        "Meta-Kommentar. Maximal ~150 Woerter."
    )
    user = f"Datum: {date_iso}\n\nAggregierte Daten:\n{context_md or '(keine Daten)'}"
    return system, user


async def run_briefing_once(now: datetime | None = None) -> dict:
    """Generiert + speichert das Morgenbriefing fuer HEUTE (idempotent).

    Rueckgabe ist ein Status-Dict (fuer Tests/Logs). Wirft nicht — Fehler werden
    als ``{"ok": False, ...}`` zurueckgegeben und der Idempotenz-Guard wieder
    freigegeben.
    """
    if not (_JARVIS_CORE_OK and settings.jarvis_briefing_enabled and settings.openai_api_key):
        return {"ok": False, "reason": "disabled"}

    from app.redis_client import RedisKeys, get_redis

    now = now or datetime.now(ZURICH)
    date_iso = now.strftime("%Y-%m-%d")
    key = RedisKeys.jarvis_daily_briefing(date_iso)

    redis = await get_redis()
    # Kurzes TTL nur auf den Platzhalter (W1): der fertige Text bekommt unten 36h.
    acquired = await redis.set(key, "__generating__", nx=True, ex=PLACEHOLDER_TTL_SECONDS)
    if not acquired:
        logger.info("Morning briefing for %s already done/in progress — skip", date_iso)
        return {"ok": True, "skipped": True, "reason": "already_done_today", "date": date_iso}

    try:
        briefing = await mc_client.vault_briefing()
        context_md = jtools.format_briefing_as_context(briefing)
        system, user = _briefing_prompt(context_md, date_iso)
        text = await frontier.complete_text(
            system=system,
            user=user,
            api_key=settings.openai_api_key,
            model=settings.jarvis_frontier_model or None,
            max_tokens=frontier.BRIEFING_MAX_TOKENS,
        )
        if not text:
            raise RuntimeError("Frontier-Modell lieferte keinen Briefing-Text")

        # Read-Path + Idempotenz: den echten Text unter denselben Key legen.
        await redis.set(
            key,
            json.dumps({"date": date_iso, "text": text}),
            ex=BRIEFING_TTL_SECONDS,
        )
        # Durable Vault-Note (upsert auf deterministischen Pfad via Titel-Slug).
        await mc_client.vault_write_note(
            text,
            type="note",
            tags=["briefing", "jarvis"],
            title=f"Morgenbriefing {date_iso}",
        )
        logger.info("Morning briefing generated for %s (%d chars)", date_iso, len(text))
        return {"ok": True, "date": date_iso, "chars": len(text)}
    except Exception as e:  # noqa: BLE001 — release guard so a retry can run
        try:
            await redis.delete(key)
        except Exception:  # noqa: BLE001
            logger.warning("Failed to release briefing guard for %s", date_iso)
        logger.exception("Morning briefing generation failed for %s", date_iso)
        return {"ok": False, "error": str(e), "date": date_iso}


async def jarvis_briefing_loop() -> None:
    """Loop: schlaeft bis zur naechsten JARVIS_BRIEFING_HOUR, generiert, wiederholt.

    Startet nur bei aktivem Feature + vorhandenem Key (die Gate-Pruefung erfolgt
    auch in ``run_briefing_once``, hier zusaetzlich, um den Loop gar nicht erst zu
    starten). Per-Iteration-Fehler werden geloggt, der Loop laeuft weiter; nur ein
    ``CancelledError`` (Shutdown) bricht ab.
    """
    if not (_JARVIS_CORE_OK and settings.jarvis_briefing_enabled and settings.openai_api_key):
        logger.info("jarvis_briefing_loop not started (feature disabled or no OPENAI_API_KEY)")
        return

    hour, minute = parse_hhmm(settings.jarvis_briefing_hour)
    logger.info("jarvis_briefing_loop started (daily at %02d:%02d Europe/Zurich)", hour, minute)
    while True:
        try:
            delay = seconds_until_next(hour, minute, datetime.now(ZURICH))
            await asyncio.sleep(delay)
            await run_briefing_once()
        except asyncio.CancelledError:
            logger.info("jarvis_briefing_loop cancelled")
            break
        except Exception as e:  # noqa: BLE001 — never kill the loop
            logger.error("jarvis_briefing_loop iteration error: %s", e, exc_info=True)
            # Kurze Pause, damit ein sofort-wiederkehrender Fehler keinen Tight-Loop macht.
            await asyncio.sleep(60)
