"""Tests fuer tools_md_builder.py — Token-Sicherheit, Delegation, Skills-Passthrough."""
from app.services.tools_md_builder import generate_tools_md


def test_no_raw_token_in_output():
    """TOOLS.md darf keinen hardcoded Token enthalten — nur $MC_AGENT_TOKEN."""
    raw_token = "supersecrettoken123"
    result = generate_tools_md(
        name="TestAgent",
        emoji="🤖",
        raw_token=raw_token,
        board_id="board-uuid-123",
        is_board_lead=False,
    )
    assert raw_token not in result, "Hardcoded raw_token gefunden — muss $MC_AGENT_TOKEN sein"
    assert "$MC_AGENT_TOKEN" in result


def test_no_raw_token_board_lead():
    """Board Lead TOOLS.md darf ebenfalls keinen hardcoded Token enthalten."""
    raw_token = "anothersecret456"
    result = generate_tools_md(
        name="Henry",
        emoji="👑",
        raw_token=raw_token,
        board_id="board-uuid-123",
        is_board_lead=True,
    )
    assert raw_token not in result
    assert "$MC_AGENT_TOKEN" in result


def test_no_raw_token_no_board():
    """Auch ohne board_id: kein hardcoded Token."""
    raw_token = "tokenwithoutboard"
    result = generate_tools_md(
        name="Wanderer",
        emoji="🌍",
        raw_token=raw_token,
        board_id=None,
        is_board_lead=False,
    )
    assert raw_token not in result


def test_non_board_lead_has_assigned_agent_id():
    """Nicht-Board-Lead Task-Erstellung muss assigned_agent_id und parent_task_id enthalten."""
    result = generate_tools_md(
        name="Planner",
        emoji="📋",
        raw_token="tok",
        board_id="board-uuid-123",
        is_board_lead=False,
    )
    assert "assigned_agent_id" in result, "assigned_agent_id fehlt in nicht-Board-Lead TOOLS.md"
    assert "parent_task_id" in result, "parent_task_id fehlt in nicht-Board-Lead TOOLS.md"


def test_task_creation_has_stitch_passthrough_instruction():
    """Task-Erstellung muss Hinweis enthalten, Skills/Tools aus dem Haupt-Task weiterzugeben."""
    result = generate_tools_md(
        name="Planner",
        emoji="📋",
        raw_token="tok",
        board_id="board-uuid-123",
        is_board_lead=False,
    )
    assert "Stitch" in result or "SKILLS" in result or "Skill" in result, \
        "Kein Stitch/Skills-Passthrough-Hinweis in TOOLS.md gefunden"
