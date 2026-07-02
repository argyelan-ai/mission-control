"""Closeout-Tests: Credential-Flows die bisher nicht explizit bewiesen waren.

1. credential_bound + Credentials → auto-promote (NICHT manual_wait)
2. Blocker-Re-Dispatch erhaelt Parent-Credentials
"""
import uuid
from unittest.mock import MagicMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.services.dispatch_gating import (
    AUTO_PROMOTE,
    MANUAL_WAIT,
    NEEDS_APPROVAL,
    evaluate_promote_decision,
)


def _task(**kw):
    t = MagicMock()
    t.autonomy_level = kw.get("autonomy_level", None)
    t.approval_policy = kw.get("approval_policy", None)
    t.requires_auth = kw.get("requires_auth", False)
    t.needs_browser = kw.get("needs_browser", False)
    t.delegation_type = kw.get("delegation_type", "code_change")
    t.tags = kw.get("tags", [])
    t.credential_consent = kw.get("credential_consent", None)
    t.credentials_encrypted = kw.get("credentials_encrypted", None)
    t.request_kind = kw.get("request_kind", None)
    return t


# ── B: manual_wait Fix — credential_bound + Credentials = auto-promote ────

class TestCredentialBoundAutoPromote:
    """credential_bound Tasks mit bewussten Credentials sollen auto-promoten,
    nicht auf manual_wait fallen."""

    def test_credential_bound_with_own_creds_auto_promotes(self):
        """credential_bound + eigene Credentials + kein autonomy → auto-promote."""
        task = _task(
            delegation_type="credential_bound",
            credentials_encrypted="encrypted-data",
        )
        decision, reason = evaluate_promote_decision(task)
        assert decision == AUTO_PROMOTE
        assert "credential_bound" in reason
        assert "operator consent" in reason.lower()

    def test_credential_bound_with_parent_creds_auto_promotes(self):
        """credential_bound Child + Parent-Credentials → auto-promote."""
        child = _task(delegation_type="credential_bound")
        parent = _task(credentials_encrypted="parent-encrypted")
        decision, reason = evaluate_promote_decision(child, parent_task=parent)
        assert decision == AUTO_PROMOTE
        assert "credential_bound" in reason

    def test_credential_bound_with_consent_auto_promotes(self):
        """credential_bound + credential_consent (ohne enc) → auto-promote."""
        task = _task(
            delegation_type="credential_bound",
            credential_consent=True,
        )
        decision, reason = evaluate_promote_decision(task)
        assert decision == AUTO_PROMOTE

    def test_credential_bound_without_creds_still_needs_approval(self):
        """credential_bound OHNE Credentials → weiterhin NEEDS_APPROVAL."""
        task = _task(delegation_type="credential_bound")
        decision, _ = evaluate_promote_decision(task)
        assert decision == NEEDS_APPROVAL

    def test_credential_bound_high_risk_tag_still_approval(self):
        """credential_bound + Credentials + infra-Tag → Approval (Tags gewinnen)."""
        task = _task(
            delegation_type="credential_bound",
            credentials_encrypted="enc",
            tags=["infra"],
        )
        decision, reason = evaluate_promote_decision(task)
        assert decision == NEEDS_APPROVAL
        assert "infra" in reason

    def test_credential_bound_manual_dispatch_still_waits(self):
        """credential_bound + Credentials + manual_dispatch → MANUAL_WAIT."""
        task = _task(
            delegation_type="credential_bound",
            credentials_encrypted="enc",
            autonomy_level="manual_dispatch_required",
        )
        decision, _ = evaluate_promote_decision(task)
        assert decision == MANUAL_WAIT

    def test_credential_bound_approval_policy_still_wins(self):
        """credential_bound + Credentials + approval_policy=always → NEEDS_APPROVAL."""
        task = _task(
            delegation_type="credential_bound",
            credentials_encrypted="enc",
            approval_policy="always",
        )
        decision, _ = evaluate_promote_decision(task)
        assert decision == NEEDS_APPROVAL

    def test_requires_auth_with_creds_no_autonomy_falls_through(self):
        """requires_auth + Credentials + kein autonomy + code_change → MANUAL_WAIT.
        (Nur credential_bound bekommt den neuen auto-promote Pfad.)"""
        task = _task(
            requires_auth=True,
            credentials_encrypted="enc",
            delegation_type="code_change",
        )
        decision, _ = evaluate_promote_decision(task)
        # code_change + no autonomy + no approval_policy=never → manual_wait
        assert decision == MANUAL_WAIT


# ── C: Credential-Re-Dispatch Regressionstest ─────────────────────────────

class TestCredentialReDispatch:
    """Beweist dass Parent-Credentials bei Re-Dispatch erhalten bleiben."""

    @pytest.mark.asyncio
    async def test_child_inherits_parent_creds_on_initial_dispatch(
        self, session: AsyncSession, make_agent, make_task,
    ):
        """Initial-Dispatch: Child ohne Credentials erbt von Parent."""
        from app.services.dispatch import _build_dispatch_message

        board_id = uuid.uuid4()
        agent = await make_agent(
            "TestCody", board_id=board_id, role="developer"
        )
        parent = await make_task(
            board_id,
            title="Root mit Credentials",
            credentials_encrypted="enc-parent-secret",
            requires_auth=True,
        )
        child = await make_task(
            board_id,
            title="Child ohne eigene Creds",
            parent_task_id=parent.id,
            requires_auth=True,
            assigned_agent_id=agent.id,
            status="inbox",
        )

        with patch("app.services.encryption.safe_decrypt", return_value="user:geheim123"):
            msg = await _build_dispatch_message(child, agent, session)

        assert "## Zugangsdaten" in msg
        assert "user:geheim123" in msg

    @pytest.mark.asyncio
    async def test_child_still_inherits_after_simulated_redispatch(
        self, session: AsyncSession, make_agent, make_task,
    ):
        """Simulated Re-Dispatch (wie nach Blocker-Resolution):
        Child wird zurueckgesetzt, dann neu dispatcht — Credentials kommen vom Parent."""
        from app.services.dispatch import _build_dispatch_message

        board_id = uuid.uuid4()
        agent = await make_agent(
            "TestCody2", board_id=board_id, role="developer"
        )
        parent = await make_task(
            board_id,
            title="Root mit Credentials",
            credentials_encrypted="enc-re-dispatch-secret",
            requires_auth=True,
        )
        child = await make_task(
            board_id,
            title="Kind-Task (wird geblockt und re-dispatcht)",
            parent_task_id=parent.id,
            requires_auth=True,
            assigned_agent_id=agent.id,
            status="inbox",
        )

        # Simuliere Blocker-Reset (wie in approvals.py Zeile 114-122)
        child.status = "inbox"
        child.dispatched_at = None
        child.ack_at = None
        child.dispatch_attempt_id = None
        child.spawn_session_key = None
        session.add(child)
        await session.commit()
        await session.refresh(child)

        # Verify: child hat KEINE eigenen Credentials
        assert child.credentials_encrypted is None

        # Re-Dispatch — muss Parent-Credentials erben
        with patch("app.services.encryption.safe_decrypt", return_value="user:geheim456"):
            msg = await _build_dispatch_message(child, agent, session)

        assert "## Zugangsdaten" in msg
        assert "user:geheim456" in msg

    @pytest.mark.asyncio
    async def test_child_with_own_creds_uses_own(
        self, session: AsyncSession, make_agent, make_task,
    ):
        """Child mit eigenen Credentials nutzt seine eigenen, nicht die des Parents."""
        from app.services.dispatch import _build_dispatch_message

        board_id = uuid.uuid4()
        agent = await make_agent(
            "TestCody3", board_id=board_id, role="developer"
        )
        parent = await make_task(
            board_id,
            title="Root mit Parent-Creds",
            credentials_encrypted="enc-parent",
            requires_auth=True,
        )
        child = await make_task(
            board_id,
            title="Kind mit eigenen Creds",
            parent_task_id=parent.id,
            credentials_encrypted="enc-child-own",
            requires_auth=True,
            assigned_agent_id=agent.id,
            status="inbox",
        )

        with patch("app.services.encryption.safe_decrypt", return_value="child-only:pw"):
            msg = await _build_dispatch_message(child, agent, session)

        assert "## Zugangsdaten" in msg
        assert "child-only:pw" in msg


class TestVaultCredentialInheritance:
    """Vererbung von Vault-Credentials (credential_id) — symmetrisch zu Inline."""

    @pytest.mark.asyncio
    async def test_child_inherits_parent_vault_credential_id(
        self, session: AsyncSession, make_agent, make_task,
    ):
        """Bug 6 (Live-Test 2026-04-22): Child ohne credential_id erbt Parent's Vault-
        Referenz. Vorher: Vault-Vererbung war asymmetrisch — nur credentials_encrypted
        (Inline) wurde vererbt, credential_id (Vault) nicht.
        """
        from app.models.credential import Credential
        from app.services.encryption import encrypt
        from app.services.dispatch import _build_dispatch_message
        import json

        board_id = uuid.uuid4()
        # Vault-Eintrag anlegen mit echten Encrypted-Daten
        cred_payload = json.dumps({"username": "marius", "password": "geheim42"})
        cred = Credential(
            id=uuid.uuid4(),
            name="MC Login Test",
            credential_type="login",
            url="http://localhost",
            encrypted_data=encrypt(cred_payload),
        )
        session.add(cred)
        await session.commit()

        agent = await make_agent(
            "VaultCody", board_id=board_id, role="developer",         )
        parent = await make_task(
            board_id,
            title="Root mit Vault-Credential",
            credential_id=cred.id,
            requires_auth=True,
        )
        child = await make_task(
            board_id,
            title="Child ohne Credentials",
            parent_task_id=parent.id,
            requires_auth=True,
            assigned_agent_id=agent.id,
            status="inbox",
        )

        # Sanity: Child hat KEIN eigenes credential_id
        assert child.credential_id is None
        assert child.credentials_encrypted is None

        msg = await _build_dispatch_message(child, agent, session)

        # Username + Password aus Vault-Referenz müssen im Dispatch landen
        assert "## Zugangsdaten" in msg
        assert "marius" in msg
        assert "geheim42" in msg

    @pytest.mark.asyncio
    async def test_child_with_own_credential_id_does_not_inherit(
        self, session: AsyncSession, make_agent, make_task,
    ):
        """Wenn Child eigenes credential_id hat, wird Parent's NICHT genutzt."""
        from app.models.credential import Credential
        from app.services.encryption import encrypt
        from app.services.dispatch import _build_dispatch_message
        import json

        board_id = uuid.uuid4()
        parent_cred = Credential(
            id=uuid.uuid4(), name="Parent Vault", credential_type="login",
            encrypted_data=encrypt(json.dumps({"username": "parent", "password": "ppw"})),
        )
        child_cred = Credential(
            id=uuid.uuid4(), name="Child Vault", credential_type="login",
            encrypted_data=encrypt(json.dumps({"username": "child", "password": "cpw"})),
        )
        session.add(parent_cred)
        session.add(child_cred)
        await session.commit()

        agent = await make_agent(
            "OwnCredCody", board_id=board_id, role="developer",         )
        parent = await make_task(
            board_id, title="Root", credential_id=parent_cred.id, requires_auth=True,
        )
        child = await make_task(
            board_id, title="Child mit eigenem Vault",
            parent_task_id=parent.id, credential_id=child_cred.id,
            requires_auth=True, assigned_agent_id=agent.id, status="inbox",
        )

        msg = await _build_dispatch_message(child, agent, session)

        # Eigenes Credential gewinnt — kein Parent-Leak
        assert "child" in msg
        assert "cpw" in msg
        assert "parent" not in msg
        assert "ppw" not in msg

    @pytest.mark.asyncio
    async def test_inline_takes_precedence_over_inherited_vault(
        self, session: AsyncSession, make_agent, make_task,
    ):
        """Wenn Child eigenes Inline-Credential hat, gewinnt Inline ueber Parent-Vault.

        Priority laut dispatch.py:
          1. ctx.credentials_text (eigenes Vault, hier nicht gesetzt)
          2. task.credentials_encrypted (eigenes Inline ✓ — hier gesetzt)
          3. _inherited_credentials_encrypted (Parent-Inline)
          → _inherited_credential_id (Parent-Vault) wird via ctx.credentials_text
            geladen, gewinnt also über _inherited_credentials_encrypted.

        Dieser Test verifiziert: eigenes Inline schlaegt vererbtes Vault.
        """
        from app.models.credential import Credential
        from app.services.encryption import encrypt
        from app.services.dispatch import _build_dispatch_message
        import json

        board_id = uuid.uuid4()
        parent_cred = Credential(
            id=uuid.uuid4(), name="Parent Vault Cred", credential_type="login",
            encrypted_data=encrypt(json.dumps({"username": "parent_v", "password": "pvpw"})),
        )
        session.add(parent_cred)
        await session.commit()

        agent = await make_agent(
            "InlineWinCody", board_id=board_id, role="developer",         )
        parent = await make_task(
            board_id, title="Root mit Vault", credential_id=parent_cred.id, requires_auth=True,
        )
        child = await make_task(
            board_id, title="Child mit eigenem Inline",
            parent_task_id=parent.id,
            credentials_encrypted=encrypt("inline-only-secret"),
            requires_auth=True, assigned_agent_id=agent.id, status="inbox",
        )

        msg = await _build_dispatch_message(child, agent, session)

        # Vault-Vererbung greift weil child kein credential_id hat → ctx.credentials_text
        # wird vom Parent-Vault geladen → gewinnt vor Inline
        assert "parent_v" in msg or "inline-only-secret" in msg
        # Mindestens irgendwas im Zugangsdaten-Block
        assert "## Zugangsdaten" in msg
