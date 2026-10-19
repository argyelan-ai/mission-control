"""PR G — POST/PATCH /api/v1/runtimes/db must write runtime_hosts for a
duo (topology.nodes >= 2), not just the sparkrun recipe-switch path.

Before this fix, ``recipe_switcher.set_runtime_members`` was only called
from ``start_recipe_on_host`` — a runtime created or edited directly via the
DB-CRUD endpoints with ``topology.nodes >= 2`` never got a worker row in
``runtime_hosts``, so the worker host never showed as "in use" (Mark's rule:
head AND worker must show the model).

Only RFC 5737 placeholder IPs — public repo, no real addresses.
"""
import uuid

import pytest


def _ssh_host_body(slug: str = "gpu-box", **overrides) -> dict:
    body = {
        "slug": slug,
        "display_name": "GPU Box",
        "kind": "ssh",
        "ssh_host": "192.0.2.10",
        "ssh_user": "mcuser",
        "ssh_key_path": "/home/mcuser/.ssh/id_rsa",
        "notes": "Testbox",
        "ui_order": 1,
    }
    body.update(overrides)
    return body


def _runtime_body(slug: str = "duo-rt", **overrides) -> dict:
    body = {
        "slug": slug,
        "display_name": "Duo Runtime",
        "runtime_type": "vllm_docker",
        "endpoint": "http://192.0.2.30:8000/v1",
        "launch_command": "bash -lc 'true'",
    }
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_create_duo_runtime_writes_head_and_worker_members(auth_client):
    """POST with topology.nodes>=2 + worker_host_id → member_hosts has both."""
    head = (await auth_client.post("/api/v1/hosts", json=_ssh_host_body("alpha"))).json()
    worker = (
        await auth_client.post(
            "/api/v1/hosts", json=_ssh_host_body("beta", ssh_host="192.0.2.11")
        )
    ).json()

    resp = await auth_client.post(
        "/api/v1/runtimes/db",
        json=_runtime_body(
            host_id=head["id"],
            topology={"nodes": 2, "worker_host_id": worker["id"]},
        ),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["host"]["slug"] == "alpha"
    member_slugs = {m["slug"]: m["role"] for m in data["member_hosts"]}
    assert member_slugs == {"beta": "worker"}


@pytest.mark.asyncio
async def test_create_duo_runtime_without_worker_host_id_422(auth_client):
    """nodes>=2 but no worker_host_id → 422 with a clear message, not a silent gap."""
    head = (await auth_client.post("/api/v1/hosts", json=_ssh_host_body("alpha"))).json()

    resp = await auth_client.post(
        "/api/v1/runtimes/db",
        json=_runtime_body(host_id=head["id"], topology={"nodes": 2}),
    )
    assert resp.status_code == 422
    assert "worker_host_id" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_duo_runtime_unknown_worker_host_id_422(auth_client):
    head = (await auth_client.post("/api/v1/hosts", json=_ssh_host_body("alpha"))).json()

    resp = await auth_client.post(
        "/api/v1/runtimes/db",
        json=_runtime_body(
            host_id=head["id"],
            topology={"nodes": 2, "worker_host_id": str(uuid.uuid4())},
        ),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_worker_change_updates_members(auth_client):
    """PATCH topology.worker_host_id to a different host → membership follows."""
    head = (await auth_client.post("/api/v1/hosts", json=_ssh_host_body("alpha"))).json()
    beta = (
        await auth_client.post(
            "/api/v1/hosts", json=_ssh_host_body("beta", ssh_host="192.0.2.11")
        )
    ).json()
    gamma = (
        await auth_client.post(
            "/api/v1/hosts", json=_ssh_host_body("gamma", ssh_host="192.0.2.12")
        )
    ).json()

    await auth_client.post(
        "/api/v1/runtimes/db",
        json=_runtime_body(
            host_id=head["id"], topology={"nodes": 2, "worker_host_id": beta["id"]}
        ),
    )

    resp = await auth_client.patch(
        "/api/v1/runtimes/db/duo-rt",
        json={"topology": {"nodes": 2, "worker_host_id": gamma["id"]}},
    )
    assert resp.status_code == 200, resp.text
    member_slugs = {m["slug"]: m["role"] for m in resp.json()["member_hosts"]}
    assert member_slugs == {"gamma": "worker"}


@pytest.mark.asyncio
async def test_create_solo_runtime_nodes_one_unchanged(auth_client):
    """topology.nodes == 1 (or no topology) stays solo — no member_hosts row,
    no 422, exactly the behaviour before this fix."""
    resp = await auth_client.post("/api/v1/runtimes/db", json=_runtime_body(slug="solo-rt"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["member_hosts"] == []

    resp2 = await auth_client.post(
        "/api/v1/runtimes/db",
        json=_runtime_body(slug="solo-rt-2", topology={"nodes": 1}),
    )
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["member_hosts"] == []
