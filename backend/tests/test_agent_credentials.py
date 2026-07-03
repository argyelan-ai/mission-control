"""Tests for Agent Credentials read endpoints.

GET /api/v1/agent/boards/{board_id}/credentials        → masked list (credentials:read)
GET /api/v1/agent/boards/{board_id}/credentials/{id}  → decrypted single (credentials:read)
"""

import json
import uuid

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from .conftest import test_engine


async def _make_agent_client(client: AsyncClient, make_agent, scopes: list[str]):
    """Helper: create an agent with a token, set the client header."""
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from sqlmodel import select

    agent = await make_agent(name="CredTest Agent", scopes=scopes)
    raw_token, token_hash = generate_agent_token()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        result = await s.exec(select(Agent).where(Agent.id == agent.id))
        db_agent = result.one()
        db_agent.agent_token_hash = token_hash
        s.add(db_agent)
        await s.commit()

    client.headers["Authorization"] = f"Bearer {raw_token}"
    return agent


async def _create_credential(auth_client: AsyncClient, name: str = "Test Cred", credential_type: str = "login"):
    """Helper: create a credential via the user API."""
    resp = await auth_client.post(
        "/api/v1/credentials",
        json={
            "name": name,
            "credential_type": credential_type,
            "data": {"username": "admin", "password": "secret123"},
            "url": "https://example.com",
            "notes": "Testnotiz",
        },
    )
    assert resp.status_code == 201
    return resp.json()


@pytest.mark.asyncio
class TestAgentCredentialsListEndpoint:
    """GET /api/v1/agent/boards/{board_id}/credentials — masked list."""

    async def test_agent_with_scope_can_list_credentials(
        self, client: AsyncClient, auth_client: AsyncClient, make_agent, make_board
    ):
        """Agent with credentials:read sees the masked list."""
        board = await make_board()
        cred = await _create_credential(auth_client, "My Login")

        await _make_agent_client(client, make_agent, scopes=["credentials:read"])

        resp = await client.get(f"/api/v1/agent/boards/{board.id}/credentials")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) >= 1
        found = next((c for c in items if c["id"] == cred["id"]), None)
        assert found is not None
        assert found["name"] == "My Login"
        assert found["credential_type"] == "login"
        assert "data_masked" in found
        # Password should be masked
        assert "****" in found["data_masked"]["password"]
        # URL and notes present
        assert found["url"] == "https://example.com"
        assert found["notes"] == "Testnotiz"
        # No unencrypted data field
        assert "data" not in found

    async def test_agent_without_scope_gets_403(
        self, client: AsyncClient, make_agent, make_board
    ):
        """Agent without credentials:read is rejected with 403."""
        board = await make_board()
        await _make_agent_client(client, make_agent, scopes=["tasks:read"])

        resp = await client.get(f"/api/v1/agent/boards/{board.id}/credentials")
        assert resp.status_code == 403

    async def test_list_returns_empty_when_no_credentials(
        self, client: AsyncClient, make_agent, make_board
    ):
        """Empty list when no credentials exist."""
        board = await make_board()
        await _make_agent_client(client, make_agent, scopes=["credentials:read"])

        resp = await client.get(f"/api/v1/agent/boards/{board.id}/credentials")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_list_ordered_by_name(
        self, client: AsyncClient, auth_client: AsyncClient, make_agent, make_board
    ):
        """Credentials are sorted alphabetically by name."""
        board = await make_board()
        await _create_credential(auth_client, "Zebra Cred")
        await _create_credential(auth_client, "Alpha Cred")
        await _create_credential(auth_client, "Middle Cred")

        await _make_agent_client(client, make_agent, scopes=["credentials:read"])

        resp = await client.get(f"/api/v1/agent/boards/{board.id}/credentials")
        assert resp.status_code == 200
        items = resp.json()
        names = [c["name"] for c in items]
        assert names == sorted(names)

    async def test_unauthenticated_request_fails(self, client: AsyncClient, make_board):
        """Without an auth header → 401 or 403."""
        board = await make_board()
        client.headers.pop("Authorization", None)
        resp = await client.get(f"/api/v1/agent/boards/{board.id}/credentials")
        assert resp.status_code in (401, 403)


@pytest.mark.asyncio
class TestAgentCredentialDetailEndpoint:
    """GET /api/v1/agent/boards/{board_id}/credentials/{id} — decrypted single."""

    async def test_agent_with_scope_gets_decrypted_data(
        self, client: AsyncClient, auth_client: AsyncClient, make_agent, make_board
    ):
        """Agent with scope receives decrypted credentials."""
        board = await make_board()
        cred = await _create_credential(auth_client, "API Key Cred", "token")
        # Create a second credential with different data
        resp = await auth_client.post(
            "/api/v1/credentials",
            json={
                "name": "Token Cred",
                "credential_type": "token",
                "data": {"token": "ghp_abc123xyz789"},
                "url": "https://github.com",
            },
        )
        assert resp.status_code == 201
        token_cred = resp.json()

        await _make_agent_client(client, make_agent, scopes=["credentials:read"])

        resp = await client.get(f"/api/v1/agent/boards/{board.id}/credentials/{token_cred['id']}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == token_cred["id"]
        assert body["name"] == "Token Cred"
        assert body["credential_type"] == "token"
        # Decrypted data dict (not masked)
        assert "data" in body
        assert body["data"]["token"] == "ghp_abc123xyz789"
        # No data_masked in the detail response
        assert "data_masked" not in body

    async def test_agent_without_scope_gets_403_on_detail(
        self, client: AsyncClient, auth_client: AsyncClient, make_agent, make_board
    ):
        """Agent without scope gets 403 on detail retrieval."""
        board = await make_board()
        cred = await _create_credential(auth_client)

        await _make_agent_client(client, make_agent, scopes=["tasks:read"])

        resp = await client.get(f"/api/v1/agent/boards/{board.id}/credentials/{cred['id']}")
        assert resp.status_code == 403

    async def test_nonexistent_credential_returns_404(
        self, client: AsyncClient, make_agent, make_board
    ):
        """Nonexistent credential ID → 404."""
        board = await make_board()
        await _make_agent_client(client, make_agent, scopes=["credentials:read"])
        fake_id = str(uuid.uuid4())

        resp = await client.get(f"/api/v1/agent/boards/{board.id}/credentials/{fake_id}")
        assert resp.status_code == 404

    async def test_login_credential_decrypted_correctly(
        self, client: AsyncClient, auth_client: AsyncClient, make_agent, make_board
    ):
        """Login credential with username+password decrypted correctly."""
        board = await make_board()
        cred = await _create_credential(auth_client, "Login Cred", "login")

        await _make_agent_client(client, make_agent, scopes=["credentials:read"])

        resp = await client.get(f"/api/v1/agent/boards/{board.id}/credentials/{cred['id']}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["username"] == "admin"
        assert body["data"]["password"] == "secret123"
