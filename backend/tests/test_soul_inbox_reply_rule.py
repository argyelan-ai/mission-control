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


def test_reply_style_threshold_is_sharp():
    """Messung 31.07.2026: die alte Schwelle "one or two sentences -> plain
    text" war ein Schlupfloch — 2-4-Satz-Antworten lasen sich legitim als
    Fliesstext. Neue Schwelle: EIN kurzer Satz darf plain sein, ab zwei
    Saetzen oder sobald etwas Aufzaehlbares drinsteckt -> Bullets."""
    soul = _soul().lower()
    assert "one short sentence" in soul
    assert "enumerable" in soul
    # Das alte Schlupfloch ("One or two sentences -> plain text") darf nicht
    # wieder reinrutschen. "One or two sentences are plenty" (Antwortlaenge
    # bei Acks) ist ein anderer, legitimer Satz — darum der volle Wortlaut:
    assert "one or two sentences → plain text" not in soul


def test_reply_style_survives_without_comm_v2():
    """Messung 31.07.2026: die Stilregel stand NUR im comm_v2-Block — Agenten
    ohne comm_v2 (Downloader, Shakespeare) chatten aber real ueber Slack mit
    Mark und hatten die Regel GAR NICHT in der ausgelieferten SOUL. Sie muss
    ungegated drinstehen — aber ohne tote comm_v2-Werkzeug-Referenzen."""
    soul = _soul(comm_v2=False).lower()
    assert "scannable" in soul
    assert "bullets" in soul
    assert "wall of text" in soul
    assert "one short sentence" in soul
    assert "enumerable" in soul
    assert "update/evidence/next" in soul


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


def test_reply_targets_the_source_thread():
    """Live-Vorfall 03.08.2026: Agent las die Operator-Nachricht NACH mc finish
    (Nudge kommt erst an der Turn-Grenze), antwortete mit nacktem `mc msg` —
    und die Antwort fiel in den DM-Thread zurueck (kein aktiver Task mehr),
    waehrend der Operator im Task-Thread wartete. Die Regel muss das explizite
    Thread-Targeting mit der Footer-ID lehren."""
    soul = _soul()
    assert "mc msg --thread <id>" in soul
    lowered = soul.lower()
    assert "came from" in lowered
    assert "footer" in lowered
    # Das WARUM: nackter mc msg faellt auf Task-/DM-Thread zurueck.
    assert "dm thread" in lowered
    # Der konkrete Fehlermodus: Nachricht zum gerade geschlossenen Task.
    assert "mc finish" in soul


def test_reply_rule_kills_the_redelivery_excuse():
    """Der Agent tat die Operator-Nachricht als 'Redelivery des Briefings' ab
    und liess sie unbeantwortet im falschen Thread versanden. Eine Nachricht
    mit Thread-Footer ist von einem Menschen in einen Thread geschrieben —
    das muss die SOUL explizit sagen. (Nicht auf das blosse Wort 'redelivery'
    pruefen — das steht schon im at-least-once-Absatz der Delivery-Doku.)"""
    soul = _soul().lower()
    assert "just a redelivery" in soul
    assert "a human wrote it into a thread" in soul


def test_tools_md_teaches_thread_targeting():
    """TOOLS.md ist die Referenz-Doku der beiden Verben — sie muss dasselbe
    Targeting lehren wie die SOUL, sonst driften Regel und Referenz."""
    from app.services.tools_md_builder import generate_tools_md

    tools_md = generate_tools_md(
        name="TestAgent", emoji="🤖", raw_token="tok", board_id="board-uuid-123",
        is_board_lead=False, comm_v2=True,
    )
    assert "mc msg --thread <id>" in tools_md
    assert "footer" in tools_md.lower()
