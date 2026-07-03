"""Tests for Phase 2 Operator-Intake: fields, Planning Brief, Root-vs-Child."""
from unittest.mock import MagicMock
import pytest

from app.services.dispatch import build_planning_brief


# ── Planning Brief ──────────────────────────────────────

def _root_task(**overrides):
    """Root/intake task with Structured Mode."""
    t = MagicMock()
    t.intake_mode = "structured"
    t.request_kind = "code_change"
    t.desired_output = "Feature X implementieren"
    t.acceptance_criteria = "Tests gruen, Code reviewed"
    t.scope_out = "Keine Doku-Aenderungen"
    t.risk_notes = "Cache nicht invalidieren"
    t.needs_browser = False
    t.requires_auth = False
    t.approval_policy = "on_plan"
    t.autonomy_level = "execute_low_risk"
    t.reference_urls = ["https://example.com/spec"]
    t.reference_notes = "Siehe Abschnitt 3"
    t.publish_allowed = False
    t.parent_task_id = None  # Root task
    for k, v in overrides.items():
        setattr(t, k, v)
    return t


def test_planning_brief_structured():
    """Structured Mode produces a complete Planning Brief."""
    task = _root_task()
    brief = build_planning_brief(task)
    assert brief is not None
    assert "Operator Briefing (structured)" in brief
    assert "code_change" in brief
    assert "Feature X" in brief
    assert "Keine Doku" in brief
    assert "Cache nicht" in brief
    assert "on_plan" in brief
    assert "execute_low_risk" in brief
    assert "example.com" in brief
    assert "Not allowed" in brief


def test_planning_brief_quick():
    """Quick Mode produces a minimal brief."""
    task = _root_task(
        intake_mode="quick",
        desired_output=None,
        scope_out=None,
        risk_notes=None,
        reference_urls=None,
        reference_notes=None,
        approval_policy=None,
        autonomy_level=None,
        publish_allowed=None,
    )
    brief = build_planning_brief(task)
    assert brief is not None
    assert "Operator Briefing (quick)" in brief
    assert "code_change" in brief


def test_planning_brief_legacy_returns_none():
    """Legacy task without intake_mode → no brief."""
    task = _root_task(intake_mode=None)
    brief = build_planning_brief(task)
    assert brief is None


def test_planning_brief_empty_fields():
    """All fields null → no brief (header only, too short)."""
    task = _root_task(
        intake_mode="quick",
        request_kind=None,
        desired_output=None,
        acceptance_criteria=None,
        scope_out=None,
        risk_notes=None,
        needs_browser=None,
        requires_auth=False,
        approval_policy=None,
        autonomy_level=None,
        reference_urls=None,
        reference_notes=None,
        publish_allowed=None,
    )
    brief = build_planning_brief(task)
    assert brief is None  # Header only, no sections → None


# ── request_kind vs delegation_type separation ────────────

def test_request_kind_not_delegation_type():
    """request_kind and delegation_type are different fields."""
    task = _root_task(request_kind="research")
    task.delegation_type = None  # Root tasks have no delegation_type
    brief = build_planning_brief(task)
    assert "research" in brief
    assert task.delegation_type is None


# ── Root-vs-Child: intake fields only on root ───────────

def test_child_task_has_no_brief():
    """Child task (parent_task_id set) → intake_mode typically null."""
    task = MagicMock()
    task.intake_mode = None  # Child tasks don't get an intake_mode
    task.parent_task_id = "parent-id"
    brief = build_planning_brief(task)
    assert brief is None


# ── Browser and credentials kept separate ────────────────────

def test_browser_without_credentials():
    """needs_browser=True, requires_auth=False → only browser in brief."""
    task = _root_task(needs_browser=True, requires_auth=False)
    brief = build_planning_brief(task)
    assert "Browser needed:** Yes" in brief
    assert "Credentials needed" not in brief  # requires_auth=False


def test_credentials_without_browser():
    """needs_browser=False, requires_auth=True → only credentials in brief."""
    task = _root_task(needs_browser=False, requires_auth=True)
    brief = build_planning_brief(task)
    assert "Credentials needed:** Yes" in brief
    assert "Browser needed" not in brief  # needs_browser=False


def test_both_browser_and_credentials():
    """Both set → both in brief."""
    task = _root_task(needs_browser=True, requires_auth=True)
    brief = build_planning_brief(task)
    assert "Browser needed:** Yes" in brief
    assert "Credentials needed:** Yes" in brief


# ── Reference URLs as a list ────────────────────────────

def test_reference_urls_list():
    """reference_urls is a list, not CSV."""
    task = _root_task(reference_urls=["https://a.com", "https://b.com"])
    brief = build_planning_brief(task)
    assert "https://a.com" in brief
    assert "https://b.com" in brief


def test_reference_urls_empty_list():
    """Empty list → no reference section."""
    task = _root_task(reference_urls=[])
    brief = build_planning_brief(task)
    assert "References" not in brief


# ── Enum validation (Pydantic) ─────────────────────────

def test_request_kind_literal_validation():
    """Pydantic TaskCreate only accepts valid request_kind values."""
    from app.routers.tasks import TaskCreate
    # Valid
    t = TaskCreate(title="Test", request_kind="research")
    assert t.request_kind == "research"

    # Invalid
    with pytest.raises(Exception):
        TaskCreate(title="Test", request_kind="invalid_kind")


def test_approval_policy_literal_validation():
    """Pydantic TaskCreate only accepts valid approval_policy values."""
    from app.routers.tasks import TaskCreate
    t = TaskCreate(title="Test", approval_policy="on_plan")
    assert t.approval_policy == "on_plan"

    with pytest.raises(Exception):
        TaskCreate(title="Test", approval_policy="sometimes")


def test_autonomy_level_literal_validation():
    """Pydantic TaskCreate only accepts valid autonomy_level values."""
    from app.routers.tasks import TaskCreate
    t = TaskCreate(title="Test", autonomy_level="draft_only")
    assert t.autonomy_level == "draft_only"

    with pytest.raises(Exception):
        TaskCreate(title="Test", autonomy_level="yolo")
