"""Tests for Phase 1.5 delegation contracts.

Structured required fields per delegation_type.
Guards apply at task creation (agent + dashboard).
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine
from app.services.delegation_contracts import validate_delegation_contract

_BROADCAST_PATCH = patch("app.services.activity.broadcast", new_callable=AsyncMock)
_RPC_PATCH = patch("app.routers.agent_scoped.rpc", AsyncMock(connected=True), create=True)
_ENCRYPT_PATCH = patch("app.services.encryption.encrypt", return_value="encrypted_test_value")


async def _setup_contract_scenario():
    """Create board + lead + developer."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    lead_id = uuid.uuid4()
    dev_id = uuid.uuid4()
    source_task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(
            id=board_id, name="Contract Board", slug=f"contract-{uuid.uuid4().hex[:8]}",
        )
        s.add(board)

        lead_token_raw, lead_token_hash = generate_agent_token()
        lead = Agent(
            id=lead_id, name="Henry", role="lead",
            board_id=board_id, agent_token_hash=lead_token_hash,
            is_board_lead=True,
            scopes=["tasks:read", "tasks:write", "tasks:create", "tasks:manage"],
        )
        s.add(lead)

        dev_token_raw, dev_token_hash = generate_agent_token()
        dev = Agent(
            id=dev_id, name="Sparky", role="developer",
            board_id=board_id, agent_token_hash=dev_token_hash,
            is_board_lead=False,
            scopes=["tasks:read", "tasks:write", "tasks:create"],
        )
        s.add(dev)

        # Source task for review tests
        source_task = Task(
            id=source_task_id, board_id=board_id,
            title="Source Task fuer Review", status="review",
        )
        s.add(source_task)

        # Parent task with branch_name for the inheritance test
        parent_with_branch = Task(
            id=uuid.uuid4(), board_id=board_id,
            title="Parent mit Branch", status="in_progress",
            branch_name="feature/inherited-branch",
            requires_auth=True,
        )
        s.add(parent_with_branch)

        await s.commit()

    return {
        "board_id": board_id,
        "lead_id": lead_id, "lead_token": lead_token_raw,
        "dev_id": dev_id, "dev_token": dev_token_raw,
        "source_task_id": source_task_id,
        "parent_with_branch_id": parent_with_branch.id,
    }


# ────────────────────────────────────────────────────────────
# Unit Tests: validate_delegation_contract()
# ────────────────────────────────────────────────────────────

def test_unit_code_change_missing_branch():
    errors, warnings = validate_delegation_contract("code_change", {
        "acceptance_criteria": "Tests gruen",
    })
    assert any("missing_branch_name" in e for e in errors)


def test_unit_code_change_missing_criteria():
    errors, warnings = validate_delegation_contract("code_change", {
        "branch_name": "feature/test",
    })
    assert any("missing_acceptance_criteria" in e for e in errors)


def test_unit_code_change_complete():
    errors, warnings = validate_delegation_contract("code_change", {
        "branch_name": "feature/test",
        "acceptance_criteria": "Tests gruen",
    })
    assert errors == []


def test_unit_credential_bound_missing_creds():
    errors, warnings = validate_delegation_contract("credential_bound", {
        "target_url": "http://localhost",
        "acceptance_criteria": "Login funktioniert",
    })
    assert any("missing_credentials" in e for e in errors)


def test_unit_visual_proof_missing_url():
    errors, warnings = validate_delegation_contract("visual_proof", {
        "acceptance_criteria": "Screenshot zeigt Dashboard",
    })
    assert any("missing_target_url" in e for e in errors)


def test_unit_visual_proof_without_branch_passes_silently():
    """visual_proof has no `recommended` fields — branch_name is intentionally
    omitted because verification tasks often run against deployed URLs with
    no associated code branch (2026-05-18: removed to stop Discord noise).
    """
    errors, warnings = validate_delegation_contract("visual_proof", {
        "target_url": "http://localhost/tasks",
        "acceptance_criteria": "Screenshot zeigt Dashboard",
        "expected_content": "Dashboard mit Sidebar und Task-Liste",
    })
    assert errors == []
    assert warnings == []


def test_unit_visual_proof_missing_expected_content():
    errors, warnings = validate_delegation_contract("visual_proof", {
        "target_url": "http://localhost/tasks",
        "acceptance_criteria": "Screenshot zeigt Dashboard",
    })
    assert any("missing_expected_content" in e for e in errors)


def test_unit_review_missing_source():
    errors, warnings = validate_delegation_contract("review", {})
    assert any("missing_source_task_id" in e for e in errors)


def test_unit_no_delegation_type_passes():
    errors, warnings = validate_delegation_contract(None, {})
    assert errors == []


def test_unit_conditional_requires_auth():
    errors, warnings = validate_delegation_contract("code_change", {
        "branch_name": "feature/test",
        "acceptance_criteria": "OK",
        "requires_auth": True,
    })
    assert any("missing_credentials" in e for e in errors)


# ────────────────────────────────────────────────────────────
# Integration Tests: API level
# ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_api_code_change_without_branch_blocked(client):
    """code_change without branch_name → 422."""
    ids = await _setup_contract_scenario()

    with _BROADCAST_PATCH, _RPC_PATCH:
        resp = await client.post(
            f"/api/v1/agent/boards/{ids['board_id']}/tasks",
            headers={"Authorization": f"Bearer {ids['lead_token']}"},
            json={
                "title": "Code Task ohne Branch",
                "description": "A" * 60,
                "assigned_agent_id": str(ids["dev_id"]),
                "delegation_type": "code_change",
                "acceptance_criteria": "Tests gruen",
            },
        )

    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"
    assert "missing_branch_name" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_api_code_change_complete_passes(client):
    """code_change with all fields → 201."""
    ids = await _setup_contract_scenario()

    with _BROADCAST_PATCH, _RPC_PATCH:
        resp = await client.post(
            f"/api/v1/agent/boards/{ids['board_id']}/tasks",
            headers={"Authorization": f"Bearer {ids['lead_token']}"},
            json={
                "title": "Code Task komplett",
                "description": "A" * 60,
                "assigned_agent_id": str(ids["dev_id"]),
                "delegation_type": "code_change",
                "branch_name": "feature/test-contract",
                "acceptance_criteria": "Tests gruen, kein bestehender Code kaputt",
            },
        )

    assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert data["delegation_type"] == "code_change"
    assert data["branch_name"] == "feature/test-contract"


@pytest.mark.asyncio
async def test_api_credential_bound_without_creds_blocked(client):
    """credential_bound without credentials → 422."""
    ids = await _setup_contract_scenario()

    with _BROADCAST_PATCH, _RPC_PATCH:
        resp = await client.post(
            f"/api/v1/agent/boards/{ids['board_id']}/tasks",
            headers={"Authorization": f"Bearer {ids['lead_token']}"},
            json={
                "title": "Login Task ohne Creds",
                "description": "A" * 60,
                "assigned_agent_id": str(ids["dev_id"]),
                "delegation_type": "credential_bound",
                "target_url": "http://localhost/login",
                "acceptance_criteria": "Login funktioniert",
            },
        )

    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"
    assert "missing_credentials" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_api_no_delegation_type_legacy_passes(client):
    """Without delegation_type → legacy, no contract check → 201."""
    ids = await _setup_contract_scenario()

    with _BROADCAST_PATCH, _RPC_PATCH:
        resp = await client.post(
            f"/api/v1/agent/boards/{ids['board_id']}/tasks",
            headers={"Authorization": f"Bearer {ids['lead_token']}"},
            json={
                "title": "Legacy Task ohne Contract",
                "description": "A" * 60,
                "assigned_agent_id": str(ids["dev_id"]),
            },
        )

    assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_api_branch_inherited_from_parent(client):
    """code_change child inherits branch_name from the parent."""
    ids = await _setup_contract_scenario()

    with _BROADCAST_PATCH, _RPC_PATCH, _ENCRYPT_PATCH:
        resp = await client.post(
            f"/api/v1/agent/boards/{ids['board_id']}/tasks",
            headers={"Authorization": f"Bearer {ids['lead_token']}"},
            json={
                "title": "Child erbt Branch",
                "description": "A" * 60,
                "assigned_agent_id": str(ids["dev_id"]),
                "delegation_type": "code_change",
                "acceptance_criteria": "Tests gruen",
                "parent_task_id": str(ids["parent_with_branch_id"]),
                "credentials": "test:pass",  # Parent has requires_auth=True → inherited → credentials needed
                # branch_name NOT set → should inherit from the parent
            },
        )

    assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert data["branch_name"] == "feature/inherited-branch"
