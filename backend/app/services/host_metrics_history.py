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
import time

import redis.asyncio as aioredis

from app.redis_client import RedisKeys

# 720 Punkte @ 5s Poll-Intervall = 1h Fenster (Spec §6).
HISTORY_MAX_POINTS = 720
HISTORY_WINDOW_SECONDS = 3600
# Dedupe: das Frontend pollt alle 5s je Host, aber mehrere Tabs/Clients
# können denselben Host gleichzeitig pollen — ein Punkt pro 4s reicht für
# eine 1h/720-Punkte-Auflösung und verhindert doppelte/verdichtete Punkte.
HISTORY_DEDUPE_SECONDS = 4


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
    """Schreibt einen Verlaufspunkt für ``host_id``, falls der letzte
    Schreibvorgang mindestens HISTORY_DEDUPE_SECONDS zurückliegt.

    Nur für erfolgreiche, GPU-tragende Metrik-Aufrufe gedacht — der Aufrufer
    (routers/hosts.py) ruft dies nur bei ``metrics.get("reachable")`` und
    kind in (ssh, agent). Gibt True zurück wenn geschrieben wurde, sonst
    False (Dedupe-Fenster noch offen) — nützlich für Tests."""
    key = RedisKeys.host_metrics_history(host_id)
    now = time.time()

    last_raw = await redis.lindex(key, -1)
    if last_raw is not None:
        try:
            last_point = json.loads(last_raw)
            if now - float(last_point.get("t", 0)) < HISTORY_DEDUPE_SECONDS:
                return False
        except (ValueError, TypeError):
            pass  # kaputter alter Punkt — überschreiben statt blockieren

    point = metrics_to_history_point(metrics, t=now)
    await redis.rpush(key, json.dumps(point))
    await redis.ltrim(key, -HISTORY_MAX_POINTS, -1)
    return True


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
