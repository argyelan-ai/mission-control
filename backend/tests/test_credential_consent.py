"""Tests for task-scoped credential consent.

Test matrix:
- Root consent + non-destructive auth → skip approval
- Root consent + credential_bound → still approval
- No consent + auth → approval
- consent + high-risk tags → approval
- consent + mixed parent → approval
- Regression: no-auth tasks unaffected
"""
from unittest.mock import MagicMock
import pytest
from app.services.dispatch_gating import (
    AUTO_PROMOTE, NEEDS_APPROVAL, MANUAL_WAIT,
    evaluate_promote_decision,
)


def _task(**kw):
    t = MagicMock()
    t.autonomy_level = kw.get("autonomy_level", "execute_low_risk")
    t.approval_policy = kw.get("approval_policy", None)
    t.requires_auth = kw.get("requires_auth", False)
    t.needs_browser = kw.get("needs_browser", False)
    t.delegation_type = kw.get("delegation_type", "code_change")
    t.tags = kw.get("tags", [])
    t.credential_consent = kw.get("credential_consent", None)
    t.credentials_encrypted = kw.get("credentials_encrypted", None)
    t.request_kind = kw.get("request_kind", None)
    return t


# ── Positive: consent skips auth-approval ───────────────

def test_consent_visual_proof_skips_auth_approval():
    """Root consent + visual_proof + auth → no extra approval."""
    child = _task(requires_auth=True, delegation_type="visual_proof")
    parent = _task(credential_consent=True)
    decision, reason = evaluate_promote_decision(child, parent_task=parent)
    assert decision == AUTO_PROMOTE


def test_consent_code_change_skips_auth_approval():
    """Root consent + code_change + auth → no extra approval."""
    child = _task(requires_auth=True, delegation_type="code_change")
    parent = _task(credential_consent=True)
    decision, _ = evaluate_promote_decision(child, parent_task=parent)
    assert decision == AUTO_PROMOTE


def test_consent_review_skips_auth_approval():
    """Root consent + review + auth → no extra approval."""
    child = _task(requires_auth=True, delegation_type="review")
    parent = _task(credential_consent=True)
    decision, _ = evaluate_promote_decision(child, parent_task=parent)
    assert decision == AUTO_PROMOTE


# ── Negative: consent does NOT unlock dangerous cases ───



def test_no_creds_no_consent_auth_needs_approval():
    """No credentials and no consent → auth needs approval."""
    child = _task(requires_auth=True, delegation_type="visual_proof")
    parent = _task(credential_consent=None)
    decision, reason = evaluate_promote_decision(child, parent_task=parent)
    assert decision == NEEDS_APPROVAL
    assert "credential" in reason.lower()


def test_consent_with_infra_tags_still_approval():
    """consent does not protect against high-risk tags."""
    child = _task(requires_auth=True, delegation_type="code_change", tags=["infra"])
    parent = _task(credential_consent=True)
    decision, reason = evaluate_promote_decision(child, parent_task=parent)
    assert decision == NEEDS_APPROVAL
    assert "infra" in reason


def test_consent_with_mixed_parent_still_approval():
    """consent does not protect against a mixed parent."""
    child = _task(requires_auth=True, delegation_type="code_change")
    parent = _task(credential_consent=True, request_kind="mixed")
    decision, reason = evaluate_promote_decision(child, parent_task=parent)
    assert decision == NEEDS_APPROVAL
    assert "mixed" in reason.lower()


def test_consent_with_explicit_approval_policy():
    """Explicit approval_policy wins over consent."""
    child = _task(requires_auth=True, approval_policy="on_plan")
    parent = _task(credential_consent=True)
    decision, _ = evaluate_promote_decision(child, parent_task=parent)
    assert decision == NEEDS_APPROVAL


# ── Regression: no-auth tasks unaffected ────────────────

def test_no_auth_task_unaffected_by_consent():
    """Tasks without auth remain unaffected by the consent feature."""
    child = _task(requires_auth=False, delegation_type="code_change")
    parent = _task(credential_consent=True)
    decision, _ = evaluate_promote_decision(child, parent_task=parent)
    assert decision == AUTO_PROMOTE


def test_no_auth_no_consent_normal():
    """Standard case without auth → behaves as before."""
    child = _task(requires_auth=False)
    decision, _ = evaluate_promote_decision(child)
    assert decision == AUTO_PROMOTE
