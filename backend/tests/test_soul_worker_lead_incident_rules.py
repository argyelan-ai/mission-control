"""Regeln aus den Vorfaellen vom 07./08.09.2026 — SOUL/Delegations-Doku.

Fuenf belegte Vorfaelle, fuenf Regeln. Die Tests nageln sie fest, damit ein
kuenftiger Umbau der Templates sie nicht still wieder verliert:

1. Ein Worker fuhr nach `mc finish` noch 40-min-Test-Suiten im Hintergrund und
   hielt seine Session ueber eine Stunde — das Deploy-Fenster war blockiert.
2. Ein Worker stellte seine Rueckfrage als Terminal-Dialog der eigenen CLI und
   stand eine Stunde still; der Watchdog hielt ihn fuer lebendig, weil der
   Tool-Heartbeat weiterlief.
3. `tmux kill-server` in einer Probe toetete die eigene Session des Workers.
4. Der Lead setzte den Worker-Subtask selbst auf `done` und dispatchte sofort
   die naechste Karte — sie landete im laufenden Zug (70-min-Stall).
5. Delegations-Karten mit 4 885-7 136 Zeichen wurden beschnitten; der
   Prompt-Builder kappt bei DISPATCH_HARD_CHARS = 4 000.
"""
import uuid

from app.models.agent import Agent
from app.services.reference_docs_builder import generate_reference_docs
from app.services.template_renderer import build_agent_context, render_agent_file


def _soul(role: str = "developer", comm_v2: bool = True, name: str = "TestAgent") -> str:
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


# --- 1. Nach `mc finish` ist Schluss --------------------------------------

def test_worker_soul_ends_the_session_after_finish():
    soul = _soul()
    assert "### After `mc finish` you are done" in soul
    idx = soul.index("### After `mc finish` you are done")
    window = soul[idx : idx + 900].lower()
    assert "background" in window
    assert "start nothing new" in window
    # Das WARUM: die Session haelt den Slot und blockiert das Deploy-Fenster.
    assert "blocks the deploy" in window


def test_finish_rule_demands_killing_background_jobs_first():
    soul = _soul().lower()
    idx = soul.index("### after `mc finish` you are done")
    window = soul[idx : idx + 900]
    assert "before you call `mc finish`" in window


def test_finish_rule_is_worker_only():
    """Der Lead schliesst keine Worker-Karten — die Worker-Truth-Sektion ist
    fuer ihn ausgeblendet, die Regel darf ihn also nicht erreichen."""
    lead = _soul(role="Orchestrator", name="Lead")
    assert "### After `mc finish` you are done" not in lead


# --- 2. Fragen nur ueber `mc ask` -----------------------------------------

HEADING = "### Questions go through MC, never through your CLI's own dialog"


def test_worker_asks_via_mc_ask_not_a_terminal_dialog():
    soul = _soul()
    assert HEADING in soul
    idx = soul.index(HEADING)
    window = soul[idx : idx + 900]
    assert "mc ask" in window
    assert "--blocking" in window
    assert "mc blocked" in window
    lowered = window.lower()
    assert "interactive prompt" in lowered
    # Das WARUM: niemand sieht den Dialog, der Watchdog haelt den Agenten
    # trotzdem fuer lebendig.
    assert "heartbeat" in lowered


def test_ask_rule_survives_without_comm_v2_but_without_pilot_verbs():
    """Ohne comm_v2 gibt es kein `mc ask` (Kontrakt in
    test_agent_docs_contract) — die Regel bleibt, der Kanal wird `mc question`
    / `mc blocked`."""
    soul = _soul(comm_v2=False)
    assert HEADING in soul
    assert "mc ask" not in soul
    idx = soul.index(HEADING)
    window = soul[idx : idx + 900]
    assert "mc question" in window
    assert "mc blocked" in window


# --- 3. Proben nie gegen die eigene Session -------------------------------

def test_worker_soul_forbids_tmux_kill_server():
    soul = _soul()
    assert "### Probes never touch your own session" in soul
    idx = soul.index("### Probes never touch your own session")
    window = soul[idx : idx + 900]
    assert "kill-server" in window
    assert "tmux -L probe" in window
    assert "HOME=/tmp/probe" in window
    assert "without an explicit" in window.lower()


def test_probe_rule_demands_a_fresh_checkout_for_test_suites():
    soul = _soul()
    idx = soul.index("### Probes never touch your own session")
    window = soul[idx : idx + 900].lower()
    assert "origin/main" in window
    assert "fresh checkout" in window


# --- 4. Lead schliesst Worker-Subtasks nicht selbst ------------------------

def _lead_soul() -> str:
    return _soul(role="Orchestrator", name="Lead")


def test_lead_waits_for_the_workers_own_finish():
    soul = _lead_soul()
    assert "**The worker closes their own subtask — I never do" in soul
    idx = soul.index("The worker closes their own subtask")
    window = soul[idx : idx + 900]
    assert "subtask_completed" in window
    assert "mc finish" in window
    lowered = window.lower()
    assert "running turn" in lowered
    assert "mc park" in window


def test_lead_rule_is_not_shipped_to_workers():
    worker = _soul(role="developer")
    assert "The worker closes their own subtask" not in worker


def test_delegation_doc_teaches_the_same_close_rule():
    docs = generate_reference_docs({"operator_name": "TestOp"})
    content = docs["delegation"]
    assert "The worker closes their own subtask" in content
    assert "subtask_completed" in content
    assert "mc park" in content


# --- 5. Delegations-Karten <= 3 000 Zeichen -------------------------------

def test_lead_soul_caps_the_description_at_3000_chars():
    soul = _lead_soul()
    assert "### Size limit: 3 000 characters per description" in soul
    idx = soul.index("### Size limit: 3 000 characters per description")
    window = soul[idx : idx + 1200]
    assert "DISPATCH_HARD_CHARS" in window
    assert "dispatch_message_builder.py" in window
    assert "under 3 000\ncharacters" in window or "under 3 000 characters" in window


def test_soul_quotes_the_real_cap_value_from_the_code():
    """Der Cap darf nicht geraten sein — die Zahl im Template muss der
    Konstante im Prompt-Builder entsprechen."""
    from app.services.dispatch_message_builder import DISPATCH_HARD_CHARS

    soul = _lead_soul()
    idx = soul.index("### Size limit: 3 000 characters per description")
    window = soul[idx : idx + 1200]
    assert f"{DISPATCH_HARD_CHARS // 1000} 000 characters" in window


def test_size_rule_teaches_anchors_instead_of_prose():
    soul = _lead_soul()
    idx = soul.index("### Size limit: 3 000 characters per description")
    window = soul[idx : idx + 1200]
    assert ":142" in window  # Datei:Zeile-Anker als Muster
    assert "mc docs" in window
    assert "Definition of Done" in window


def test_delegation_doc_teaches_the_same_size_rule():
    docs = generate_reference_docs({"operator_name": "TestOp"})
    content = docs["delegation"]
    assert "DISPATCH_HARD_CHARS" in content
    assert "under 3 000" in content


# --- 6. Eine Karte kommt nie im laufenden Zug an --------------------------

IN_TURN = "### A task never arrives inside your turn"


def test_worker_soul_says_a_card_never_arrives_in_turn():
    soul = _soul()
    assert IN_TURN in soul
    idx = soul.index(IN_TURN)
    window = soul[idx : idx + 900]
    assert "new_task" in window
    assert "mc inbox" in window
    assert "mc patch --status waiting" in window
    lowered = window.lower()
    assert "turn boundary" in lowered
    assert "deadlock" in lowered
    # Das WARUM: die Karte bleibt unzugestellt, bis der Dispatch eskaliert.
    assert "escalates the dispatch" in lowered


def test_in_turn_rule_forbids_the_sleep_inbox_loop():
    soul = _soul()
    idx = soul.index(IN_TURN)
    window = soul[idx : idx + 900]
    assert "`sleep` + `mc inbox`" in window


def test_in_turn_rule_survives_without_comm_v2():
    """Ohne comm_v2 gibt es kein `mc inbox` (Doku-Kontrakt) — die Regel
    bleibt, der Kanal wird neutral benannt."""
    soul = _soul(comm_v2=False)
    assert IN_TURN in soul
    assert "mc inbox" not in soul
    idx = soul.index(IN_TURN)
    window = soul[idx : idx + 900]
    assert "`sleep` + a message check" in window


def test_in_turn_rule_is_worker_only():
    lead = _soul(role="Orchestrator", name="Lead")
    assert IN_TURN not in lead


LEAD_SENTENCE = "needs a second task for the SAME worker"


def test_lead_never_orders_an_in_turn_wait():
    soul = _lead_soul()
    assert LEAD_SENTENCE in soul
    idx = soul.index(LEAD_SENTENCE)
    window = soul[idx : idx + 300]
    assert "mc finish" in window
    assert "wait in-turn for a card" in window


def test_in_turn_lead_sentence_is_not_shipped_to_workers():
    worker = _soul(role="developer")
    assert LEAD_SENTENCE not in worker


def test_delegation_doc_teaches_the_in_turn_rule():
    docs = generate_reference_docs({"operator_name": "TestOp"})
    content = docs["delegation"]
    assert LEAD_SENTENCE in content
    assert "wait in-turn for a card" in content
