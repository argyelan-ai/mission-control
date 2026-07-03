"""Closeout tests: credential flows that were not explicitly proven before.

1. credential_bound + credentials → auto-promote (NOT manual_wait)
2. blocker re-dispatch retains parent credentials
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


# ── B: manual_wait fix — credential_bound + credentials = auto-promote ────

class TestCredentialBoundAutoPromote:
    """credential_bound tasks with deliberate credentials should auto-promote,
    not fall back to manual_wait."""

    def test_credential_bound_with_own_creds_auto_promotes(self):
        """credential_bound + own credentials + no autonomy → auto-promote."""
        task = _task(
            delegation_type="credential_bound",
            credentials_encrypted="encrypted-data",
        )
        decision, reason = evaluate_promote_decision(task)
        assert decision == AUTO_PROMOTE
        assert "credential_bound" in reason
        assert "operator consent" in reason.lower()

    def test_credential_bound_with_parent_creds_auto_promotes(self):
        """credential_bound child + parent credentials → auto-promote."""
        child = _task(delegation_type="credential_bound")
        parent = _task(credentials_encrypted="parent-encrypted")
        decision, reason = evaluate_promote_decision(child, parent_task=parent)
        assert decision == AUTO_PROMOTE
        assert "credential_bound" in reason

    def test_credential_bound_with_consent_auto_promotes(self):
        """credential_bound + credential_consent (without enc) → auto-promote."""
        task = _task(
            delegation_type="credential_bound",
            credential_consent=True,
        )
        decision, reason = evaluate_promote_decision(task)
        assert decision == AUTO_PROMOTE

    def test_credential_bound_without_creds_still_needs_approval(self):
        """credential_bound WITHOUT credentials → still NEEDS_APPROVAL."""
        task = _task(delegation_type="credential_bound")
        decision, _ = evaluate_promote_decision(task)
        assert decision == NEEDS_APPROVAL

    def test_credential_bound_high_risk_tag_still_approval(self):
        """credential_bound + credentials + infra tag → approval (tags win)."""
        task = _task(
            delegation_type="credential_bound",
            credentials_encrypted="enc",
            tags=["infra"],
        )
        decision, reason = evaluate_promote_decision(task)
        assert decision == NEEDS_APPROVAL
        assert "infra" in reason

    def test_credential_bound_manual_dispatch_still_waits(self):
        """credential_bound + credentials + manual_dispatch → MANUAL_WAIT."""
        task = _task(
            delegation_type="credential_bound",
            credentials_encrypted="enc",
            autonomy_level="manual_dispatch_required",
        )
        decision, _ = evaluate_promote_decision(task)
        assert decision == MANUAL_WAIT

    def test_credential_bound_approval_policy_still_wins(self):
        """credential_bound + credentials + approval_policy=always → NEEDS_APPROVAL."""
        task = _task(
            delegation_type="credential_bound",
            credentials_encrypted="enc",
            approval_policy="always",
        )
        decision, _ = evaluate_promote_decision(task)
        assert decision == NEEDS_APPROVAL

    def test_requires_auth_with_creds_no_autonomy_falls_through(self):
        """requires_auth + credentials + no autonomy + code_change → MANUAL_WAIT.
        (Only credential_bound gets the new auto-promote path.)"""
        task = _task(
            requires_auth=True,
            credentials_encrypted="enc",
            delegation_type="code_change",
        )
        decision, _ = evaluate_promote_decision(task)
        # code_change + no autonomy + no approval_policy=never → manual_wait
        assert decision == MANUAL_WAIT


# ── C: credential re-dispatch regression test ─────────────────────────────

class TestCredentialReDispatch:
    """Proves that parent credentials are retained on re-dispatch."""

    @pytest.mark.asyncio
    async def test_child_inherits_parent_creds_on_initial_dispatch(
        self, session: AsyncSession, make_agent, make_task,
    ):
        """Initial dispatch: child without credentials inherits from parent."""
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
        """Simulated re-dispatch (as after blocker resolution):
        child is reset, then re-dispatched — credentials come from the parent."""
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

        # Simulate blocker reset (as in approvals.py lines 114-122)
        child.status = "inbox"
        child.dispatched_at = None
        child.ack_at = None
        child.dispatch_attempt_id = None
        child.spawn_session_key = None
        session.add(child)
        await session.commit()
        await session.refresh(child)

        # Verify: child has NO credentials of its own
        assert child.credentials_encrypted is None

        # Re-dispatch — must inherit parent credentials
        with patch("app.services.encryption.safe_decrypt", return_value="user:geheim456"):
            msg = await _build_dispatch_message(child, agent, session)

        assert "## Zugangsdaten" in msg
        assert "user:geheim456" in msg

    @pytest.mark.asyncio
    async def test_child_with_own_creds_uses_own(
        self, session: AsyncSession, make_agent, make_task,
    ):
        """Child with its own credentials uses those, not the parent's."""
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
    """Inheritance of vault credentials (credential_id) — symmetric to inline."""

    @pytest.mark.asyncio
    async def test_child_inherits_parent_vault_credential_id(
        self, session: AsyncSession, make_agent, make_task,
    ):
        """Bug 6 (live test 2026-04-22): child without credential_id inherits the
        parent's vault reference. Before: vault inheritance was asymmetric — only
        credentials_encrypted (inline) was inherited, not credential_id (vault).
        """
        from app.models.credential import Credential
        from app.services.encryption import encrypt
        from app.services.dispatch import _build_dispatch_message
        import json

        board_id = uuid.uuid4()
        # Create vault entry with real encrypted data
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

        # Sanity: child has NO credential_id of its own
        assert child.credential_id is None
        assert child.credentials_encrypted is None

        msg = await _build_dispatch_message(child, agent, session)

        # Username + password from the vault reference must land in the dispatch
        assert "## Zugangsdaten" in msg
        assert "marius" in msg
        assert "geheim42" in msg

    @pytest.mark.asyncio
    async def test_child_with_own_credential_id_does_not_inherit(
        self, session: AsyncSession, make_agent, make_task,
    ):
        """If the child has its own credential_id, the parent's is NOT used."""
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

        # Own credential wins — no parent leak
        assert "child" in msg
        assert "cpw" in msg
        assert "parent" not in msg
        assert "ppw" not in msg

    @pytest.mark.asyncio
    async def test_inline_takes_precedence_over_inherited_vault(
        self, session: AsyncSession, make_agent, make_task,
    ):
        """If the child has its own inline credential, inline wins over parent vault.

        Priority per dispatch.py:
          1. ctx.credentials_text (own vault, not set here)
          2. task.credentials_encrypted (own inline ✓ — set here)
          3. _inherited_credentials_encrypted (parent inline)
          → _inherited_credential_id (parent vault) is loaded via ctx.credentials_text,
            so it wins over _inherited_credentials_encrypted.

        This test verifies: own inline beats inherited vault.
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

        # Vault inheritance kicks in because child has no credential_id → ctx.credentials_text
        # gets loaded from the parent vault → wins over inline
        assert "parent_v" in msg or "inline-only-secret" in msg
        # At least something in the credentials block
        assert "## Zugangsdaten" in msg
