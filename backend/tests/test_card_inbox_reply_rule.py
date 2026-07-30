"""Auch die Operating Card muss die Antwortregel tragen — nicht nur die SOUL.

Vorgeschichte: PR #181 hat die Regel "nach jedem `mc inbox` immer per `mc msg`
in den Thread zurueckantworten" in `SOUL.md.j2` verankert (Vorfall 28.07.2026:
FreeCode antwortete Mark nur auf dem eigenen Bildschirm, Marks Chat blieb
einseitig).

Die Luecke: es gibt einen ZWEITEN Systemprompt-Weg. `CARD.md.j2` (Operating
Card, <=5.5KB statt ~29KB SOUL) wird fuer `use_operating_card`-Agenten gerendert.
Der omp-Harness (Sparky) ist sogar CARD-ONLY — `/opt/omp-bridge/bridge.py`
faellt bewusst NIE auf SOUL.md zurueck. Ohne die Regel in der Card bleiben
genau diese Agenten einseitig.

Die Card hat ein hartes Byte-Budget, die Formulierung ist darum kurz. Diese
Tests nageln den Kern fest — Antwortpflicht, das WARUM, die Folgebewertung —
nicht den Wortlaut.
"""
import uuid

from app.models.agent import Agent
from app.services.template_renderer import build_agent_context, render_agent_file


def _card(comm_v2: bool = True, operator_name: str = "Mark", harness: str = "omp") -> str:
    """Rendert die Operating Card — Muster aus test_operating_card.py."""
    agent = Agent(
        id=uuid.uuid4(),
        name="Sparky",
        role="developer",
        board_id=uuid.uuid4(),
        harness=harness,
        comm_v2=comm_v2,
    )
    ctx = build_agent_context(agent, agents_on_board=[])
    ctx["comm_v2"] = comm_v2
    ctx["operator_name"] = operator_name
    return render_agent_file("CARD.md.j2", ctx)


def test_card_demands_a_reply_into_the_thread():
    """Der Kern: nach jedem `mc inbox` geht eine Antwort per `mc msg` zurueck."""
    card = _card()
    assert "mc inbox" in card
    assert "mc msg" in card
    assert "thread" in card.lower()


def test_card_rule_covers_every_message_not_only_questions():
    """Der eigentliche Fehler von PR #181: die alte Regel galt nur fuer Fragen.
    Eine Begruessung/ein Hinweis muss genauso beantwortet werden."""
    card = _card().lower()
    assert "every message" in card or "not only questions" in card


def test_card_rule_explains_why_screen_output_is_invisible():
    """Ohne das WARUM haelt sich ein Modell nicht dran.

    Absichtlich satz-genau geprueft: "screen" allein steht schon in der
    Verb-Liste (`mc verify` -> screenshots) und "invisible" in der
    Deliverables-Regel — ein blosses `in card` waere gruen ohne die Regel.
    """
    sentences = [s.lower() for s in _card().replace("\n", " ").split(".")]
    assert any(
        "screen" in s and ("invisible" in s or "not your screen" in s)
        for s in sentences
    ), "Die Begruendung (Bildschirmausgabe erreicht den Operator nicht) fehlt"


def test_card_rule_asks_what_the_message_changes():
    """Zweiter Schritt: beurteilen, ob die laufende Arbeit sich aendert
    (Kurskorrektur / Nachricht zu bereits erledigter Aufgabe)."""
    lowered = _card().lower()
    assert "course correction" in lowered or "changes what" in lowered
    assert "finished" in lowered or "closed" in lowered or "reopen" in lowered


def test_card_rule_carries_the_scannable_style():
    """Marks Wunsch 30.07.2026: laengere Chat-Antworten scannbar — Bullets +
    fette Lead-ins statt Textwand. Kurzform in der Card (Byte-Budget), die
    Vollform steht in der SOUL."""
    lowered = _card().lower()
    assert "scannable" in lowered
    assert "bullets" in lowered
    assert "wall of text" in lowered


def test_card_rule_names_the_operator():
    """Die Regel selbst nennt den Operator beim Namen — kein rohes Jinja im Text.

    Der Name wird im Regel-Absatz geprueft, nicht irgendwo in der Card: die
    Kopfzeile enthaelt ihn ohnehin.
    """
    card = _card(operator_name="Zaphod")
    assert "{{" not in card
    rule_block = card.split("`mc inbox` ends with")[1].split("\n\n")[0]
    assert "Zaphod" in rule_block


def test_card_rule_is_absent_without_comm_v2():
    """Ein Agent ohne comm_v2 hat keine Inbox — die Regel waere ein toter
    Verweis auf ein Werkzeug, das er nicht hat (Muster aus der SOUL)."""
    card = _card(comm_v2=False)
    assert "mc inbox" not in card
    assert "mc msg" not in card


def test_card_rule_also_reaches_the_claude_harness():
    """Nicht nur omp: start-claude.sh/start-kimi.sh nutzen CARD.md sobald es
    existiert, mit SOUL.md nur als Fallback."""
    card = _card(harness="claude")
    assert "mc inbox" in card
    assert "mc msg" in card
