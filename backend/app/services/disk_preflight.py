"""Plattenplatz-Preflight fuer Bau-Wege, die im Backend-Prozess laufen.

Warum es das gibt (gemessen, 2026-09-16): die Platte lief voll. `docker system
df` zeigte 75,8 GB Build-Cache — Docker raeumt den von sich aus nie auf.
`docker compose up --build` starb mitten im Layer-Schreiben mit einem rohen
"no space left on device", nach Minuten Arbeit, und der einzige Anhaltspunkt
war der rohe Docker-Fehler.

Dieses Modul ist die Python-Seite derselben Pruefung, die
``docker/shared/disk-preflight.sh`` fuer die Shell-Seite macht. Beide lesen
dieselbe Schwelle aus derselben Quelle (``build_min_free_gb`` /
``MC_BUILD_MIN_FREE_GB``) — zwei Implementierungen, die auseinanderdriften
koennten, sind genau die Bug-Klasse, die dieses Projekt schon einmal hatte;
deshalb hier die Formel, dort die Formel, aber dieselbe Zahl und dieselbe
Richtung (unten == verweigern).

Zwei Regeln:

* **Unlesbar heisst unbekannt, nicht null.** Laesst sich der freie Platz nicht
  ermitteln, laeuft der Bau weiter (mit Warnung). Eine Verweigerung auf Basis
  einer erfundenen Zahl waere schlimmer als das Problem, das sie verhindert.
* **Ein unbekannter Wert darf keine Zahl erfinden** — dieselbe Regel wie
  ``recipe_install._parse_free_gb``.
"""

from __future__ import annotations

import logging
import subprocess

from app.config import settings

logger = logging.getLogger("mc.disk_preflight")

# `df` darf den Bau nie haengen lassen. Ein haengendes `df` (haengendes NFS-
# Mount) waere schlimmer als kein Preflight: der Bau haette ohnehin nicht
# laufen koennen, aber jetzt wartet der Aufrufer stattdessen.
DF_TIMEOUT_S = 10.0


def parse_free_gb(df_output: str) -> float | None:
    """Freie GB aus ``df -Pk <pfad>``. None, wenn nicht lesbar.

    Nimmt die LETZTE Zeile: ``df -Pk`` gibt genau eine Datenzeile aus, aber
    haengt bei manchen Filesystemen eine Zeile nach — und die letzte ist die
    ausgewertete. ``parts[3]`` ist `Avail` (POSIX-Spalte 4).
    """
    lines = [ln for ln in df_output.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    parts = lines[-1].split()
    if len(parts) < 4:
        return None
    try:
        return round(int(parts[3]) / (1024 * 1024), 1)
    except ValueError:
        return None


def free_gb(path: str = "/") -> float | None:
    """Freier Platz in GB an ``path``, oder None wenn unlesbar.

    ``df -Pk`` ist POSIX — ``df -h --output=avail`` ist GNU-only und stirbt auf
    BSD/macOS (dieselbe Begruendung wie ``services/host_probe.py``).
    """
    try:
        proc = subprocess.run(
            ["df", "-Pk", path],
            capture_output=True,
            text=True,
            timeout=DF_TIMEOUT_S,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        logger.warning("df -Pk %s nicht ausfuehrbar: %s", path, e)
        return None
    if proc.returncode != 0:
        logger.warning("df -Pk %s fehlgeschlagen: %s", path, proc.stderr[:200])
        return None
    return parse_free_gb(proc.stdout)


def build_preflight_error(path: str = "/") -> str | None:
    """Meldung fuer den Betreiber, oder None wenn gebaut werden darf.

    Dasselbe Vertragsmuster wie ``docker_agent_sync.compose_preflight_error``:
    ein String heisst "nicht bauen, das hier anzeigen", None heisst "weiter".
    """
    minimum = settings.build_min_free_gb
    available = free_gb(path)

    if available is None:
        # Unbekannt ist nicht null — siehe Modul-Docstring.
        logger.warning(
            "Plattenplatz an %s nicht lesbar — Bau laeuft weiter (Pruefung uebersprungen)",
            path,
        )
        return None

    if available < minimum:
        # The keep bound comes from settings, not a literal: the operator edits
        # BUILD_CACHE_KEEP_GB, and a hardcoded number here would start advising
        # a prune that contradicts their own configuration — the same
        # config-drift bug the watchdog note is tested against.
        keep = settings.build_cache_keep_gb
        return (
            f"Zu wenig Plattenplatz: {available} GB frei an {path}, "
            f"noetig sind {minimum} GB (Schwelle BUILD_MIN_FREE_GB). "
            "Ein Bau ohne Platz stirbt mitten im Layer-Schreiben mit einem "
            "rohen \"no space left on device\". "
            f"Fix: docker builder prune --keep-storage {keep}g, "
            "oder die Schwelle senken (BUILD_MIN_FREE_GB in .env)."
        )

    logger.debug("Plattenplatz-Preflight: %.1f GB frei (Schwelle %d GB)", available, minimum)
    return None


def build_cache_cleanup() -> None:
    """Build-Cache nach einem ERFOLGREICHEN Bau unter die Obergrenze stutzen.

    Nur nach Erfolg aufrufen: ein Cache-Wipe nach einem FEHLGESCHLAGENEN Bau
    zerstoert genau den Layer-Cache, den der Wiederholungsversuch braucht.

    Wirft nie — ein Cache-Problem nach einem gruenen Bau ist kein Baufehler,
    und daraus einen zu machen wuerde einen Bau scheitern lassen, der sein
    Image bereits produziert hat.
    """
    keep = settings.build_cache_keep_gb
    try:
        proc = subprocess.run(
            ["docker", "builder", "prune", "--force", "--keep-storage", f"{keep}g"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as e:  # noqa: BLE001 — best-effort, siehe Docstring
        logger.warning("Build-Cache-Aufraeumen fehlgeschlagen: %s", e)
        return
    if proc.returncode != 0:
        logger.warning("docker builder prune fehlgeschlagen: %s", proc.stderr[:200])
    else:
        logger.info("Build-Cache auf %d GB begrenzt", keep)
