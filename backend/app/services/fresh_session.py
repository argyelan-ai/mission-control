"""Frische Sitzung, fuer die noch keine Datei existiert.

Befund 01.09.2026, live an ``mc-agent-sparky`` nachgemessen: omp legt bei
``/new`` KEINE neue Sitzungsdatei an. Im Terminal steht sofort
``✔ New session started``, auf der Platte passiert nichts — die Datei
entsteht erst mit der ersten Nachricht der neuen Sitzung. Bis dahin ist die
ALTE Datei die neueste, ``find_active_session`` liefert sie weiter, und der
Chat zeigte den alten Verlauf, als waere nichts geschehen (Operator-Befund:
„omp handelt /new nicht korrekt und loescht den Chatverlauf nicht").

Claude Code hat dieses Problem nicht: sein ``/clear`` schreibt einen
Eintrag in eine NEUE Datei, der Rollover greift von selbst.

Dieses Modul haelt pro Agent den Moment, ab dem eine frische Sitzung gilt.
Gesetzt wird er vom Live-Tailer, wenn der Marker im Terminal NEU auftaucht
(``transcript_chat``); gelesen wird er von der Historien-Route — sie muss
leer antworten, solange nur aeltere Dateien existieren, sonst holt der
Refetch nach ``session_changed`` den alten Verlauf sofort zurueck.

Bewusst im Prozess-Speicher: die Luecke ist kurz (bis zur ersten Nachricht)
und ein Backend-Neustart in genau dieser Luecke zeigt schlimmstenfalls den
alten Verlauf noch einmal — die Datenbank dafuer zu bemuehen waere
unverhaeltnismaessig.
"""

from __future__ import annotations

import time
from pathlib import Path

_marks: dict[str, float] = {}


def mark(agent_id: str, at: float | None = None) -> None:
    """Ab ``at`` (Unix-Sekunden, Standard: jetzt) gilt fuer diesen Agenten
    eine frische Sitzung."""
    _marks[str(agent_id)] = time.time() if at is None else at


def is_stale(agent_id: str, session_path: Path) -> bool:
    """Gehoert diese Datei zum ALTEN Gespraech vor der Marke?

    Eine Datei, die nach der Marke geschrieben wurde, IST die neue Sitzung —
    sie verbraucht die Marke, ab dann entscheidet wieder allein die Platte.
    """
    marked_at = _marks.get(str(agent_id))
    if marked_at is None:
        return False
    try:
        mtime = session_path.stat().st_mtime
    except OSError:
        return False
    if mtime >= marked_at:
        _marks.pop(str(agent_id), None)
        return False
    return True


def marked_at(agent_id: str) -> float | None:
    return _marks.get(str(agent_id))


def reset_for_tests() -> None:
    _marks.clear()
