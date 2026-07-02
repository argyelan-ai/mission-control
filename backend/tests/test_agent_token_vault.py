"""MC_AGENT_TOKEN → Vault-Write bei jeder Token-Generierung (Fresh-Install-Fix).

Fund 2026-07-02 (critical): der Vault-Key mc_token_{slug} wurde nur GELESEN
(routers/internal.py::agent_bootstrap), aber ausser vom einmaligen Migrations-
Skript nie geschrieben. Folge: frisch erstellte Agents bekamen via
/internal/bootstrap kein MC_TOKEN → poll.sh crash-loopte, One-Click-Deploy
war auf Fresh-Installs tot.

Deckt die drei User-erreichbaren Pfade ab: create_agent, Template-Instantiate
(_do_instantiate) und reset-token — inkl. End-to-End über den Bootstrap-
Endpoint selbst.
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

    # Vault-Key-Schema muss dem Bootstrap-Lookup entsprechen (agent.name.lower())
    assert await _vault_token(async_session, "vaultbot") == raw_token


@pytest.mark.asyncio
async def test_bootstrap_delivers_token_after_create(auth_client, async_session):
    """End-to-End: der Container-Bootstrap-Pfad findet den frischen Token."""
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
    # Vault liefert den NEUEN Token — sonst startet der Container mit dem alten
    assert await _vault_token(async_session, "rotatebot") == new_token


@pytest.mark.asyncio
async def test_template_instantiate_writes_token_to_vault(async_session):
    """_do_instantiate (Template-Pfad, auch von Approvals/agent-scoped genutzt)
    schreibt den Token in den Vault. Direkt getestet statt über den HTTP-
    Endpoint, damit kein Provisioning-BackgroundTask im Test-Env anläuft."""
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
