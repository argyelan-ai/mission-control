"""Eine Tuer fuer Discord-Alarme — mit Wiederholungssperre und Sammelmeldung.

Vorher schickte ``emit_event`` jede Warnung sofort und einzeln nach Discord.
Gemessen an der Live-DB (7 Tage): 164 Alarm-Events, davon 156 Warnungen,
einzelne Meldungen bis zu 13x wortgleich. Wer so viel bekommt, liest nichts
mehr — die eine Meldung, die zaehlt, geht im Rest unter.

Zwei Regeln:

* **Wiederholungssperre.** Dasselbe Thema geht innerhalb von
  ``DEDUP_TTL_SECONDS`` nur einmal raus. Zahlen im Titel bilden kein neues
  Thema: der Watchdog misst alle 30s einen anderen Millisekundenwert, und
  ohne diese Normalisierung waere jede Messung "neu" und die Sperre wirkungslos.
* **Dringlichkeit entscheidet den Weg.** ``error``/``critical`` gehen sofort
  raus. Warnungen sammeln sich und kommen als EINE Nachricht, sobald das
  Zeitfenster voll ist oder zu viele warten.

Was hier NICHT passiert: das ActivityEvent unterdruecken. Die Historie in der
UI bleibt vollstaendig — nur der Discord-Kanal wird leiser. Wer einen Vorfall
nachvollziehen will, findet ihn weiterhin lueckenlos.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time

from app.config import settings
from app.redis_client import get_redis

logger = logging.getLogger("mc.discord_notify")

# Sofort-Zustellung nur fuer diese Stufen. Warnungen werden gesammelt.
IMMEDIATE_SEVERITIES = ("error", "critical")
DIGEST_SEVERITIES = ("warning",)

DEDUP_KEY_PREFIX = "mc:discord:seen:"
DIGEST_KEY = "mc:discord:digest"
DIGEST_MAX_ITEMS = 20  # Sammlung geht frueher raus, wenn so viele warten

# Beide ueber .env stellbar (DISCORD_DIGEST_WINDOW_SECONDS /
# DISCORD_DEDUP_TTL_SECONDS), damit die Lautstaerke ohne Code-Aenderung passt.
DEDUP_TTL_SECONDS = settings.discord_dedup_ttl_seconds
DIGEST_WINDOW_SECONDS = settings.discord_digest_window_seconds

# Nur eingeklammerte Messwerte gelten als Rauschen: "(342ms)", "(3 consecutive
# probes)", "(5 Neustarts)". Bewusst NICHT jede Zahl — sonst faellt
# "deepseek-v4" mit "deepseek-v5" zusammen und zwei echte Runtimes werden zu
# einem Thema. Lieber eine Meldung zu viel als eine verschluckte.
_MEASURED = re.compile(r"\(\s*\d[^)]*\)")


def _topic(event_type: str, title: str) -> str:
    """Fingerabdruck eines Themas.

    "antwortet langsam (342ms)" und "(501ms)" sind dasselbe Problem, nicht
    zwei — sonst greift die Sperre nie, weil der Watchdog alle 30s einen
    anderen Wert misst.
    """
    normalised = _MEASURED.sub("(#)", title)[:200]
    return hashlib.sha1(f"{event_type}|{normalised}".encode()).hexdigest()


async def _deliver(title: str, description: str, severity: str = "warning") -> None:
    """Tatsaechlicher Versand. Eigene Funktion, damit Tests hier ansetzen."""
    from app.services.discord import send_discord_notification
    from app.services.discord_router import notify_alert

    # Ops-Webhook ist optional und steigt ohne Konfiguration still aus
    # (discord.py). Wo beide Wege konfiguriert sind, kaeme dieselbe Meldung
    # sonst doppelt an — deshalb liegt der Kanal-Post im else.
    if settings.discord_webhook_ops:
        await send_discord_notification(
            title=title, description=description, severity=severity,
        )
    else:
        await notify_alert(title, description, severity)


async def notify_event(
    event_type: str,
    title: str,
    severity: str,
    *,
    detail: dict | None = None,
) -> str:
    """Einen Alarm einreichen.

    Gibt zurueck, was damit passiert ist — ``sent``, ``queued``,
    ``suppressed`` (Wiederholung) oder ``skipped`` (nicht alarmwuerdig).
    Wirft nie: eine Benachrichtigung darf den Arbeitsfluss nicht kippen.
    """
    if severity not in IMMEDIATE_SEVERITIES and severity not in DIGEST_SEVERITIES:
        return "skipped"

    try:
        redis = await get_redis()
        key = f"{DEDUP_KEY_PREFIX}{_topic(event_type, title)}"
        fresh = await redis.set(key, "1", nx=True, ex=DEDUP_TTL_SECONDS)
        if not fresh:
            logger.debug("Discord: Wiederholung unterdrueckt — %s", title[:80])
            return "suppressed"

        if severity in IMMEDIATE_SEVERITIES:
            await _deliver(title, f"Ereignis: {event_type}", severity)
            return "sent"

        await redis.rpush(DIGEST_KEY, json.dumps({
            "ts": time.time(),
            "title": title,
            "event_type": event_type,
            "severity": severity,
        }))
        return "queued"
    except Exception as e:  # noqa: BLE001 — Benachrichtigung ist best-effort
        logger.warning("Discord-Benachrichtigung fehlgeschlagen: %s", e)
        return "skipped"


async def flush_digest() -> int:
    """Gesammelte Warnungen als eine Nachricht rausschicken.

    Laeuft im Watchdog-Takt. Sendet nur, wenn das Zeitfenster abgelaufen ist
    oder zu viele warten — sonst sammelt es weiter. Gibt die Zahl der
    zugestellten Warnungen zurueck (0 = nichts getan).
    """
    try:
        redis = await get_redis()
        count = await redis.llen(DIGEST_KEY)
        if not count:
            return 0

        if count < DIGEST_MAX_ITEMS:
            oldest_raw = await redis.lindex(DIGEST_KEY, 0)
            if oldest_raw:
                try:
                    oldest = json.loads(oldest_raw)
                    if time.time() - float(oldest.get("ts", 0)) < DIGEST_WINDOW_SECONDS:
                        return 0
                except (ValueError, TypeError):
                    pass  # unlesbarer Eintrag: lieber jetzt rausschicken

        # Atomar leeren, damit ein zweiter Worker nicht dasselbe nochmal sendet.
        items_raw = await redis.lrange(DIGEST_KEY, 0, -1)
        await redis.delete(DIGEST_KEY)

        items = []
        for raw in items_raw:
            try:
                items.append(json.loads(raw))
            except (ValueError, TypeError):
                continue
        if not items:
            return 0

        # Wiederholungen buendeln: "3x qwen-general nicht erreichbar"
        grouped: dict[str, dict] = {}
        for item in items:
            t = _topic(item.get("event_type", ""), item.get("title", ""))
            entry = grouped.setdefault(t, {"title": item.get("title", ""), "n": 0})
            entry["n"] += 1

        lines = []
        for entry in sorted(grouped.values(), key=lambda e: -e["n"]):
            prefix = f"{entry['n']}x " if entry["n"] > 1 else ""
            lines.append(f"• {prefix}{entry['title']}")

        minutes = max(1, round(DIGEST_WINDOW_SECONDS / 60))
        title = f"{len(items)} Warnungen gesammelt"
        description = (
            "\n".join(lines[:25])
            + f"\n\nGesammelt ueber bis zu {minutes} Minuten. "
            "Fehler kommen weiterhin sofort."
        )
        await _deliver(title, description, "warning")
        return len(items)
    except Exception as e:  # noqa: BLE001 — best-effort
        logger.warning("Discord-Sammelmeldung fehlgeschlagen: %s", e)
        return 0
