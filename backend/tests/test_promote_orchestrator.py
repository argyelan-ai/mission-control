"""Tests fuer Phase 4A Promote-Orchestrator (mit 4A.1 Hardening).

Testmatrix:
- Auto-Promote: ONLY for explicit execute_low_risk or approval_policy=never
- Approval: credentials, high-risk tags, approval_policy, mixed parent
- Manual Wait: manual_dispatch, advise_only, draft_only, INSUFFICIENT CLASSIFICATION
- Root→Child: parent fields inherited correctly
- Flag: orchestrator off → no decisions
- Dedupe: no event spam
"""
from unittest.mock import MagicMock

import pytest

from app.services.dispatch_gating import (
    AUTO_PROMOTE,
    MANUAL_WAIT,
    NEEDS_APPROVAL,
    evaluate_promote_decision,
)


def _task(**kwargs):
    t = MagicMock()
    t.autonomy_level = kwargs.get("autonomy_level", None)
    t.approval_policy = kwargs.get("approval_policy", None)
    t.requires_auth = kwargs.get("requires_auth", False)
    t.needs_browser = kwargs.get("needs_browser", False)
    t.credential_consent = kwargs.get("credential_consent", None)
    t.credentials_encrypted = kwargs.get("credentials_encrypted", None)
    t.delegation_type = kwargs.get("delegation_type", "code_change")
    t.tags = kwargs.get("tags", [])
    t.request_kind = kwargs.get("request_kind", None)
    return t


# ── Auto-Promote (ONLY explicit safe cases) ────────────

def test_execute_low_risk_auto_promotes():
    """autonomy_level=execute_low_risk → auto-promote."""
    decision, _ = evaluate_promote_decision(_task(autonomy_level="execute_low_risk"))
    assert decision == AUTO_PROMOTE


def test_approval_policy_never_with_execute_low_risk_still_auto_promotes():
    """approval_policy=never should not suppress explicit execute_low_risk auto-promote."""
    decision, reason = evaluate_promote_decision(
        _task(autonomy_level="execute_low_risk", approval_policy="never")
    )
    assert decision == AUTO_PROMOTE
    assert "execute_low_risk" in reason


def test_execute_with_approval_no_risk_auto_promotes():
    """execute_with_approval_on_risk + no risk signals → auto-promote."""
    decision, _ = evaluate_promote_decision(
        _task(autonomy_level="execute_with_approval_on_risk")
    )
    assert decision == AUTO_PROMOTE


def test_approval_policy_never_simple_code_auto_promotes():
    """approval_policy=never + code_change + no auth → auto-promote."""
    decision, _ = evaluate_promote_decision(
        _task(approval_policy="never", delegation_type="code_change")
    )
    assert decision == AUTO_PROMOTE


def test_low_risk_code_change_auto_promotes():
    """Explicit low-risk code changes should auto-promote even without approval_policy."""
    decision, reason = evaluate_promote_decision(
        _task(autonomy_level="execute_low_risk", delegation_type="code_change")
    )
    assert decision == AUTO_PROMOTE
    assert "execute_low_risk" in reason


# ── CONSERVATIVE DEFAULT: null → manual wait ────────────

def test_null_autonomy_null_approval_manual_wait():
    """No autonomy, no approval → MANUAL WAIT (conservative default)."""
    decision, reason = evaluate_promote_decision(_task())
    assert decision == MANUAL_WAIT
    assert "insufficient" in reason.lower()


def test_null_everything_manual_wait():
    """All fields null/default → MANUAL WAIT."""
    decision, _ = evaluate_promote_decision(
        _task(autonomy_level=None, approval_policy=None)
    )
    assert decision == MANUAL_WAIT


# ── Needs Approval ──────────────────────────────────────

def test_credentials_need_approval():
    """requires_auth=true → Approval."""
    decision, reason = evaluate_promote_decision(_task(requires_auth=True))
    assert decision == NEEDS_APPROVAL
    assert "credential" in reason.lower()


def test_credential_bound_needs_approval():
    """delegation_type=credential_bound → Approval."""
    decision, _ = evaluate_promote_decision(_task(delegation_type="credential_bound"))
    assert decision == NEEDS_APPROVAL


def test_approval_policy_on_plan():
    """approval_policy=on_plan → Approval."""
    decision, _ = evaluate_promote_decision(_task(approval_policy="on_plan"))
    assert decision == NEEDS_APPROVAL


def test_approval_policy_always():
    """approval_policy=always → Approval."""
    decision, _ = evaluate_promote_decision(_task(approval_policy="always"))
    assert decision == NEEDS_APPROVAL


def test_infra_tag_needs_approval():
    """Tag 'infra' → Approval."""
    decision, reason = evaluate_promote_decision(_task(tags=[{"name": "infra"}]))
    assert decision == NEEDS_APPROVAL
    assert "infra" in reason


def test_db_tag_needs_approval():
    """Tag 'db' → Approval."""
    decision, _ = evaluate_promote_decision(_task(tags=[{"name": "db"}]))
    assert decision == NEEDS_APPROVAL


def test_browser_plus_auth_needs_approval():
    """Browser + Auth → Approval."""
    decision, _ = evaluate_promote_decision(
        _task(needs_browser=True, requires_auth=True)
    )
    assert decision == NEEDS_APPROVAL


# ── Manual Wait ─────────────────────────────────────────

def test_manual_dispatch_required():
    """autonomy_level=manual_dispatch_required → manual wait."""
    decision, _ = evaluate_promote_decision(
        _task(autonomy_level="manual_dispatch_required")
    )
    assert decision == MANUAL_WAIT


def test_advise_only():
    """autonomy_level=advise_only → manual wait."""
    decision, _ = evaluate_promote_decision(_task(autonomy_level="advise_only"))
    assert decision == MANUAL_WAIT


def test_draft_only():
    """autonomy_level=draft_only → manual wait."""
    decision, _ = evaluate_promote_decision(_task(autonomy_level="draft_only"))
    assert decision == MANUAL_WAIT


# ── Root→Child Inheritance ──────────────────────────────

def test_child_inherits_approval_from_parent():
    """Child without approval_policy inherits from parent."""
    child = _task(approval_policy=None)
    parent = _task(approval_policy="on_plan")
    decision, reason = evaluate_promote_decision(child, parent_task=parent)
    assert decision == NEEDS_APPROVAL
    assert "approval_policy" in reason


def test_child_inherits_autonomy_from_parent():
    """Child without autonomy inherits execute_low_risk from parent."""
    child = _task(autonomy_level=None)
    parent = _task(autonomy_level="execute_low_risk")
    decision, _ = evaluate_promote_decision(child, parent_task=parent)
    assert decision == AUTO_PROMOTE


def test_child_inherits_auth_from_parent():
    """Child without requires_auth inherits from parent."""
    child = _task(requires_auth=False)
    parent = _task(requires_auth=True)
    decision, reason = evaluate_promote_decision(child, parent_task=parent)
    assert decision == NEEDS_APPROVAL
    assert "credential" in reason.lower()


def test_mixed_parent_needs_approval():
    """Parent request_kind=mixed → child needs approval."""
    child = _task()
    parent = _task(request_kind="mixed")
    decision, reason = evaluate_promote_decision(child, parent_task=parent)
    assert decision == NEEDS_APPROVAL
    assert "mixed" in reason.lower()


def test_child_own_fields_override_parent():
    """Child's own explicit fields take precedence."""
    child = _task(autonomy_level="manual_dispatch_required")
    parent = _task(autonomy_level="execute_low_risk")
    decision, _ = evaluate_promote_decision(child, parent_task=parent)
    assert decision == MANUAL_WAIT  # Child wins


# ── Edge Cases ──────────────────────────────────────────

def test_string_tags_handled():
    """Tags als Strings werden korrekt verarbeitet."""
    decision, _ = evaluate_promote_decision(_task(tags=["infra"]))
    assert decision == NEEDS_APPROVAL


def test_priority_order_manual_beats_approval():
    """manual > approval."""
    decision, _ = evaluate_promote_decision(
        _task(autonomy_level="manual_dispatch_required", approval_policy="on_plan")
    )
    assert decision == MANUAL_WAIT


def test_credential_bound_with_auth_beats_execute_low_risk():
    """credential_bound + Auth darf trotz execute_low_risk nicht auto-promoten."""
    decision, reason = evaluate_promote_decision(
        _task(
            autonomy_level="execute_low_risk",
            delegation_type="credential_bound",
            requires_auth=True,
        )
    )
    assert decision == NEEDS_APPROVAL
    assert "credential" in reason.lower()


def test_priority_order_explicit_approval_beats_auto():
    """Explicit approval policies still beat auto-promote intent."""
    decision, _ = evaluate_promote_decision(
        _task(autonomy_level="execute_low_risk", approval_policy="always")
    )
    assert decision == NEEDS_APPROVAL


def test_approval_policy_never_does_not_beat_execute_low_risk():
    """approval_policy=never is permissive and should not override execute_low_risk."""
    decision, _ = evaluate_promote_decision(
        _task(autonomy_level="execute_low_risk", approval_policy="never")
    )
    assert decision == AUTO_PROMOTE
