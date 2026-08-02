"""Die SOUL muss Agenten sagen: Chat-Auftrag → Chat-Antwort.

Live-Befund 01.08.2026 (Movie-Search-Task): Der Auftrag kam im Team-Chat an,
der Agent erledigte ihn sauber und schickte den Report — der landete by design
nur im Reports-Kanal. Im Chat-Thread, wo der Operator gefragt hatte, kam nie
eine Antwort an. Verhalten technisch korrekt (Report-Gate → Reports-Kanal),
aber der Operator musste den Kanal wechseln, um zu erfahren, wie sein eigener
Auftrag ausging.

Die Regel: Kam der Auftrag aus einem Chat-Thread, geht nach `mc report` und
vor `mc finish` zusaetzlich eine kurze Ergebnis-Zusammenfassung per `mc msg`
in genau diese Konversation. Kam der Auftrag vom Board/UI ohne Chat, bleibt
es still — eine Zusammenfassung, nach der niemand gefragt hat, ist Laerm.

Diese Tests nageln die Regel fest, damit ein kuenftiger SOUL-Umbau sie nicht
still wieder verliert (Muster: test_soul_inbox_reply_rule).
"""
import uuid

from app.models.agent import Agent
from app.services.reference_docs_builder import generate_reference_docs
from app.services.template_renderer import build_agent_context, render_agent_file


def _soul(role: str = "developer", comm_v2: bool = True, name: str = "FreeCode") -> str:
    """Rendert die SOUL ueber den Produktions-Template-Pfad."""
    agent = Agent(
        id=uuid.uuid4(),
        name=name,
        role=role,
        board_id=uuid.uuid4(),
        comm_v2=comm_v2,
    )
    ctx = build_agent_context(agent, agents_on_board=[])
    ctx["comm_v2"] = comm_v2
    return render_agent_file("SOUL.md.j2", ctx)


def _rule_section(soul: str) -> str:
    """Der Abschnitt zwischen der Regel-Ueberschrift und der naechsten Headline."""
    start = soul.index("Chat order → chat answer")
    rest = soul[start:]
    end = rest.find("\n**FALLBACK DISCIPLINE")
    return rest if end == -1 else rest[:end]


def test_rule_exists_in_reports_section():
    """Der Kern: die Reports-Sektion lehrt die Chat-Antwort nach dem Report."""
    soul = _soul()
    section = _rule_section(soul)
    assert "mc report" in section
    assert "mc msg" in section
    assert "mc finish" in section


def test_rule_applies_without_comm_v2():
    """Der Ausloeser des Live-Befunds war ein Agent OHNE comm_v2 — die Regel
    darf nicht hinter dem comm_v2-Gate liegen, `mc msg` ist serverseitig
    ungegated."""
    soul = _soul(comm_v2=False)
    assert "Chat order → chat answer" in soul


def test_rule_teaches_thread_targeting():
    """Lebt die Konversation nicht im aktuellen Task-Thread, muss der Agent
    wissen, wie er sie trotzdem erreicht: --thread <id> aus dem Footer."""
    section = _rule_section(_soul())
    assert "--thread" in section
    assert "footer" in section.lower()


def test_rule_says_when_to_stay_silent():
    """Board-/UI-Auftraege ohne Chat-Konversation bekommen KEINE Zusammenfassung —
    sonst erzeugt jede Task-Erledigung einen neuen Slack-Thread (Laerm)."""
    section = _rule_section(_soul()).lower()
    assert "skip" in section
    assert "noise" in section


def test_rule_forbids_report_duplication():
    """Die Chat-Antwort ist kurz und ersetzt den Report nicht — kein
    Scaffolding, kein zweiter Report im Thread."""
    section = _rule_section(_soul()).lower()
    assert "one or two sentences" in section
    assert "scaffolding" in section


def test_orchestrator_owns_the_origin_thread_after_delegation():
    """Boss delegiert und konsolidiert („wer dispatcht, sendet") — dieselbe
    Pflicht gilt fuer die Chat-Antwort: der Ursprungs-Thread des Auftrags
    bekommt die Zusammenfassung von Boss, nicht vom Subtask-Worker."""
    soul = _soul(role="Orchestrator", name="Boss")
    assert "Chat origin → chat answer (consolidation duty)" in soul
    idx = soul.index("consolidation duty")
    window = soul[idx : idx + 700]
    assert "--thread" in window
    assert "origin" in window.lower()


def test_report_doc_teaches_the_same_rule():
    """docs/report.md.j2 ist die Nachschlage-Fassung der Reports-Sektion —
    SOUL und Doku muessen dieselbe Regel lehren, sonst driftet eine von beiden."""
    docs = generate_reference_docs({"operator_name": "TestOp"})
    content = docs["report"]
    assert "Chat order → chat answer" in content
    assert "--thread" in content
    assert "skip" in content.lower()


def test_worker_soul_has_no_orchestrator_duty():
    """Die Konsolidierungspflicht ist Orchestrator-exklusiv — ein Worker, der
    sie liest, wuerde faelschlich in fremde Threads posten."""
    soul = _soul(role="developer")
    assert "consolidation duty" not in soul
