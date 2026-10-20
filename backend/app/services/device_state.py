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
# Review M5 (02.09.2026): enger gezogen. 64 GB Reserve wäre auf einer
# 128-GB-Box die halbe Maschine, 16 GB reicht für jede sinnvolle Einstellung
# (im Feld: 5 GB). MTU unter 1500 hat keinen Nutzen, nur einen leisen
# Leistungseinbruch im Verbund. IDENTISCH zu scripts/node-agent/mc-node-agent.py
# (MIN_FREE_KBYTES_MIN/MAX, MTU_MIN/MAX) und zu den Wrappern in scripts/device/.
MIN_FREE_KBYTES_RANGE = (65_536, 16_777_216)   # 64 MB … 16 GB, in KB
MTU_RANGE = (1_500, 9_000)                      # Ethernet-Standard … Jumbo-Frames

# Ab wann eine Ist-Meldung als "nicht mehr frisch" gilt. Der Agent schlägt
# alle 15 s an (nodes.HEARTBEAT_INTERVAL_S); 120 s lassen also acht Runden
# ausfallen, bevor die Ampel meckert — kurze Netzhänger bleiben ruhig.
STALE_AFTER_S = 120

# Positivliste für laufende Engines (ADR-096 §3.2, Mehrbox-Runtimes): MC
# schickt im Soll `watch` = [{kind, name}], der node-agent meldet für genau
# diese Namen running/stopped/unknown. IDENTISCH zu
# scripts/node-agent/mc-node-agent.py (ANCHOR_KINDS, WATCH_MAX,
# ANCHOR_NAME_MAX, ANCHOR_NAME_CHARS) — Paritätstest in
# tests/test_node_anchor_api.py.
ANCHOR_KINDS = ("container", "process")
WATCH_MAX = 16
ANCHOR_NAME_MAX = 64
ANCHOR_NAME_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
# Namen, die auf JEDER Box laufen — als Anker hiessen sie "running" für immer.
ANCHOR_NAME_DENY = frozenset((
    "sh", "bash", "dash", "zsh", "ash", "fish", "python", "node", "perl", "ruby", "java",
    "env", "sudo", "su", "uv", "uvx", "tmux", "screen", "init", "systemd", "cron",
    "docker", "dockerd", "containerd", "containerd-shim",
    "runc", "ssh", "sshd", "tailscaled", "nvidia-smi", "containerd-shim-runc-v2",
))
ANCHOR_RUNNING = "running"
ANCHOR_STOPPED = "stopped"
ANCHOR_UNKNOWN = "unknown"

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

    Innerhalb von gelb gilt "stale" VOR "pending" (Review M4, 02.09.2026):
    ein Agent, der seit Minuten nichts meldet, wird das Soll auch nicht
    nachziehen. "pending" hiesse "er arbeitet dran" — das wäre gelogen. Die
    Abweichung wird trotzdem mitgegeben (diff), damit die Oberfläche beides
    zeigen kann.
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
    if age_s is None or age_s > STALE_AFTER_S:
        return {"status": STATUS_YELLOW, "reason": "stale", "diff": diff, "age_s": age_s}
    if diff:
        return {"status": STATUS_YELLOW, "reason": "pending", "diff": diff, "age_s": age_s}

    return {"status": STATUS_GREEN, "reason": "in_sync", "diff": [], "age_s": age_s}


def valid_anchor_name(name: object) -> bool:
    """Name eines Ankers: 1..ANCHOR_NAME_MAX Zeichen aus ANCHOR_NAME_CHARS,
    erstes Zeichen Buchstabe/Ziffer, nicht in ANCHOR_NAME_DENY. Gleiche Regel
    wie im node-agent."""
    if not isinstance(name, str) or not (1 <= len(name) <= ANCHOR_NAME_MAX):
        return False
    if name[0] in "_.-":
        return False
    if not all(ch in ANCHOR_NAME_CHARS for ch in name):
        return False
    # Ohne Gross/Klein und ohne Versionsendung: `Python3.12` ist `python`.
    lowered = name.lower()
    return lowered not in ANCHOR_NAME_DENY and lowered.rstrip("0123456789.") not in ANCHOR_NAME_DENY


def anchor_status(
    current: dict[str, Any] | None,
    reported_at: datetime | None,
    kind: str,
    name: str,
    now: datetime | None = None,
) -> str:
    """Läuft dieser Anker? ``running`` / ``stopped`` / ``unknown``.

    Grundsatz (ADR-096 §3.2): **nie ein falsches "running"**. ``unknown``,
    wenn die Box nichts gemeldet hat, die Meldung älter als STALE_AFTER_S
    ist, der Anker nicht in der Meldung steht (alter Agent, noch kein
    ``watch``) oder der Agent selbst "unknown" sagt (z.B. kein Docker-
    Leserecht). Der Leser (Runtime-Status, PR 3) baut darauf ``degraded``.
    """
    now = now or utcnow()
    if not current or reported_at is None:
        return ANCHOR_UNKNOWN
    if (now - ensure_aware(reported_at)).total_seconds() > STALE_AFTER_S:
        return ANCHOR_UNKNOWN
    anchors = current.get("anchors")
    if not isinstance(anchors, list):
        return ANCHOR_UNKNOWN
    for entry in anchors:
        if isinstance(entry, dict) and entry.get("kind") == kind and entry.get("name") == name:
            state = entry.get("state")
            return state if state in (ANCHOR_RUNNING, ANCHOR_STOPPED) else ANCHOR_UNKNOWN
    return ANCHOR_UNKNOWN


def handle_watch_entries(handles: list[str]) -> list[dict[str, str]]:
    """Positivliste für generische Handles: jeder Name als Container UND als
    Prozess.

    MCs Handle-Vertrag (``runtime_manager._ssh_handle_names`` /
    ``anchor_running``): ``process_name`` trägt einen Prozess- ODER einen
    Containernamen, geprüft wird jeder Name auf beide Arten. Wer `watch` aus
    Runtime-Handles baut (PR 6), nimmt diese Funktion — sonst liest ein
    Container-Handle als Prozess für immer "stopped".
    """
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in handles:
        name = (raw or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append({"kind": "container", "name": name})
        out.append({"kind": "process", "name": name})
    return out


def handle_status(
    current: dict[str, Any] | None,
    reported_at: datetime | None,
    names: list[str],
    now: datetime | None = None,
) -> str:
    """Läuft eine Runtime-Engine laut node-agent? Dieselbe ODER-Regel wie
    ``runtime_manager.anchor_running``: jeder Handle wird als Container UND als
    Prozess nachgeschlagen, ein Treffer genügt.

    - ``running``: irgendein Nachschlag sagt running (frische Meldung).
    - ``stopped``: ALLE Nachschläge (beide Arten, alle Handles) sagen stopped.
    - sonst ``unknown`` — auch wenn eine Art fehlt (Schreiber hat nur eine Art
      in `watch` gelegt): "stopped" wäre dann geraten.

    Grenze: ein Anker heisst "ein Container/Prozess dieses Namens läuft", NICHT
    "diese Runtime läuft" — mehrere Rezepte teilen sich Namen. Leser
    kombinieren das mit der Identität, die der Kopf ausliefert.
    """
    lookups: list[str] = []
    for raw in names:
        name = (raw or "").strip()
        if not name:
            continue
        for kind in ANCHOR_KINDS:
            lookups.append(anchor_status(current, reported_at, kind, name, now=now))
    if not lookups:
        return ANCHOR_UNKNOWN
    if ANCHOR_RUNNING in lookups:
        return ANCHOR_RUNNING
    if all(state == ANCHOR_STOPPED for state in lookups):
        return ANCHOR_STOPPED
    return ANCHOR_UNKNOWN
