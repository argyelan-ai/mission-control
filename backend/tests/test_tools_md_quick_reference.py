"""Tests for the 'Typische Abläufe' section in TOOLS.md — role-aware worked examples.

Goal: each flow is a copy-paste-able end-to-end walkthrough with concrete
tool-call examples and real inputs (not a command list). Role-awareness
via scopes: a writer sees no delegation flow, a researcher sees no plugin
management, etc.
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
    """Ablauf 1 (task received + completed) is ALWAYS present, regardless of scopes."""
    flows = _flows_section(_gen(scopes=["heartbeat"]))
    assert "Ablauf 1" in flows
    assert "mc me" in flows
    assert "mc ack" in flows
    assert "mc done" in flows
    assert "mc blocked --type" in flows
    # ACK error note (already-ACKed awareness)
    assert "In Progress" in flows
    # Non-relevant flows NOT present with heartbeat-only
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
    # Clear rule that callback-wait = in_progress (not blocked)
    assert "in_progress" in flows
    assert "blocked" in flows  # part of the warning


def test_flow5_plugin_mgmt_only_board_lead():
    """Plugin-mgmt flow ONLY for is_board_lead=True + AGENTS_MANAGE."""
    # Non-lead with agents:manage → not visible
    flows_worker = _flows_section(_gen(scopes=["heartbeat", "agents:manage"], is_board_lead=False))
    assert "Ablauf 5" not in flows_worker
    assert "mc plugin-list" not in flows_worker

    # Board-lead with agents:manage → visible
    flows_lead = _flows_section(_gen(scopes=["heartbeat", "agents:manage"], is_board_lead=True))
    assert "Ablauf 5" in flows_lead
    assert "mc plugin-list" in flows_lead
    assert "mc plugin-assign" in flows_lead
    assert "mc worker-restart" in flows_lead
    # Install request with task_id coupling
    assert "install-requests" in flows_lead
    assert "task_id" in flows_lead


def test_flow6_memory_knowledge_read():
    flows = _flows_section(_gen(scopes=["heartbeat", "knowledge:read"]))
    assert "Ablauf 6" in flows
    assert "mc memory" in flows


def test_empty_scopes_means_all_flows():
    """scopes=[] → backward-compat ALL_SCOPES → all flows visible."""
    flows = _flows_section(_gen(scopes=[], is_board_lead=True))
    assert "Ablauf 1" in flows
    assert "Ablauf 2" in flows
    assert "Ablauf 3" in flows
    assert "Ablauf 4" in flows
    assert "Ablauf 5" in flows
    assert "Ablauf 6" in flows


def test_flows_use_real_task_context_placeholders():
    """Flows use $TASK_ID (real env-var) and not <task-uuid> as a placeholder."""
    flows = _flows_section(_gen(scopes=[]))
    assert "$TASK_ID" in flows  # real env-var from mc-context.env


def test_blocker_types_documented():
    """All 6 valid blocker_type values are explained in Ablauf 1."""
    flows = _flows_section(_gen(scopes=["heartbeat"]))
    for bt in ["missing_info", "technical_problem", "decision_needed",
               "permission_needed", "dependency_blocked", "other"]:
        assert bt in flows
