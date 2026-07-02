"""Tests fuer 'Typische Ablaeufe' Sektion in TOOLS.md — role-aware worked examples.

Ziel: jeder Flow ist ein copy-paste-faehiger End-to-End-Ablauf mit konkreten
Tool-Call-Beispielen und realen Inputs (nicht Command-Liste). Role-Awareness
via Scopes: ein Writer sieht kein Delegation-Flow, ein Researcher kein Plugin-
Management, etc.
"""
from app.services.tools_md_builder import generate_tools_md


def _gen(scopes: list[str] | None = None, is_board_lead: bool = False) -> str:
    return generate_tools_md(
        name="TestAgent", emoji="🤖", raw_token="tkn",
        board_id="board-123", is_board_lead=is_board_lead, scopes=scopes or [],
    )


def _flows_section(md: str) -> str:
    return md.split("## Typische Abläufe")[1].split("\n---\n")[0]


def test_flows_header_present():
    md = _gen(scopes=["heartbeat"])
    assert "## Typische Abläufe" in md
    assert "Ablauf 1" in md  # universal lifecycle


def test_flow1_lifecycle_universal():
    """Ablauf 1 (Task empfangen + abschliessen) ist IMMER drin, unabhaengig von Scopes."""
    flows = _flows_section(_gen(scopes=["heartbeat"]))
    assert "Ablauf 1" in flows
    assert "mc me" in flows
    assert "mc ack" in flows
    assert "mc done" in flows
    assert "mc blocked --type" in flows
    # ACK-Error-Hinweis (Already-ACKed Awareness)
    assert "In Progress" in flows
    # Nicht-relevante Flows NICHT bei heartbeat-only
    assert "Ablauf 2" not in flows  # telegram
    assert "Ablauf 4" not in flows  # delegation


def test_flow2_telegram_chat_write():
    flows = _flows_section(_gen(scopes=["heartbeat", "chat:write"]))
    assert "Ablauf 2" in flows
    assert "mc telegram" in flows
    assert "--file" in flows
    assert "--photo" in flows
    assert "mc verify" in flows


def test_flow3_deliverable_tasks_write():
    flows = _flows_section(_gen(scopes=["heartbeat", "tasks:write"]))
    assert "Ablauf 3" in flows
    assert "mc deliverable" in flows
    assert "mc pdf" in flows
    assert "mc checkpoint" in flows
    assert "mc checklist" in flows


def test_flow4_delegation_tasks_create():
    flows = _flows_section(_gen(scopes=["heartbeat", "tasks:create"]))
    assert "Ablauf 4" in flows
    assert "mc delegate" in flows
    # Klare Regel dass Callback-Wait = in_progress (nicht blocked)
    assert "in_progress" in flows
    assert "blocked" in flows  # part of the warning


def test_flow5_plugin_mgmt_only_board_lead():
    """Plugin-Mgmt Flow NUR fuer is_board_lead=True + AGENTS_MANAGE."""
    # Non-lead mit agents:manage → nicht sichtbar
    flows_worker = _flows_section(_gen(scopes=["heartbeat", "agents:manage"], is_board_lead=False))
    assert "Ablauf 5" not in flows_worker
    assert "mc plugin-list" not in flows_worker

    # Board-lead mit agents:manage → sichtbar
    flows_lead = _flows_section(_gen(scopes=["heartbeat", "agents:manage"], is_board_lead=True))
    assert "Ablauf 5" in flows_lead
    assert "mc plugin-list" in flows_lead
    assert "mc plugin-assign" in flows_lead
    assert "mc worker-restart" in flows_lead
    # Install-Request mit task_id-Koppelung
    assert "install-requests" in flows_lead
    assert "task_id" in flows_lead


def test_flow6_memory_knowledge_read():
    flows = _flows_section(_gen(scopes=["heartbeat", "knowledge:read"]))
    assert "Ablauf 6" in flows
    assert "mc memory" in flows


def test_empty_scopes_means_all_flows():
    """scopes=[] → backward-compat ALL_SCOPES → alle Flows sichtbar."""
    flows = _flows_section(_gen(scopes=[], is_board_lead=True))
    assert "Ablauf 1" in flows
    assert "Ablauf 2" in flows
    assert "Ablauf 3" in flows
    assert "Ablauf 4" in flows
    assert "Ablauf 5" in flows
    assert "Ablauf 6" in flows


def test_flows_use_real_task_context_placeholders():
    """Flows nutzen $TASK_ID (real env-var) und nicht <task-uuid> als Platzhalter."""
    flows = _flows_section(_gen(scopes=[]))
    assert "$TASK_ID" in flows  # echte env-var aus mc-context.env


def test_blocker_types_documented():
    """Alle 6 valide blocker_type Werte sind im Ablauf 1 erklaert."""
    flows = _flows_section(_gen(scopes=["heartbeat"]))
    for bt in ["missing_info", "technical_problem", "decision_needed",
               "permission_needed", "dependency_blocked", "other"]:
        assert bt in flows
