"""MC_AGENT_TOKEN → vault write on every token generation (fresh-install fix).

Found 2026-07-02 (critical): the vault key mc_token_{slug} was only READ
(routers/internal.py::agent_bootstrap), but never written except by the
one-off migration script. Consequence: freshly created agents got no
MC_TOKEN via /internal/bootstrap → poll.sh crash-looped, one-click deploy
was dead on fresh installs.

Covers the three user-reachable paths: create_agent, template instantiate
(_do_instantiate), and reset-token — including end-to-end via the bootstrap
endpoint itself.
"""
import pytest
from sqlmodel import select

from app.models.secret import Secret
from app.services.encryption import safe_decrypt


async def _vault_token(async_session, slug: str) -> str | None:
    result = await async_session.exec(
        select(Secret).where(Secret.key == f"mc_token_{slug}")
    )
    secret = result.first()
    return safe_decrypt(secret.encrypted_value) if secret else None


@pytest.mark.asyncio
async def test_create_agent_writes_token_to_vault(auth_client, async_session):
    resp = await auth_client.post(
        "/api/v1/agents",
        json={"name": "VaultBot", "agent_runtime": "cli-bridge"},
    )
    assert resp.status_code == 201, resp.text
    raw_token = resp.json()["token"]

    # Vault key schema must match the bootstrap lookup (agent.name.lower())
    assert await _vault_token(async_session, "vaultbot") == raw_token


@pytest.mark.asyncio
async def test_bootstrap_delivers_token_after_create(auth_client, async_session):
    """End-to-end: the container bootstrap path finds the fresh token."""
    resp = await auth_client.post(
        "/api/v1/agents",
        json={"name": "BootBot", "agent_runtime": "cli-bridge"},
    )
    assert resp.status_code == 201, resp.text
    raw_token = resp.json()["token"]

    boot = await auth_client.get("/api/v1/internal/bootstrap?agent_name=BootBot")
    assert boot.status_code == 200, boot.text
    assert boot.json()["MC_AGENT_TOKEN"] == raw_token


@pytest.mark.asyncio
async def test_reset_token_rotates_vault_secret(auth_client, async_session):
    created = await auth_client.post(
        "/api/v1/agents",
        json={"name": "RotateBot", "agent_runtime": "cli-bridge"},
    )
    agent_id = created.json()["id"]
    old_token = created.json()["token"]

    reset = await auth_client.post(f"/api/v1/agents/{agent_id}/reset-token")
    assert reset.status_code == 200, reset.text
    new_token = reset.json()["token"]

    assert new_token != old_token
    # Vault must return the NEW token — otherwise the container starts with the old one
    assert await _vault_token(async_session, "rotatebot") == new_token


@pytest.mark.asyncio
async def test_template_instantiate_writes_token_to_vault(async_session):
    """_do_instantiate (template path, also used by approvals/agent-scoped)
    writes the token to the vault. Tested directly instead of via the HTTP
    endpoint, so no provisioning background task runs in the test env."""
    from app.models.agent_template import AgentTemplate
    from app.routers.agent_templates import _do_instantiate

    template = AgentTemplate(
        name="Vaulter",
        emoji="🧰",
        role="developer",
        soul_md="Test soul",
        is_builtin=False,
    )
    async_session.add(template)
    await async_session.commit()
    await async_session.refresh(template)

    agent, raw_token = await _do_instantiate(
        template=template,
        board_id=None,
        name=None,
        model=None,
        session=async_session,
    )

    assert agent.name == "Vaulter"
    assert await _vault_token(async_session, "vaulter") == raw_token


@pytest.mark.asyncio
async def test_delete_agent_removes_vault_secret(auth_client, async_session):
    """Deleting an agent must also remove its mc_token_<slug> vault secret.
    Before 2026-07-11 the DELETE only cleaned up DB FKs, so the token secret
    lingered — and even re-entered docker/.env.agents on the next start-all.sh
    run as a stale token for a non-existent agent.

    The agent is inserted directly (not via POST) to avoid the create
    endpoint's best-effort background provisioning task, which opens a real
    Postgres session and is irrelevant to the delete cascade under test."""
    from app.models.agent import Agent
    from app.services.secrets_helper import upsert_agent_token_secret

    agent = Agent(name="DeleteMe", agent_runtime="cli-bridge")
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)
    await upsert_agent_token_secret(async_session, agent, "tok-xyz")
    assert await _vault_token(async_session, "deleteme") is not None

    delete = await auth_client.delete(f"/api/v1/agents/{agent.id}")
    assert delete.status_code == 204, delete.text

    async_session.expire_all()
    assert await _vault_token(async_session, "deleteme") is None


@pytest.mark.asyncio
async def test_delete_agent_removes_vault_secret_after_rename(auth_client, async_session):
    """Regression (reviewer 2026-07-11): a plain PATCH rename does NOT rotate
    the token. Under the slug scheme the vault key is derived from the stable
    insert-time slug (never changed on rename), so writer AND delete agree on
    the key regardless of the current name. Multi-word name so the space→dash
    slug mapping is exercised."""
    from app.models.agent import Agent
    from app.services.secrets_helper import upsert_agent_token_secret

    agent = Agent(name="Renamed One", agent_runtime="cli-bridge")
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)
    assert agent.slug == "renamed-one"
    # New writer keys on the slug → 'mc_token_renamed-one' (dash), not the name.
    await upsert_agent_token_secret(async_session, agent, "tok-orig")
    assert await _vault_token(async_session, "renamed-one") is not None
    assert await _vault_token(async_session, "renamed one") is None  # never space-form

    # Rename WITHOUT rotating the token (plain metadata edit) — slug is unchanged.
    agent.name = "Totally Different"
    async_session.add(agent)
    await async_session.commit()

    delete = await auth_client.delete(f"/api/v1/agents/{agent.id}")
    assert delete.status_code == 204, delete.text

    async_session.expire_all()
    assert await _vault_token(async_session, "renamed-one") is None


@pytest.mark.asyncio
async def test_writer_keys_token_on_slug_not_name(async_session):
    """Core of the slug migration: a multi-word agent's token lands under the
    dash-form slug key, never the legacy space-form name key."""
    from app.models.agent import Agent
    from app.services.secrets_helper import upsert_agent_token_secret

    agent = Agent(name="Multi Word", agent_runtime="cli-bridge")
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)
    assert agent.slug == "multi-word"

    await upsert_agent_token_secret(async_session, agent, "tok-slug")

    assert await _vault_token(async_session, "multi-word") == "tok-slug"
    assert await _vault_token(async_session, "multi word") is None


@pytest.mark.asyncio
async def test_bootstrap_reads_multiword_token_via_slug(auth_client, async_session):
    """End-to-end reader: the bootstrap endpoint resolves a multi-word agent's
    token via the slug key that the writer produced (writer↔reader symmetry).

    Agent inserted directly + token written via the writer, so no POST-create
    background provisioning task (which opens a real Postgres session) runs."""
    from app.models.agent import Agent
    from app.services.secrets_helper import upsert_agent_token_secret

    agent = Agent(name="Multi Boot", agent_runtime="cli-bridge")
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)
    await upsert_agent_token_secret(async_session, agent, "tok-boot")

    # Stored under the slug key, not the space-form name key.
    assert await _vault_token(async_session, "multi-boot") == "tok-boot"
    assert await _vault_token(async_session, "multi boot") is None

    boot = await auth_client.get("/api/v1/internal/bootstrap?agent_name=Multi Boot")
    assert boot.status_code == 200, boot.text
    assert boot.json()["MC_AGENT_TOKEN"] == "tok-boot"


@pytest.mark.asyncio
async def test_delete_agent_without_vault_secret_still_succeeds(auth_client, async_session):
    """The vault delete is best-effort: an agent whose secret was never
    written must still delete cleanly (no 500)."""
    from app.models.agent import Agent

    agent = Agent(name="NoSecret", agent_runtime="cli-bridge")
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)
    assert await _vault_token(async_session, "nosecret") is None

    delete = await auth_client.delete(f"/api/v1/agents/{agent.id}")
    assert delete.status_code == 204, delete.text
