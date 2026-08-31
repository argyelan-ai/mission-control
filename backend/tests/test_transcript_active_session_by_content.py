"""Die aktive Sitzung wird am INHALT erkannt, nicht am Datei-mtime.

Hintergrund (Operator-Befund 31.08.2026, Boss): MC zeigte einen 11 Tage alten
Chat. Ursache: eine alte Sitzungsdatei war beruehrt worden (mtime frisch) ohne
neuen Gespraechs-Eintrag und verdeckte damit die tatsaechlich laufende Sitzung.
Der mtime ist als Aktivitaets-Signal untauglich — der Zeitstempel des letzten
Eintrags ist die einzige Quelle, die sagt, wann zuletzt gesprochen wurde.

Fixtures sind getrimmte, neutralisierte Kopien des Claude-Code-Schemas.
"""
from __future__ import annotations

import json
import os
import time

from app.services import transcript_chat as tc


def _write_session(tdir, name: str, last_iso: str, *, mtime: float | None = None):
    """Schreibt eine Sitzungsdatei mit definiertem letztem Eintrag + optionalem mtime."""
    path = tdir / f"{name}.jsonl"
    lines = [
        {"type": "user", "uuid": f"{name}-1", "timestamp": last_iso,
         "message": {"role": "user", "content": "hallo"}},
        {"type": "assistant", "uuid": f"{name}-2", "timestamp": last_iso,
         "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}},
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_beruehrte_alte_datei_verdeckt_die_laufende_sitzung_nicht(tmp_path):
    """Kern des Bugs: alte Datei hat den FRISCHEREN mtime, aber alten Inhalt."""
    jetzt = time.time()
    alt = _write_session(tmp_path, "alt", "2026-08-20T20:43:59.000Z", mtime=jetzt)      # beruehrt!
    aktuell = _write_session(tmp_path, "aktuell", "2026-08-31T08:51:00.000Z", mtime=jetzt - 600)

    result = tc.find_active_session(tmp_path)

    assert result is not None
    path, meta = result
    assert path == aktuell, "die Sitzung mit dem juengsten INHALT muss gewinnen"
    assert meta["sessionId"] == "aktuell"
    assert path != alt


def test_ohne_beruehrung_gewinnt_weiterhin_die_juengste_sitzung(tmp_path):
    """Regressionsschutz: der Normalfall bleibt unveraendert."""
    jetzt = time.time()
    _write_session(tmp_path, "aelter", "2026-08-30T10:00:00.000Z", mtime=jetzt - 3600)
    neuer = _write_session(tmp_path, "neuer", "2026-08-31T10:00:00.000Z", mtime=jetzt)

    result = tc.find_active_session(tmp_path)

    assert result is not None
    assert result[0] == neuer


def test_datei_ohne_lesbaren_zeitstempel_faellt_auf_mtime_zurueck(tmp_path):
    """Fremde/kaputte Formate duerfen nicht unsichtbar werden."""
    jetzt = time.time()
    kaputt = tmp_path / "kaputt.jsonl"
    kaputt.write_text("das ist kein json\n{ebenfalls nicht}\n", encoding="utf-8")
    os.utime(kaputt, (jetzt, jetzt))
    _write_session(tmp_path, "mit_inhalt", "2026-08-20T10:00:00.000Z", mtime=jetzt - 7200)

    result = tc.find_active_session(tmp_path)

    assert result is not None
    # kaputt hat den frischeren mtime und keinen Inhalts-Zeitstempel -> mtime zaehlt
    assert result[0] == kaputt


def test_last_entry_timestamp_liest_nur_das_dateiende(tmp_path):
    """Grosse Dateien: es wird nur der Schwanz gelesen, der Wert stimmt trotzdem."""
    path = tmp_path / "gross.jsonl"
    fuell = {"type": "system", "uuid": "x", "timestamp": "2026-01-01T00:00:00.000Z",
             "content": "x" * 500}
    letzte = {"type": "assistant", "uuid": "letzte", "timestamp": "2026-08-31T12:00:00.000Z",
              "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}}
    with path.open("w", encoding="utf-8") as fh:
        for _ in range(400):  # deutlich groesser als das gelesene Ende
            fh.write(json.dumps(fuell) + "\n")
        fh.write(json.dumps(letzte) + "\n")

    ts = tc.last_entry_timestamp(path)

    assert ts is not None
    assert time.strftime("%Y-%m-%d", time.gmtime(ts)) == "2026-08-31"


def test_last_entry_timestamp_ohne_zeitstempel_ist_none(tmp_path):
    leer = tmp_path / "leer.jsonl"
    leer.write_text("", encoding="utf-8")
    assert tc.last_entry_timestamp(leer) is None


def test_last_entry_timestamp_ueberspringt_eintraege_ohne_zeitstempel(tmp_path):
    """Die letzte Zeile kann ein Eintrag ohne timestamp sein — dann zaehlt die davor."""
    path = tmp_path / "gemischt.jsonl"
    lines = [
        {"type": "assistant", "uuid": "a", "timestamp": "2026-08-31T09:00:00.000Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}},
        {"type": "summary", "uuid": "b", "summary": "kein timestamp hier"},
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")

    ts = tc.last_entry_timestamp(path)

    assert ts is not None
    assert time.strftime("%H", time.gmtime(ts)) == "09"
