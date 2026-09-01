"""Den Terminal-Strom eines Agenten an- und abschalten (Live-Schicht).

``tmux pipe-pane`` schreibt alles, was in einem Pane erscheint, fortlaufend in
eine Datei — Push statt Abfrage. Die Datei liegt im Container unter
``/home/agent/.claude``, und dieses Verzeichnis ist bei JEDEM Agenten-Container
auf den Host gemountet (live geprueft fuer claude, omp und kimi:
``~/.mc/agents/<slug>/claude-config`` → ``/home/agent/.claude``).

Damit liest das Backend den Strom mit derselben Mechanik wie ein Transkript:
Byte-Offsets, Datei-I/O im Thread, Fehler pro Durchlauf abgefangen. Kein neuer
Transportweg, kein zusaetzlicher Dienst, keine zweite WebSocket-Kette.

Der Strom ist die Kuer, das Transkript die Pflicht: schlaegt hier irgendetwas
fehl, faellt die Vorschau aus — der Chat selbst darf davon nichts merken.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger("mc.pane_stream")

#: Wurzel der Agenten-Verzeichnisse auf dem HOST. ``HOME_HOST`` ist Pflicht:
#: im Backend-Container zeigt ``~`` auf ``/home/mcuser``, der Docker-Daemon
#: aber braucht den Pfad des Hosts.
AGENTS_ROOT = Path(os.environ.get("HOME_HOST") or os.path.expanduser("~")) / ".mc" / "agents"

#: Im Container: /home/agent/.claude/<STREAM_FILENAME>
STREAM_FILENAME = ".mc-pane-stream.log"
CONTAINER_STREAM_PATH = f"/home/agent/.claude/{STREAM_FILENAME}"

_TIMEOUT_SECONDS = 10


def _slug(agent) -> str | None:
    slug = getattr(agent, "slug", None)
    if slug:
        return slug
    name = getattr(agent, "name", None)
    return name.lower().replace(" ", "-") if name else None


def stream_path_for(agent) -> Path | None:
    """Host-Pfad der Strom-Datei — oder None, wenn dieser Agent keine hat.

    Host-Agenten (Boss) laufen nicht in einem Container mit diesem Mount; fuer
    sie gibt es keinen Strom und damit keine Vorschau.
    """
    if getattr(agent, "agent_runtime", None) != "cli-bridge":
        return None
    slug = _slug(agent)
    if not slug:
        return None
    return AGENTS_ROOT / slug / "claude-config" / STREAM_FILENAME


def _docker_argv(slug: str, *tmux_args: str) -> list[str]:
    # Gleiche env/user-Flags wie capture_pane und agent_chat_input: eine
    # falsche Locale zerlegt mehrbyte Zeichen.
    return [
        "docker", "exec", "-e", "LANG=C.UTF-8", "-u", "agent",
        f"mc-agent-{slug}",
        "tmux", *tmux_args,
    ]


def _run(argv: list[str]) -> bool:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 — Docker weg, Timeout, alles gleich
        logger.warning("pane_stream: %s fehlgeschlagen: %s", argv[:8], exc)
        return False
    if proc.returncode != 0:
        logger.warning("pane_stream: %s -> rc=%s %s", argv[:8], proc.returncode, proc.stderr.strip())
        return False
    return True


async def start(agent) -> Path | None:
    """Schaltet den Strom ein und gibt den Host-Pfad zurueck.

    Die Datei wird vorher geleert: sonst liest der erste Poll die Reste der
    letzten Sitzung als frischen Text — derselbe Fehler, den der
    Transkript-Tailer schon einmal gemacht hat (Historie als 'live' ausgegeben).
    """
    path = stream_path_for(agent)
    slug = _slug(agent)
    if path is None or slug is None:
        return None

    def _prepare() -> bool:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"")
        except OSError as exc:
            logger.warning("pane_stream: %s nicht schreibbar: %s", path, exc)
            return False
        return _run(_docker_argv(slug, "pipe-pane", "-t", f"{slug}:0", "-O", f"cat >> {CONTAINER_STREAM_PATH}"))

    ok = await asyncio.to_thread(_prepare)
    return path if ok else None


async def pane_size(agent) -> tuple[int, int]:
    """Breite und Hoehe der Pane — oder 80x24, wenn tmux nicht antwortet.

    Der Emulator muss so breit sein wie die echte Pane: mit 80 Spalten brach
    jede Zeile von Sparkys 168 breiter Pane bei Zeichen 80 ab (Live-Gate
    01.09.2026). Ein Fehler ist nie fatal — dann ist die Vorschau nur schmal.
    """
    slug = _slug(agent)
    if slug is None:
        return 80, 24

    def _query() -> tuple[int, int]:
        argv = _docker_argv(slug, "display", "-p", "-t", f"{slug}:0", "#{pane_width}x#{pane_height}")
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=_TIMEOUT_SECONDS)
            width, height = proc.stdout.strip().split("x")
            cols, rows = int(width), int(height)
            if cols > 0 and rows > 0:
                return cols, rows
        except Exception as exc:  # noqa: BLE001 — Docker weg, Timeout, Murks in stdout
            logger.warning("pane_stream: Pane-Groesse von %s unbekannt: %s", slug, exc)
        return 80, 24

    return await asyncio.to_thread(_query)


async def stop(agent) -> None:
    """Schaltet den Strom ab.

    ``pipe-pane`` OHNE Kommando ist der Ausschalter. Mit Kommando liefe ein
    zweiter Schreiber weiter und die Datei wuechse ohne Zuschauer.
    """
    slug = _slug(agent)
    if slug is None:
        return
    await asyncio.to_thread(_run, _docker_argv(slug, "pipe-pane", "-t", f"{slug}:0"))
