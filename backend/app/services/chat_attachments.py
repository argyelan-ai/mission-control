"""Chat-Anhänge — Datei rein, absoluter Pfad raus.

## Warum das funktioniert, ohne ein neues Protokoll zu erfinden

Die CLIs lesen Dateien über Pfade. `~/.mc/references` ist in JEDEN
Agenten-Container unter **exakt demselben absoluten Pfad** gemountet
(`${HOME}/.mc/references:${HOME}/.mc/references:ro`, live an allen 11
Container-Agenten geprüft, 19.08.2026), und Host-Agenten wie Boss lesen den
Host-Pfad ohnehin direkt. Ein Pfad gilt also überall — es gibt keine
Übersetzung pro Agent, keine Kopie, keinen zweiten Speicherort.

Live bewiesen (19.08.2026): Testbild nach `references/_probe/probe.png`
gelegt, FreeCode den Pfad genannt → er hat den Bildinhalt korrekt
wiedergegeben.

## Warum NICHT `reference_ingest`

Der Task-/Slack-Ingest nebenan ist der richtige Ort für Referenzen mit
Besitzer: er führt eine DB-Zeile, begrenzt auf 20 Dateien pro Ziel und lässt
nur eine MIME-Allowlist durch. Für einen laufenden Chat passt keines der
drei (Operator-Entscheid 19.08.2026: „mache für alle Agenten, alle
Dateitypen sollten funktionieren"). Übernommen wird ausschliesslich die
Härtung — Traversal-Guard auf dem ROHEN Namen, Prüfsummen-Präfix,
realpath-Gegenprobe, Grössenlimit. Dessen Allowlist bleibt unangetastet.

## Alle Dateitypen — und warum das sicher ist

Hier wird nichts nach Typ abgewiesen. Die Gefahr, gegen die die Allowlist
nebenan schützt (Review-Fund M1: der Files-Browser liefert Dateien inline
mit Endungs-MIME aus, aktive Inhalte wären damit Stored XSS im App-Origin),
ist an ihrer Wurzel behoben statt hier umschifft: `fs_service.read_stream`
zwingt aktive Typen (HTML/SVG/XML/JS) IMMER in einen Download. Eine
hochgeladene `.html` kann darum nirgends im App-Fenster ausgeführt werden.

Ob ein Agent die Datei dann auch *versteht*, ist bewusst nicht unser
Versprechen — das UI legt ab und nennt den Pfad, mehr behauptet es nicht.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from app.services.reference_ingest import references_root as _references_root

logger = logging.getLogger("mc.chat_attachments")

MAX_BYTES = 25 * 1024 * 1024  # 25 MB — Operator-Entscheid 19.08.2026
RETENTION_DAYS = 30

# Unterbaum innerhalb des references-Roots. Alles Aufräumen bleibt strikt
# hierin — Task-/Projekt-Referenzen gehören der References-API und werden von
# hier NIE angefasst (Lehre: feedback_cleanup_scripts_scope_to_own_ids).
_SUBTREE = "chat"

_IMAGE_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".heif", ".bmp", ".avif"}
)

# Ein Agenten-Slug baut hier ein Verzeichnis. Er kommt zwar aus der DB, wird
# aber trotzdem geprüft — Verteidigung in der Tiefe schlägt Vertrauen auf den
# Aufrufer, und ein Slug ist billig zu validieren.
_SAFE_SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


class ChatAttachmentError(Exception):
    """Ein Grund, warum die Datei nicht angenommen wurde — in Marks Sprache
    formuliert, weil er als HTTP-Detail direkt im UI landet."""


@dataclass(frozen=True)
class StoredAttachment:
    """Was der Composer braucht: wohin die Datei kam und wie sie heisst."""

    path: str      # absoluter Pfad, identisch auf Host UND im Container
    name: str      # Originalname, für die Kachel im Chat
    bytes: int
    is_image: bool


def _sanitize_name(filename: str) -> str:
    """Traversal-Guard auf dem ROHEN Namen, VOR basename.

    Die Reihenfolge ist der Punkt: `os.path.basename("../../etc/passwd")`
    ergibt ein harmlos aussehendes "passwd" und hätte den Angriff still
    geschluckt. Erst prüfen, dann kürzen (Muster aus memory.py, Pitfall 6)."""
    raw = (filename or "").strip()
    if not raw:
        return "datei"
    if ".." in raw or "/" in raw or "\\" in raw or raw.startswith("~"):
        raise ChatAttachmentError(f"Ungültiger Dateiname: {raw!r}")
    name = os.path.basename(raw)
    if not name or name in {".", ".."}:
        raise ChatAttachmentError(f"Ungültiger Dateiname: {raw!r}")
    # NUL-Bytes killen jeden subprocess-Aufruf weiter unten mit einem ValueError
    # (→ 500 statt einer Meldung) — hier abfangen, nicht dort.
    return name.replace("\x00", "")


def _agent_dir(slug: str) -> str:
    if not slug or not _SAFE_SLUG_RE.match(slug):
        raise ChatAttachmentError(f"Ungültiger Agenten-Name: {slug!r}")
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    return os.path.join(_references_root(), _SUBTREE, slug, month)


def store_attachment(*, slug: str, filename: str, contents: bytes) -> StoredAttachment:
    """Legt eine Datei ab und gibt ihren absoluten Pfad zurück.

    Der Name bekommt ein Prüfsummen-Präfix: zwei Screenshots heissen beide
    `Bildschirmfoto.png`, dürfen sich aber nie gegenseitig überschreiben.
    Derselbe Inhalt unter demselben Namen ergibt denselben Pfad — ein
    versehentlich doppelt eingefügter Screenshot verdoppelt nichts."""
    if not contents:
        raise ChatAttachmentError("Die Datei ist leer.")
    if len(contents) > MAX_BYTES:
        raise ChatAttachmentError(
            f"Die Datei ist {len(contents) / (1024 * 1024):.1f} MB — "
            f"maximal {MAX_BYTES // (1024 * 1024)} MB pro Anhang."
        )

    safe_name = _sanitize_name(filename)
    file_dir = _agent_dir(slug)
    os.makedirs(file_dir, exist_ok=True)

    sha = hashlib.sha256(contents).hexdigest()[:16]
    target = os.path.join(file_dir, f"{sha}-{safe_name}")

    # Gegenprobe NACH dem Zusammenbauen: ein Symlink im Pfad könnte uns sonst
    # aus dem Root heraustragen, obwohl jeder einzelne Teil sauber aussah.
    real_dir = os.path.realpath(file_dir)
    real_target = os.path.realpath(target)
    if not real_target.startswith(real_dir + os.sep):
        raise ChatAttachmentError("Pfad verlässt den Anhang-Ordner.")

    if not os.path.exists(target):
        # Erst neben die Zieldatei schreiben, dann umbenennen: ein Abbruch
        # mitten im Schreiben darf nie einen halben Anhang hinterlassen, den
        # der Agent dann liest.
        tmp = f"{target}.part"
        with open(tmp, "wb") as fh:
            fh.write(contents)
        os.replace(tmp, target)

    return StoredAttachment(
        path=target,
        name=safe_name,
        bytes=len(contents),
        is_image=os.path.splitext(safe_name)[1].lower() in _IMAGE_EXTENSIONS,
    )


def cleanup_old_attachments(*, retention_days: int = RETENTION_DAYS) -> int:
    """Entfernt Anhänge, die älter als das Fenster sind; gibt deren Anzahl
    zurück. Fail-silent: Aufräumen darf nie einen Upload scheitern lassen.

    Der Ordner wächst sonst still — MC-Freezes kamen schon einmal von einer
    zu 97 % vollen Platte."""
    base = os.path.join(_references_root(), _SUBTREE)
    if not os.path.isdir(base):
        return 0

    cutoff = time.time() - retention_days * 86400
    removed = 0
    for dirpath, _dirnames, filenames in os.walk(base):
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                if os.path.getmtime(full) < cutoff:
                    os.remove(full)
                    removed += 1
            except OSError:
                logger.warning("chat_attachments: konnte %s nicht entfernen", full, exc_info=True)
    return removed
