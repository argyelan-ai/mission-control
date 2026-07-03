"""Tests for project scopes in scopes.py."""
from app.scopes import DEFAULT_SCOPES, ALL_SCOPES, AgentRole, Scope


def test_project_scopes_exist():
    assert Scope.PROJECT_READ == "project:read"
    assert Scope.PROJECT_WRITE == "project:write"


def test_project_scopes_in_all_scopes():
    assert "project:read" in ALL_SCOPES
    assert "project:write" in ALL_SCOPES


def test_lead_has_all_project_scopes():
    lead_scopes = DEFAULT_SCOPES[AgentRole.LEAD]
    assert "project:read" in lead_scopes
    assert "project:write" in lead_scopes


def test_developer_has_project_scopes():
    dev_scopes = DEFAULT_SCOPES[AgentRole.DEVELOPER]
    assert "project:read" in dev_scopes
    assert "project:write" in dev_scopes


def test_researcher_has_project_scopes():
    researcher_scopes = DEFAULT_SCOPES[AgentRole.RESEARCHER]
    assert "project:read" in researcher_scopes
    assert "project:write" in researcher_scopes


def test_planner_has_project_read():
    planner_scopes = DEFAULT_SCOPES[AgentRole.PLANNER]
    assert "project:read" in planner_scopes
    assert "project:write" in planner_scopes


def test_writer_has_no_project_scopes():
    writer_scopes = DEFAULT_SCOPES[AgentRole.WRITER]
    assert "project:read" not in writer_scopes
    assert "project:write" not in writer_scopes


def test_tester_has_no_project_scopes():
    tester_scopes = DEFAULT_SCOPES[AgentRole.TESTER]
    assert "project:read" not in tester_scopes
    assert "project:write" not in tester_scopes


def test_tester_has_credentials_read():
    """Tester needs credentials:read for mc verify --login-as Vault resolve.
    Side issue #2 (2026-04-23): login rate limit blocked auto-tests before
    the tester could log in via Vault."""
    tester_scopes = DEFAULT_SCOPES[AgentRole.TESTER]
    assert "credentials:read" in tester_scopes
