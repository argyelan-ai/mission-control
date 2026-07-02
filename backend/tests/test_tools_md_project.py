"""Tests fuer Project-Sektion in tools_md_builder.py."""
from app.services.tools_md_builder import generate_tools_md
from app.scopes import Scope


PROJECT_SCOPES = [s.value for s in [
    Scope.PROJECT_READ, Scope.PROJECT_WRITE,
    Scope.TASKS_READ, Scope.TASKS_WRITE,
    Scope.HEARTBEAT, Scope.CHAT_WRITE,
]]

READ_ONLY_SCOPES = [s.value for s in [
    Scope.PROJECT_READ, Scope.TASKS_READ, Scope.HEARTBEAT,
]]

NO_PROJECT_SCOPES = [s.value for s in [
    Scope.TASKS_READ, Scope.TASKS_WRITE, Scope.HEARTBEAT,
]]


def test_project_section_present_when_project_read_scope():
    result = generate_tools_md(
        name="FreeCode", emoji="💻", raw_token="tok123",
        board_id="board-1", scopes=PROJECT_SCOPES,
    )
    assert "## Projekt-Kontext abrufen" in result
    assert "project_id" in result


def test_project_read_shows_get_project_endpoint():
    result = generate_tools_md(
        name="FreeCode", emoji="💻", raw_token="tok123",
        board_id="board-1", scopes=PROJECT_SCOPES,
    )
    assert "/api/v1/agent/projects/{project_id}" in result


def test_project_write_shows_complete_phase_endpoint():
    result = generate_tools_md(
        name="FreeCode", emoji="💻", raw_token="tok123",
        board_id="board-1", scopes=PROJECT_SCOPES,
    )
    assert "/api/v1/agent/phases/" in result
    assert "complete" in result


def test_project_write_shows_deliverable_v2_fields():
    result = generate_tools_md(
        name="FreeCode", emoji="💻", raw_token="tok123",
        board_id="board-1", scopes=PROJECT_SCOPES,
    )
    assert "is_pinned" in result
    assert "scope" in result
    assert "git_commit" in result


def test_project_section_absent_without_project_scopes():
    result = generate_tools_md(
        name="Tester", emoji="🧪", raw_token="tok456",
        board_id="board-1", scopes=NO_PROJECT_SCOPES,
    )
    assert "## Projekt-Kontext abrufen" not in result


def test_project_read_only_no_complete_phase():
    result = generate_tools_md(
        name="Reader", emoji="📖", raw_token="tok789",
        board_id="board-1", scopes=READ_ONLY_SCOPES,
    )
    assert "## Projekt-Kontext abrufen" in result
    # complete endpoint requires project:write
    assert "/complete" not in result


def test_project_section_absent_without_board_id():
    result = generate_tools_md(
        name="FreeCode", emoji="💻", raw_token="tok123",
        board_id=None, scopes=PROJECT_SCOPES,
    )
    assert "## Projekt-Kontext abrufen" not in result
