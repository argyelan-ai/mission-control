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


# ── Bildschirmgroesse (Live-Gate 01.09.2026) ────────────────────────────────


def test_screen_takes_the_real_pane_size():
    """Live gesehen: Sparkys Pane ist 168x45, der Emulator rechnete mit 80 —
    jede Zeile brach bei Zeichen 80 ab ("…ins Lan")."""
    long_line = "Ein Fjord ist ein langer, schmaler und tiefer Meeresarm, der sich tief ins Land hineinzieht und dort endet."
    assert len(long_line) > 80
    preview = PanePreview(cols=168, rows=45)
    preview.feed(f"● {long_line}\r\n")
    assert f"● {long_line}" in preview.text().splitlines()
    assert len(PanePreview().text().splitlines()) == 0  # Gegenprobe: 80 Spalten wuerden umbrechen
    narrow = PanePreview()
    narrow.feed(f"● {long_line}\r\n")
    assert f"● {long_line}" not in narrow.text().splitlines()


def test_fresh_copy_keeps_the_size():
    """Wird die Strom-Datei geleert, faengt der Emulator von vorn an — mit
    derselben Groesse, nicht wieder mit 80x24."""
    fresh = PanePreview(cols=168, rows=45).fresh()
    assert (fresh.cols, fresh.rows) == (168, 45)


def test_anchor_matches_a_wrapped_line_by_its_tail():
    """Eine lange Transkript-Zeile steht im Terminal umgebrochen ueber mehrere
    Bildschirmzeilen. Der Schnitt muss HINTER der letzten davon liegen, sonst
    tropft der Rest der alten Antwort in die Vorschau."""
    anchor = "Bekannte Beispiele sind der Sognefjord in Norwegen, der mit ueber 1.300 Metern Tiefe einer der tiefsten der Welt ist."
    preview = PanePreview(cols=40, rows=24)
    preview.feed(f"● {anchor}\r\n\r\n● Ein Gletscher ist eine Eismasse.\r\n")
    assert preview.text_after(anchor) == "● Ein Gletscher ist eine Eismasse."


def test_steering_box_of_queued_operator_text_is_furniture():
    """omp zeigt eingereihte Nachrichten in einer 'Steering · N'-Box — das ist
    das Echo des Operators, nie Inhalt (Echo-Regel im Skill)."""
    preview = PanePreview()
    preview.feed(
        "● Antwort laeuft noch.\r\n"
        "Steering · 2\r\n"
        "1. Und jetzt bitte in 5 Saetzen: was ist ein Gletscher?\r\n"
        "2. hallo nochmal\r\n"
        "1. Ein echter Listenpunkt danach bleibt.\r\n"
    )
    assert preview.text() == "● Antwort laeuft noch.\n1. Ein echter Listenpunkt danach bleibt."


def test_anchor_whose_tail_wraps_across_two_screen_lines_still_cuts_after_it():
    """Live 02.09.2026: der Schwanz des Ankers lag ueber einem Umbruch
    ('…sowie der' | 'Kilauea auf Hawaii.') — keine Zeile enthielt ihn, der
    Schnitt fiel auf den KOPF in Zeile 1, und die Vorschau wiederholte die
    fertige Antwort ab Zeile 2."""
    anchor = "Beruehmte Beispiele sind der Aetna und Vesuv in Italien, der Fuji in Japan sowie der Kilauea auf Hawaii."
    preview = PanePreview(cols=168, rows=24)
    # omp bricht selbst an Wortgrenzen um — die Zeilen kommen fertig geteilt.
    preview.feed(
        "Beruehmte Beispiele sind der Aetna und Vesuv in Italien, der Fuji in Japan sowie der\r\n"
        "Kilauea auf Hawaii.\r\n"
    )
    assert preview.text_after(anchor) == ""


def test_italic_grey_thinking_summary_is_not_content():
    """omp zeigt die Denk-Zusammenfassung kursiv+grau — kein Antworttext.

    Live-Gate 02.09.2026: "Same format as before, German, 4 sentences." stand
    in drei Vorschau-Events, in der fertigen Antwort aber nie.
    """
    p = PanePreview(80, 10)
    p.feed(
        "\x1b[3;38;2;156;163;176mSame format as before, German, 4 sentences.\x1b[39;23m\r\n"
        "\r\n"
        "Ein Geysir ist eine heisse Quelle, die Wasser ausstoesst.\r\n"
    )
    assert p.text() == "Ein Geysir ist eine heisse Quelle, die Wasser ausstoesst."


def test_a_single_italic_word_inside_a_normal_line_stays():
    """Sabotage-Gegenprobe: nur GANZ kursive Zeilen sind Denk-Zeilen —
    ein kursives Wort mitten im Satz bleibt Inhalt."""
    p = PanePreview(80, 10)
    p.feed("Das ist \x1b[3mwirklich\x1b[23m wichtig, sagte er laut.\r\n")
    assert p.text() == "Das ist wirklich wichtig, sagte er laut."


def test_a_lone_spinner_glyph_is_furniture():
    p = PanePreview(80, 10)
    p.feed("Ein Anfang der Antwort.\r\n\r\n ⠼\r\n")
    assert p.text() == "Ein Anfang der Antwort."


def test_delivery_echo_typed_by_poll_sh_is_furniture():
    """poll.sh tippt „📬 Neue Nachrichten (bis seq N, t) — lies sie jetzt mit:
    mc inbox" in die Pane. Das ist Maschinen-Zustellung, kein Text des Agenten —
    im Gruppenraum stünde er sonst als erste Zeile jeder Vorschau."""
    pv = PanePreview()
    pv.feed(
        b"> \xf0\x9f\x93\xac Neue Nachrichten (bis seq 42, 1756800000) \xe2\x80\x94 lies sie jetzt mit: mc inbox\r\n"
        b"Ich lese die Inbox.\r\n"
    )
    assert pv.text() == "Ich lese die Inbox."


# ── Live-Gate A (Gruppenraum, 02.09.2026): Rahmen der beiden Panes ─────────

def test_claude_code_paste_marker_in_the_composer_is_furniture():
    """poll.sh fügt den Auftrag in Claude Codes Eingabezeile ein; die zeigt
    dafür „[Pasted text #1 +13 lines]" und „paste again to expand". Beides
    stand als Vorschau eines Claude-Code-Mitglieds im Gruppenraum — es ist Maschinen-Zustellung."""
    p = PanePreview(80, 10)
    p.feed("u\r\n[Pasted text #1 +13 lines]\r\npaste again to expand\r\nIch fange an.\r\n")
    assert p.text() == "u\nIch fange an."


def test_claude_code_tool_status_line_and_lone_bullet_are_furniture():
    """„● Running 1 shell command…" ist die Werkzeug-Statuszeile, ein einsames
    „●" der Platzhalter der kommenden Antwort — kein Inhalt."""
    p = PanePreview(80, 10)
    p.feed("● Running 1 shell command…\r\n●\r\n● Die Antwort beginnt hier.\r\n")
    assert p.text() == "● Die Antwort beginnt hier."


def test_omp_update_banner_and_nudge_echo_are_furniture():
    """Nach einem Frischstart zeigt omp sein Update-Banner; die Bridge tippt
    „@/home/agent/.msg-nudge.msg" ein. Beides stand als Vorschau eines omp-Mitglieds."""
    p = PanePreview(80, 10)
    p.feed(
        "Update Available\r\nNew version 18.1.2 is available. Run: omp update\r\n"
        "@/home/agent/.msg-nudge.msg\r\nIch lese die Inbox.\r\n"
    )
    assert p.text() == "Ich lese die Inbox."
