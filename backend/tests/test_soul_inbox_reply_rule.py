"""Die SOUL muss Agenten sagen, dass sie in den Thread ZURUECK antworten.

Live-Befund 28.07.2026: Mark schrieb FreeCode aus Telegram an ("Hey @freecode").
Der Agent bekam die Nachricht, las sie mit `mc inbox`, verstand sie korrekt und
antwortete — aber nur auf seinem eigenen Bildschirm:

    ● Kein aktiver Task. Die Nachricht war nur ein kurzes "Hey @freecode"
      Mark, ich bin bereit fuer den naechsten Task. Was soll ich bauen?

Diese Antwort erreichte Mark nie. Die SOUL sagte bis dahin nur "Answer a
QUESTION it contains with mc msg" — eine Begruessung ist technisch keine Frage,
also fuehlte der Agent sich nicht angesprochen, etwas zurueckzuschicken. Ergebnis:
Der Chat war einseitig — Mark erreicht die Agenten, sie ihn nicht.

Diese Tests nageln die Regel fest, damit sie ein kuenftiger Umbau der SOUL nicht
still wieder verliert.
"""
import uuid

import pytest

from app.models.agent import Agent
from app.services.template_renderer import build_agent_context, render_agent_file


def _soul(comm_v2: bool = True, operator_name: str = "Mark") -> str:
    """Rendert die SOUL fuer einen Agenten — Muster aus test_agent_docs_contract."""
    agent = Agent(
        id=uuid.uuid4(),
        name="FreeCode",
        role="developer",
        board_id=uuid.uuid4(),
        comm_v2=comm_v2,
    )
    ctx = build_agent_context(agent, agents_on_board=[])
    ctx["comm_v2"] = comm_v2
    ctx["operator_name"] = operator_name
    return render_agent_file("SOUL.md.j2", ctx)


def test_inbox_rule_demands_a_reply_into_the_thread():
    """Der Kern: nach jedem mc inbox geht eine Antwort zurueck."""
    soul = _soul()
    assert "mc inbox" in soul
    lowered = soul.lower()
    assert "reply into the thread" in lowered or "reply" in lowered
    assert "mc msg" in soul


def test_rule_covers_more_than_questions():
    """Der eigentliche Fehler: die alte Regel galt nur fuer Fragen. Eine
    Begruessung, ein Hinweis, eine Korrektur muessen genauso beantwortet werden."""
    soul = _soul().lower()
    assert "not only questions" in soul or "every message" in soul


def test_rule_explains_why_screen_output_is_invisible():
    """Ohne das WARUM haelt sich ein Modell nicht daran — es muss verstehen,
    dass Bildschirmtext den Operator nicht erreicht."""
    soul = _soul().lower()
    assert "screen" in soul
    assert "invisible" in soul or "not your screen" in soul


def test_rule_handles_course_correction():
    soul = _soul().lower()
    assert "course correction" in soul
    assert "discarded" in soul or "do not finish" in soul


def test_rule_handles_messages_on_finished_tasks():
    """Nachrichten zu erledigten Aufgaben duerfen nicht mit 'ist zu' abgebuegelt
    werden — genau dafuer wurde der Cursor-Bug in PR #150 gefixt."""
    soul = _soul().lower()
    assert "already finished" in soul or "closed" in soul


def test_reply_style_is_scannable_not_prose():
    """Marks Wunsch 30.07.2026: Chat-Antworten scannbar — Bullets, fette
    Lead-ins, kurze Zeilen — statt Fliesstext-Wand. Der Stil ist Teil der
    Antwortregel: eine Antwort, die Mark nicht ueberfliegen kann, ist halb
    verloren."""
    soul = _soul().lower()
    assert "scannable" in soul
    assert "bullets" in soul
    assert "wall of text" in soul


def test_reply_style_separates_chat_from_report():
    """Chat != Bericht: das Update/Evidence/Next-Geruest gehoert in
    Task-Kommentare, nicht in Thread-Antworten."""
    soul = _soul().lower()
    assert "not a report" in soul
    assert "update/evidence/next" in soul


def test_operator_name_is_resolved_not_a_placeholder():
    """Die Regel nennt den Operator beim Namen — kein rohes {{ }} im Text."""
    soul = _soul(operator_name="Mark")
    assert "Mark" in soul
    assert "{{" not in soul


def test_rule_is_absent_without_comm_v2():
    """Ein Agent ohne comm_v2 hat keine Inbox — die Regel waere ein toter
    Verweis auf ein Werkzeug, das er nicht hat."""
    soul = _soul(comm_v2=False)
    assert "mc inbox" not in soul
