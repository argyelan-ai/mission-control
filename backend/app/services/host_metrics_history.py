"""Telemetrie-Verlauf 1h je Host (Bühne v2 §6, PR 3).

Schreibweg A (Pflicht, siehe Spec): dieses Modul hängt sich in den
BESTEHENDEN 5s-Poll von ``GET /hosts/{id}/metrics`` (Frontend SlotStage/
useGpuSparkline) — jeder erfolgreiche Aufruf schreibt maximal einen Punkt
alle HISTORY_DEDUPE_SECONDS in einen Redis-Ring. Kein zweiter SSH-Weg.

Schreibweg B (Hintergrund-Sampler, Setting HOST_METRICS_HISTORY_SAMPLER)
wurde bewusst NICHT gebaut: runtime_manager.get_host_metrics() öffnet für
kind=="ssh" eine echte SSH-Verbindung + nvidia-smi/free-Aufruf pro Host
(_ssh_run) — ein alle-5s-Sampler für jeden registrierten Host, egal ob ein
Browser offen ist, würde die SSH-Last der Flotte vervielfachen. Für
kind=="agent" wäre es zwar billig (nur der zuletzt gepushte Snapshot), aber
ein gemischter Sampler (billig für agent, teuer für ssh) ist mehr Komplexität
als der Nutzen hergibt, solange PR 3 (SlotStage/Bühne) sowieso einen offenen
Browser voraussetzt. Siehe PR-Beschreibung für die volle Abwägung.
"""

from __future__ import annotations

import json
import logging
import time

import redis.asyncio as aioredis

from app.redis_client import RedisKeys

logger = logging.getLogger(__name__)

# 720 Punkte @ 5s Poll-Intervall = 1h Fenster (Spec §6).
HISTORY_MAX_POINTS = 720
HISTORY_WINDOW_SECONDS = 3600
# Dedupe: das Frontend pollt alle 5s je Host, aber mehrere Tabs/Clients
# können denselben Host gleichzeitig pollen — ein Punkt pro 4s reicht für
# eine 1h/720-Punkte-Auflösung und verhindert doppelte/verdichtete Punkte.
HISTORY_DEDUPE_SECONDS = 4

# Wie oft (max.) ein Redis-Fehler beim Ring-Schreiben geloggt wird, je Host —
# verhindert Log-Spam, wenn Redis für längere Zeit ausfällt (bei 5s-Poll
# wären das sonst 12 identische Fehler pro Minute).
_ERROR_LOG_THROTTLE_SECONDS = 60
_last_error_logged_at: dict[str, float] = {}


def metrics_to_history_point(metrics: dict, *, t: float | None = None) -> dict:
    """Mappt das host_metrics()-Rückgabe-Dict auf einen Verlaufspunkt.

    ``fan`` gibt es in keiner der bestehenden Metrikquellen (SSH-Parsing
    nvidia-smi/free, Node-Agent-Telemetrie) — bleibt darum immer ``None``,
    wie in der Spec vorgesehen ("fan null wenn nicht vorhanden")."""
    return {
        "t": t if t is not None else time.time(),
        "gpu": metrics.get("gpu_util_pct"),
        "ram_used": metrics.get("ram_used_mb"),
        "ram_total": metrics.get("ram_total_mb"),
        "temp": metrics.get("gpu_temp_c"),
        "fan": metrics.get("fan_pct"),
    }


async def record_metrics_point(redis: aioredis.Redis, host_id: str, metrics: dict) -> bool:
    """Schreibt einen Verlaufspunkt für ``host_id``, dedupliziert atomar.

    Review-Fund rev-437: die frühere Version las den letzten Zeitstempel und
    schrieb dann erst — bei zwei gleichzeitigen Aufrufen (mehrere offene
    Tabs/Clients pollen denselben Host) konnten beide den Read VOR dem
    jeweils anderen Write sehen und die Dedupe umgehen (Doppelpunkt im
    selben 4s-Fenster). Jetzt: ``SET NX EX HISTORY_DEDUPE_SECONDS`` auf einen
    separaten Marker-Key — Redis garantiert, dass genau EIN gleichzeitiger
    Aufrufer den Marker bekommt (atomare Operation, kein Read-then-Write).
    Nur dieser eine schreibt den Punkt.

    Nur für erfolgreiche, GPU-tragende Metrik-Aufrufe gedacht — der Aufrufer
    (routers/hosts.py) ruft dies nur bei ``metrics.get("reachable")`` und
    kind in (ssh, agent). Gibt True zurück wenn geschrieben wurde, sonst
    False (Dedupe-Fenster noch offen bzw. jemand anders hat es gerade
    gewonnen) — nützlich für Tests."""
    marker_key = RedisKeys.host_metrics_history_dedupe_marker(host_id)
    won_marker = await redis.set(marker_key, "1", nx=True, ex=HISTORY_DEDUPE_SECONDS)
    if not won_marker:
        return False

    key = RedisKeys.host_metrics_history(host_id)
    point = metrics_to_history_point(metrics)
    await redis.rpush(key, json.dumps(point))
    await redis.ltrim(key, -HISTORY_MAX_POINTS, -1)
    return True


def log_history_failure(host_id: str, op: str, exc: Exception) -> None:
    """Loggt einen fehlgeschlagenen Ring-Zugriff (Schreiben ODER Lesen),
    gedrosselt auf höchstens 1×/_ERROR_LOG_THROTTLE_SECONDS je Host — bei
    5s-Poll wären das sonst 12 identische Warnungen pro Minute, solange
    Redis down ist (Review-Fund rev-437). ``op`` ist nur für die Log-Zeile
    ("schreiben"/"lesen") — die Drossel selbst ist pro Host, nicht pro
    Operation, damit ein flatterndes Redis nicht doppelt so oft loggt.
    Geteilte Funktion für ``record_metrics_point_safe`` (Schreiben),
    ``read_history_safe`` (Lesen) und den Router (Fehler schon beim
    ``get_redis()``, vor jedem der beiden)."""
    now = time.time()
    last_logged = _last_error_logged_at.get(host_id, 0.0)
    if now - last_logged >= _ERROR_LOG_THROTTLE_SECONDS:
        _last_error_logged_at[host_id] = now
        logger.warning(
            "Telemetrie-Verlauf für Host %s konnte nicht %s werden "
            "(gedrosseltes Log, max. 1/%ss): %s",
            host_id, op, _ERROR_LOG_THROTTLE_SECONDS, exc,
        )


async def record_metrics_point_safe(redis: aioredis.Redis, host_id: str, metrics: dict) -> bool:
    """Wie ``record_metrics_point``, aber schluckt jeden Fehler.

    Der Telemetrie-Verlauf ist ein Nebenprodukt des 5s-Metrics-Polls, nie
    sein Zweck — fällt Redis aus oder wirft der JSON-Serializer, darf das
    den eigentlichen ``GET /hosts/{id}/metrics``-Aufruf (SlotStage-Poll der
    ganzen Seite) NIE mitreissen (Review-Fund rev-437). Gibt False zurück,
    wenn nicht geschrieben wurde (Fehler ODER Dedupe)."""
    try:
        return await record_metrics_point(redis, host_id, metrics)
    except Exception as e:
        log_history_failure(host_id, "geschrieben", e)
        return False


async def read_history(
    redis: aioredis.Redis, host_id: str, window_seconds: int = HISTORY_WINDOW_SECONDS
) -> list[dict]:
    """Liest den Ring, gefiltert auf die letzten ``window_seconds``.

    Leerer Ring oder kaputte Einträge → leere/übersprungene Punkte statt
    5xx (gleicher Grundsatz wie host_metrics: nie einen Fehler werfen)."""
    key = RedisKeys.host_metrics_history(host_id)
    raw_points = await redis.lrange(key, 0, -1)
    cutoff = time.time() - window_seconds
    points: list[dict] = []
    for raw in raw_points:
        try:
            point = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if point.get("t", 0) >= cutoff:
            points.append(point)
    return points


async def read_history_safe(
    redis: aioredis.Redis, host_id: str, window_seconds: int = HISTORY_WINDOW_SECONDS
) -> list[dict]:
    """Wie ``read_history``, aber schluckt jeden Fehler (Redis down o.ä.) und
    liefert eine leere Liste statt eine Exception nach oben durchzureichen —
    GET /{host_id}/metrics/history muss immer 200 mit ``points: []``
    beantworten können, nie 5xx (Review-Fund rev-437). Gedrosseltes Log
    teilt sich die Drossel mit dem Schreibweg (``log_history_failure``)."""
    try:
        return await read_history(redis, host_id, window_seconds)
    except Exception as e:
        log_history_failure(host_id, "gelesen", e)
        return []
