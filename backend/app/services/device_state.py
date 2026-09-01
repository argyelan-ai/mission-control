"""Geräte-Soll/Ist-Abgleich und Ampel (Geräte-Steuerung, 01.09.2026).

Warum als eigener Service statt inline im Router: die Ampel wird an zwei
Stellen gebraucht (Setz-Endpunkt-Antwort und Lese-Endpunkt für die
Oberfläche) und ist die einzige Stelle, an der Soll und Ist verglichen
werden. Als reine Funktion ohne DB/HTTP ist sie ausserdem direkt testbar —
ohne sie über einen Endpunkt "durchzureichen".

Grundsatz aus dem Vertrag: **kein Feld = keine Meinung**. Ein Soll-Zustand
enthält nur die Felder, die der Betreiber wirklich vorgibt; alles andere
lässt der Agent unangetastet und darf deshalb auch die Ampel nicht auf gelb
ziehen.
"""
from datetime import datetime
from typing import Any

from app.utils import ensure_aware, utcnow

# Werte fest verdrahtet (Vertrag, Abschnitt "GPU-Modi"). Fail-closed: alles
# ausserhalb dieser Liste wird abgelehnt, nie durchgereicht — der Agent
# würde daraus einen root-nahen nvidia-smi-Aufruf bauen.
GPU_MODES = ("boost", "normal", "eco", "eco+")

# Plausibilitätsgrenzen. Sie sind bewusst weit, sollen aber Tippfehler
# abfangen, die auf dem Gerät echten Schaden anrichten: ein min_free_kbytes
# von 100 GB macht die Box unbenutzbar, eine MTU von 70 killt das Netz.
MIN_FREE_KBYTES_RANGE = (65_536, 67_108_864)   # 64 MB … 64 GB, in KB
MTU_RANGE = (1_280, 9_000)                      # IPv6-Minimum … Jumbo-Frames

# Ab wann eine Ist-Meldung als "nicht mehr frisch" gilt. Der Agent schlägt
# alle 15 s an (nodes.HEARTBEAT_INTERVAL_S); 120 s lassen also acht Runden
# ausfallen, bevor die Ampel meckert — kurze Netzhänger bleiben ruhig.
STALE_AFTER_S = 120

# Ampel-Farben. Englische Codes, damit die Oberfläche sie ohne Übersetzung
# als Schlüssel für ihre i18n-Texte nutzen kann.
STATUS_GREEN = "green"
STATUS_YELLOW = "yellow"
STATUS_RED = "red"
STATUS_GREY = "grey"


def desired_state_diff(
    desired: dict[str, Any] | None, current: dict[str, Any] | None
) -> list[str]:
    """Namen der Soll-Felder, die der Ist-Zustand (noch) nicht erfüllt.

    Nur Felder, die im Soll stehen, werden geprüft. Ein Ist ohne das Feld
    zählt als Abweichung — der Agent hat es dann noch nicht gemeldet und
    damit auch noch nicht nachgezogen.
    """
    if not desired:
        return []
    current = current or {}
    diff: list[str] = []

    for key in ("gpu_mode", "min_free_kbytes", "latency_tune"):
        if key in desired and current.get(key) != desired[key]:
            diff.append(key)

    # oom_guard ist im Soll ein Schalter (bool), im Ist ein Zustand
    # ("active"|"inactive"|"missing") — der Agent kann einen abgeschalteten
    # Wächter je nach Box als "inactive" ODER "missing" melden, beides
    # erfüllt den Wunsch "aus".
    if "oom_guard" in desired:
        ist = current.get("oom_guard")
        erfuellt = (ist == "active") if desired["oom_guard"] else (ist in ("inactive", "missing"))
        if not erfuellt:
            diff.append("oom_guard")

    # mtu ist im Soll eine nackte Zahl, im Ist {"iface": …, "value": …} —
    # die Schnittstelle sucht sich der Agent selbst aus, MC gibt sie nicht vor.
    if "mtu" in desired:
        ist_mtu = current.get("mtu")
        ist_wert = ist_mtu.get("value") if isinstance(ist_mtu, dict) else ist_mtu
        if ist_wert != desired["mtu"]:
            diff.append("mtu")

    return diff


def compute_status(
    *,
    is_agent_host: bool,
    desired: dict[str, Any] | None,
    current: dict[str, Any] | None,
    reported_at: datetime | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Ampel für ein Gerät — die Oberfläche soll nur noch eine Farbe malen.

    Reihenfolge ist Absicht: "kein Agent" schlägt alles (grau, es gibt gar
    nichts zu steuern), danach die harten Fehler (rot), dann das Wartende
    (gelb). Grün bleibt bewusst der engste Fall: Ist deckt das Soll und die
    Meldung ist frisch.
    """
    now = now or utcnow()

    if not is_agent_host:
        return {"status": STATUS_GREY, "reason": "no_agent", "diff": []}

    last_error = (current or {}).get("last_error")
    if last_error:
        return {"status": STATUS_RED, "reason": "last_error", "diff": [], "last_error": last_error}
    if not current:
        # Nie gehärtet: der Agent hat noch keinen Ist-Zustand gemeldet, also
        # laufen die Fallen aus dem Vertrag (kein Takt-Deckel, kein
        # OOM-Wächter) ungebremst — das ist ein Alarm, kein "warten".
        return {"status": STATUS_RED, "reason": "no_device_state", "diff": []}

    age_s = None
    if reported_at is not None:
        age_s = (now - ensure_aware(reported_at)).total_seconds()

    diff = desired_state_diff(desired, current)
    if diff:
        return {"status": STATUS_YELLOW, "reason": "pending", "diff": diff, "age_s": age_s}
    if age_s is None or age_s > STALE_AFTER_S:
        return {"status": STATUS_YELLOW, "reason": "stale", "diff": [], "age_s": age_s}

    return {"status": STATUS_GREEN, "reason": "in_sync", "diff": [], "age_s": age_s}
