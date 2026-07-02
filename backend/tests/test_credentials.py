"""Tests fuer Credentials Vault CRUD Router."""
import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_create_credential_login(auth_client: AsyncClient):
    resp = await auth_client.post(
        "/api/v1/credentials",
        json={
            "name": "Test Login",
            "credential_type": "login",
            "data": {"username": "admin", "password": "secret123"},
            "url": "https://example.com",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Test Login"
    assert body["credential_type"] == "login"
    assert "****" in body["data_masked"]["password"]
    assert body["data_masked"]["username"] == "admin"
    assert body["url"] == "https://example.com"


@pytest.mark.asyncio
async def test_create_credential_token(auth_client: AsyncClient):
    resp = await auth_client.post(
        "/api/v1/credentials",
        json={
            "name": "Test Token",
            "credential_type": "token",
            "data": {"token": "ghp_abc123xyz789"},
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["credential_type"] == "token"
    assert "****" in body["data_masked"]["token"]


@pytest.mark.asyncio
async def test_create_credential_custom(auth_client: AsyncClient):
    resp = await auth_client.post(
        "/api/v1/credentials",
        json={
            "name": "SSH Key",
            "credential_type": "custom",
            "data": {"content": "-----BEGIN RSA-----\nsome key data\n-----END RSA-----"},
        },
    )
    assert resp.status_code == 201
    assert resp.json()["credential_type"] == "custom"


@pytest.mark.asyncio
async def test_list_credentials(auth_client: AsyncClient):
    # Create two
    await auth_client.post("/api/v1/credentials", json={"name": "A", "credential_type": "token", "data": {"token": "aaa"}})
    await auth_client.post("/api/v1/credentials", json={"name": "B", "credential_type": "token", "data": {"token": "bbb"}})

    resp = await auth_client.get("/api/v1/credentials")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) >= 2


@pytest.mark.asyncio
async def test_update_credential(auth_client: AsyncClient):
    create_resp = await auth_client.post(
        "/api/v1/credentials",
        json={"name": "Update Me", "credential_type": "login", "data": {"username": "old", "password": "old"}, "url": "https://example.com/login"},
    )
    cred_id = create_resp.json()["id"]

    resp = await auth_client.patch(
        f"/api/v1/credentials/{cred_id}",
        json={"name": "Updated", "data": {"username": "new", "password": "newpass"}},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Updated"
    assert resp.json()["data_masked"]["username"] == "new"


@pytest.mark.asyncio
async def test_delete_credential(auth_client: AsyncClient):
    create_resp = await auth_client.post(
        "/api/v1/credentials",
        json={"name": "Delete Me", "credential_type": "token", "data": {"token": "xxx"}},
    )
    cred_id = create_resp.json()["id"]

    resp = await auth_client.delete(f"/api/v1/credentials/{cred_id}")
    assert resp.status_code == 204

    resp = await auth_client.get("/api/v1/credentials")
    ids = [c["id"] for c in resp.json()]
    assert cred_id not in ids


@pytest.mark.asyncio
async def test_delete_credential_sets_null_on_task(auth_client: AsyncClient, make_board, make_task):
    """Deleting a credential should SET NULL on tasks referencing it."""
    board = await make_board(name="Cred Test Board", slug="cred-test")

    create_resp = await auth_client.post(
        "/api/v1/credentials",
        json={"name": "Will Delete", "credential_type": "token", "data": {"token": "yyy"}},
    )
    cred_id = create_resp.json()["id"]

    # Create task with credential_id via DB (avoids SSE/Redis dependency)
    import uuid
    task = await make_task(board_id=board.id, title="Test Task", credential_id=uuid.UUID(cred_id))
    task_id = str(task.id)

    # Delete credential
    await auth_client.delete(f"/api/v1/credentials/{cred_id}")

    # Task should still exist but credential_id should be null
    task_resp = await auth_client.get(f"/api/v1/boards/{board.id}/tasks/{task_id}")
    assert task_resp.json()["credential_id"] is None


@pytest.mark.asyncio
async def test_login_credential_without_url_rejected_at_create(auth_client: AsyncClient):
    """credential_type='login' ohne url muss 422 werfen — verhindert silent
    422 beim spaeteren mc verify --login-as Vault-Resolve."""
    resp = await auth_client.post(
        "/api/v1/credentials",
        json={
            "name": "No URL Login",
            "credential_type": "login",
            "data": {"username": "x", "password": "y"},
            # url fehlt absichtlich
        },
    )
    assert resp.status_code == 422
    assert "url" in resp.text.lower()


@pytest.mark.asyncio
async def test_login_credential_with_blank_url_rejected(auth_client: AsyncClient):
    """Whitespace-only url darf nicht durchschluepfen."""
    resp = await auth_client.post(
        "/api/v1/credentials",
        json={
            "name": "Blank URL Login",
            "credential_type": "login",
            "data": {"username": "x", "password": "y"},
            "url": "   ",
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_token_credential_without_url_still_ok(auth_client: AsyncClient):
    """Nur 'login' braucht url — 'token' und 'custom' bleiben unangetastet."""
    resp = await auth_client.post(
        "/api/v1/credentials",
        json={
            "name": "API Token No URL",
            "credential_type": "token",
            "data": {"token": "abc123"},
        },
    )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_update_credential_type_to_login_requires_url(auth_client: AsyncClient):
    """State-aware Validation: credential_type von 'token' auf 'login' ohne
    url updaten muss 422 werfen — sonst entsteht ein orphaned Login-Credential
    ohne url, das beim Vault-Resolve scheitert."""
    create_resp = await auth_client.post(
        "/api/v1/credentials",
        json={"name": "Will Become Login", "credential_type": "token", "data": {"token": "x"}},
    )
    cred_id = create_resp.json()["id"]

    resp = await auth_client.patch(
        f"/api/v1/credentials/{cred_id}",
        json={"credential_type": "login", "data": {"username": "x", "password": "y"}},
    )
    assert resp.status_code == 422
    assert "url" in resp.text.lower()


@pytest.mark.asyncio
async def test_update_login_credential_url_to_blank_rejected(auth_client: AsyncClient):
    """Ein Login-Credential das eine gueltige url hat, darf per PATCH nicht
    auf leere/whitespace url gesetzt werden — sonst entsteht ein orphaned
    Login ohne url das beim Vault-Resolve scheitert."""
    create_resp = await auth_client.post(
        "/api/v1/credentials",
        json={
            "name": "Keep Valid",
            "credential_type": "login",
            "data": {"username": "u", "password": "p"},
            "url": "https://example.com/login",
        },
    )
    cred_id = create_resp.json()["id"]

    resp = await auth_client.patch(
        f"/api/v1/credentials/{cred_id}",
        json={"url": "   "},
    )
    assert resp.status_code == 422
    assert "url" in resp.text.lower()


@pytest.mark.asyncio
async def test_update_existing_login_credential_with_url_passes(auth_client: AsyncClient):
    """Update eines bereits validen Login-Credentials (mit url) ohne url-Feld
    in der Patch-Payload bleibt OK — die existierende url wird nicht angefasst."""
    create_resp = await auth_client.post(
        "/api/v1/credentials",
        json={
            "name": "Valid Login",
            "credential_type": "login",
            "data": {"username": "u", "password": "p"},
            "url": "https://example.com/login",
        },
    )
    cred_id = create_resp.json()["id"]

    resp = await auth_client.patch(
        f"/api/v1/credentials/{cred_id}",
        json={"name": "Renamed Login"},
    )
    assert resp.status_code == 200
    assert resp.json()["url"] == "https://example.com/login"
