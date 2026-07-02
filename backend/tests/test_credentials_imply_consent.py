"""Tests: Vorhandene Credentials = Operator-Consent, kein separates Approval."""
from unittest.mock import MagicMock, AsyncMock, patch
import pytest
from app.services.dispatch_gating import AUTO_PROMOTE, NEEDS_APPROVAL, MANUAL_WAIT, evaluate_promote_decision


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


# ── Positiv: Credentials vorhanden → kein Approval wegen Auth ───

def test_child_with_creds_and_auth_no_approval():
    """Child hat Credentials + requires_auth → kein Approval."""
    child = _task(requires_auth=True, credentials_encrypted="encrypted-data")
    d, _ = evaluate_promote_decision(child)
    assert d == AUTO_PROMOTE


def test_root_creds_child_auth_no_approval():
    """Root hat Credentials, Child braucht Auth → kein Approval."""
    child = _task(requires_auth=True)
    parent = _task(credentials_encrypted="encrypted-data")
    d, _ = evaluate_promote_decision(child, parent_task=parent)
    assert d == AUTO_PROMOTE


def test_credential_bound_with_creds_no_approval():
    """credential_bound + Credentials vorhanden → kein Approval."""
    child = _task(delegation_type="credential_bound", requires_auth=True, credentials_encrypted="enc")
    d, _ = evaluate_promote_decision(child)
    assert d == AUTO_PROMOTE


def test_credential_bound_root_creds_no_approval():
    """credential_bound Child, Root hat Credentials → kein Approval."""
    child = _task(delegation_type="credential_bound", requires_auth=True)
    parent = _task(credentials_encrypted="enc")
    d, _ = evaluate_promote_decision(child, parent_task=parent)
    assert d == AUTO_PROMOTE


def test_explicit_consent_also_works():
    """credential_consent=True funktioniert weiterhin."""
    child = _task(requires_auth=True)
    parent = _task(credential_consent=True)
    d, _ = evaluate_promote_decision(child, parent_task=parent)
    assert d == AUTO_PROMOTE


# ── Negativ: Keine Credentials → weiter Approval ───────────────

def test_auth_without_creds_needs_approval():
    """requires_auth aber keine Credentials → Approval."""
    child = _task(requires_auth=True)
    d, r = evaluate_promote_decision(child)
    assert d == NEEDS_APPROVAL
    assert "credentials" in r.lower()


def test_credential_bound_without_creds_needs_approval():
    """credential_bound ohne Credentials → Approval."""
    child = _task(delegation_type="credential_bound", requires_auth=True)
    d, _ = evaluate_promote_decision(child)
    assert d == NEEDS_APPROVAL


# ── Weiter konservativ trotz Credentials ────────────────────────

def test_infra_tag_still_approval_despite_creds():
    """infra-Tag → Approval trotz vorhandener Credentials."""
    child = _task(requires_auth=True, credentials_encrypted="enc", tags=["infra"])
    d, r = evaluate_promote_decision(child)
    assert d == NEEDS_APPROVAL
    assert "infra" in r


def test_mixed_parent_still_approval_despite_creds():
    """mixed Parent → Approval trotz Credentials."""
    child = _task(requires_auth=True, credentials_encrypted="enc")
    parent = _task(request_kind="mixed")
    d, r = evaluate_promote_decision(child, parent_task=parent)
    assert d == NEEDS_APPROVAL
    assert "mixed" in r.lower()


def test_approval_policy_still_wins_over_creds():
    """approval_policy=on_plan → Approval trotz Credentials."""
    child = _task(requires_auth=True, credentials_encrypted="enc", approval_policy="on_plan")
    d, _ = evaluate_promote_decision(child)
    assert d == NEEDS_APPROVAL


def test_manual_dispatch_still_wins_over_creds():
    """manual_dispatch_required → manual_wait trotz Credentials."""
    child = _task(requires_auth=True, credentials_encrypted="enc", autonomy_level="manual_dispatch_required")
    d, _ = evaluate_promote_decision(child)
    assert d == MANUAL_WAIT


# ── Regression ──────────────────────────────────────────────────

def test_no_auth_task_unaffected():
    """Tasks ohne Auth bleiben unveraendert."""
    child = _task(requires_auth=False)
    d, _ = evaluate_promote_decision(child)
    assert d == AUTO_PROMOTE
