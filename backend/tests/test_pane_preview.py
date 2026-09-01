"""Vorschau aus dem Terminal-Strom — der Extraktor (Live-Schicht, P1).

Die Fixtures sind echte ``tmux pipe-pane``-Mitschnitte von laufenden Agenten
(01.09.2026), die ``.expected.txt`` daneben ist der Text derselben Antwort aus
dem Transkript — also die Wahrheit, gegen die die Vorschau antritt.

Der Vertrag ist bewusst NICHT "zeichengleich": das Terminal bricht Zeilen hart
um und zeichnet Markdown bereits gerendert (ein Codeblock hat dort keine
Backticks). Geprueft wird darum: der Inhalt ist vollstaendig da, und nichts vom
Rahmen der Oberflaeche rutscht mit hinein.
"""
import re
from difflib import SequenceMatcher
from pathlib import Path

import pytest

from app.services.pane_preview import PanePreview

FIXTURES = Path(__file__).parent / "fixtures" / "pane_streams"


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _coverage(got: str, expected: str) -> float:
    """Anteil der Wahrheit, der sich in der Vorschau wiederfindet.

    ``autojunk=False`` ist Pflicht: SequenceMatcher ignoriert ab 200 Zeichen
    haeufige Zeichen und meldete in der Machbarkeitsprobe 81 % statt 99,8 % —
    ein kaputtes Messwerkzeug, keine kaputten Daten.
    """
    got, expected = _norm(got), _norm(re.sub(r"```[a-z]*", "", expected))
    if not expected:
        return 0.0
    blocks = SequenceMatcher(None, got, expected, autojunk=False).get_matching_blocks()
    return sum(b.size for b in blocks) / len(expected)


def _feed(name: str) -> PanePreview:
    preview = PanePreview()
    preview.feed((FIXTURES / f"{name}.raw").read_bytes())
    return preview


@pytest.mark.parametrize(
    "name",
    ["claude_code_prose", "claude_code_list_and_code", "omp_prose"],
)
def test_preview_contains_the_whole_answer(name):
    expected = (FIXTURES / f"{name}.expected.txt").read_text()
    got = _feed(name).text()
    assert _coverage(got, expected) >= 0.95, f"nur {_coverage(got, expected):.1%} wiedergefunden"


@pytest.mark.parametrize(
    "name",
    ["claude_code_prose", "claude_code_list_and_code", "omp_prose"],
)
def test_preview_carries_no_interface_furniture(name):
    got = _feed(name).text()
    for junk in ("bypass permissions", "esc to interrupt", "for agents", "Working…", "⟦esc⟧"):
        assert junk not in got, f"Rahmenelement {junk!r} steht in der Vorschau"
    assert "─────" not in got and "╭" not in got and "╰" not in got


def test_preview_shows_the_answer_while_it_is_still_being_written():
    """Live-Tauglichkeit: halb eingespielt steht der Anfang der Antwort schon da.

    Kein PRAEFIX im strengen Sinn, und das ist eine Eigenschaft des Terminals:
    beim Umbrechen zeichnet die CLI ihren Absatz neu. Friert man den Strom an
    einer beliebigen Byte-Grenze ein — genau das tut ein Poll —, kann eine
    Zeile halb ueberschrieben dastehen und ein Stueck Text doppelt zeigen. Mit
    dem naechsten Zufluss ist es weg.

    Geprueft wird deshalb: der Anfang der echten Antwort ist frueh sichtbar,
    der Text waechst, und am Ende steht die Antwort vollstaendig da. Die
    Daempfung dieses kurzen Flackerns (erst senden, wenn zwei Messungen
    dasselbe zeigen) gehoert in die Sende-Politik, nicht in diesen Extraktor.
    """
    raw = (FIXTURES / "claude_code_prose.raw").read_bytes()
    truth = _norm((FIXTURES / "claude_code_prose.expected.txt").read_text())
    anchor = "Schreibe genau vier Saetze ueber Gebirgsbaeche"

    live = PanePreview()
    live.feed(raw[: len(raw) // 2])
    early = _norm(live.text_after(anchor))
    live.feed(raw[len(raw) // 2 :])
    complete = _norm(live.text_after(anchor))

    assert truth[:80] in early, "der Anfang der Antwort war auf halbem Weg noch nicht zu sehen"
    assert len(early) < len(complete), "der Text ist nicht gewachsen"
    assert _coverage(complete, truth) >= 0.95


def test_anchor_cuts_off_everything_from_earlier_turns():
    """``text_after`` liefert NUR das Neue.

    Der Bildschirm traegt auch aeltere Zuege. Ohne den Schnitt am Anker landete
    eine alte Antwort als Vorschau unter der NEUEN Nachricht — der denkbar
    schlimmste Anzeigefehler, weil er nicht nach einem Fehler aussieht.

    Gebauter Strom statt Mitschnitt: die echten Aufzeichnungen beginnen jeweils
    mit einem frischen Bildschirm und haben gar keinen aelteren Zug, koennten
    das also nie nachweisen.
    """
    preview = PanePreview()
    preview.feed(
        "Das ist die Antwort auf eine viel aeltere Frage von gestern.\r\n"
        "❯ Wie hoch ist der Uetliberg?\r\n"
        "● Der Uetliberg ist 869 Meter hoch und liegt bei Zuerich.\r\n"
    )

    only_new = preview.text_after("Wie hoch ist der Uetliberg?")

    assert "869 Meter" in only_new
    assert "gestern" not in only_new, "der aeltere Zug steht in der Vorschau"


def test_anchor_without_a_match_returns_everything():
    """Kein Treffer heisst: lieber zu viel zeigen als eine Antwort verschlucken."""
    preview = _feed("claude_code_prose")
    assert preview.text_after("diese Zeile stand dort nie") == preview.text()


def test_empty_stream_yields_no_preview():
    assert PanePreview().text() == ""
