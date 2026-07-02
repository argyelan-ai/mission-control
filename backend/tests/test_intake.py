"""Tests fuer Phase 2 Operator-Intake: Felder, Planning Brief, Root-vs-Child."""
from unittest.mock import MagicMock
import pytest

from app.services.dispatch import build_planning_brief


# ── Planning Brief ──────────────────────────────────────

def _root_task(**overrides):
    """Root-/Intake-Task mit Structured Mode."""
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
    t.parent_task_id = None  # Root-Task
    for k, v in overrides.items():
        setattr(t, k, v)
    return t


def test_planning_brief_structured():
    """Structured Mode erzeugt vollstaendigen Planning Brief."""
    task = _root_task()
    brief = build_planning_brief(task)
    assert brief is not None
    assert "Operator-Briefing (structured)" in brief
    assert "code_change" in brief
    assert "Feature X" in brief
    assert "Keine Doku" in brief
    assert "Cache nicht" in brief
    assert "on_plan" in brief
    assert "execute_low_risk" in brief
    assert "example.com" in brief
    assert "Nicht erlaubt" in brief


def test_planning_brief_quick():
    """Quick Mode erzeugt minimalen Brief."""
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
    assert "Operator-Briefing (quick)" in brief
    assert "code_change" in brief


def test_planning_brief_legacy_returns_none():
    """Legacy-Task ohne intake_mode → kein Brief."""
    task = _root_task(intake_mode=None)
    brief = build_planning_brief(task)
    assert brief is None


def test_planning_brief_empty_fields():
    """Alle Felder null → kein Brief (nur Header, zu kurz)."""
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
    assert brief is None  # Nur Header, keine Sections → None


# ── request_kind vs delegation_type Trennung ────────────

def test_request_kind_not_delegation_type():
    """request_kind und delegation_type sind unterschiedliche Felder."""
    task = _root_task(request_kind="research")
    task.delegation_type = None  # Root-Tasks haben keinen delegation_type
    brief = build_planning_brief(task)
    assert "research" in brief
    assert task.delegation_type is None


# ── Root-vs-Child: Intake-Felder nur auf Root ───────────

def test_child_task_has_no_brief():
    """Child-Task (parent_task_id gesetzt) → intake_mode typischerweise null."""
    task = MagicMock()
    task.intake_mode = None  # Child-Tasks bekommen kein intake_mode
    task.parent_task_id = "parent-id"
    brief = build_planning_brief(task)
    assert brief is None


# ── Browser und Credentials getrennt ────────────────────

def test_browser_without_credentials():
    """needs_browser=True, requires_auth=False → nur Browser im Brief."""
    task = _root_task(needs_browser=True, requires_auth=False)
    brief = build_planning_brief(task)
    assert "Browser noetig:** Ja" in brief
    assert "Credentials noetig" not in brief  # requires_auth=False


def test_credentials_without_browser():
    """needs_browser=False, requires_auth=True → nur Credentials im Brief."""
    task = _root_task(needs_browser=False, requires_auth=True)
    brief = build_planning_brief(task)
    assert "Credentials noetig:** Ja" in brief
    assert "Browser noetig" not in brief  # needs_browser=False


def test_both_browser_and_credentials():
    """Beides gesetzt → beide im Brief."""
    task = _root_task(needs_browser=True, requires_auth=True)
    brief = build_planning_brief(task)
    assert "Browser noetig:** Ja" in brief
    assert "Credentials noetig:** Ja" in brief


# ── Reference URLs als Liste ────────────────────────────

def test_reference_urls_list():
    """reference_urls ist eine Liste, nicht CSV."""
    task = _root_task(reference_urls=["https://a.com", "https://b.com"])
    brief = build_planning_brief(task)
    assert "https://a.com" in brief
    assert "https://b.com" in brief


def test_reference_urls_empty_list():
    """Leere Liste → kein Referenz-Abschnitt."""
    task = _root_task(reference_urls=[])
    brief = build_planning_brief(task)
    assert "Referenzen" not in brief


# ── Enum-Validierung (Pydantic) ─────────────────────────

def test_request_kind_literal_validation():
    """Pydantic TaskCreate akzeptiert nur gueltige request_kind Werte."""
    from app.routers.tasks import TaskCreate
    # Gueltig
    t = TaskCreate(title="Test", request_kind="research")
    assert t.request_kind == "research"

    # Ungueltig
    with pytest.raises(Exception):
        TaskCreate(title="Test", request_kind="invalid_kind")


def test_approval_policy_literal_validation():
    """Pydantic TaskCreate akzeptiert nur gueltige approval_policy Werte."""
    from app.routers.tasks import TaskCreate
    t = TaskCreate(title="Test", approval_policy="on_plan")
    assert t.approval_policy == "on_plan"

    with pytest.raises(Exception):
        TaskCreate(title="Test", approval_policy="sometimes")


def test_autonomy_level_literal_validation():
    """Pydantic TaskCreate akzeptiert nur gueltige autonomy_level Werte."""
    from app.routers.tasks import TaskCreate
    t = TaskCreate(title="Test", autonomy_level="draft_only")
    assert t.autonomy_level == "draft_only"

    with pytest.raises(Exception):
        TaskCreate(title="Test", autonomy_level="yolo")
