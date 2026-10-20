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
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def _no_engine_probe():
    """Changing a stored member set asks whether the engine runs (ADR-096
    §1.2). No network in tests: every runtime here is stopped unless a test
    patches ``probe_running`` itself."""
    with patch("app.services.recipe_switcher.probe_running", AsyncMock(return_value=False)):
        yield


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


# ── Critic round (19.10.2026) ───────────────────────────────────────────────
#
# runtime_hosts drives eviction (_ensure_exclusive_host), restart (the
# "safety net" in restart_runtime) and the "in use" display — so every way
# the DB-CRUD endpoints can leave it stale or wrong is a silent fault.

from unittest.mock import AsyncMock, patch  # noqa: E402

from sqlmodel import select  # noqa: E402
from sqlmodel.ext.asyncio.session import AsyncSession  # noqa: E402


async def _hosts(auth_client, *slugs: str) -> dict[str, dict]:
    out = {}
    for i, slug in enumerate(slugs):
        out[slug] = (
            await auth_client.post(
                "/api/v1/hosts", json=_ssh_host_body(slug, ssh_host=f"192.0.2.{20 + i}")
            )
        ).json()
    return out


async def _duo(auth_client, slug: str, head: dict, worker: dict, **extra):
    return await auth_client.post(
        "/api/v1/runtimes/db",
        json=_runtime_body(
            slug=slug,
            host_id=head["id"],
            topology={"nodes": 2, "worker_host_id": worker["id"]},
            **extra,
        ),
    )


async def _member_rows(slug: str) -> dict[str, str]:
    """host slug -> role, straight from runtime_hosts (not the response)."""
    from tests.conftest import test_engine
    from app.models.host import Host
    from app.models.runtime import Runtime
    from app.models.runtime_host import RuntimeHost

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        rt = (await s.exec(select(Runtime).where(Runtime.slug == slug))).first()
        if rt is None:
            return {}
        rows = (
            await s.exec(
                select(RuntimeHost, Host)
                .join(Host, RuntimeHost.host_id == Host.id)
                .where(RuntimeHost.runtime_id == rt.id)
            )
        ).all()
        return {h.slug: rh.role for rh, h in rows}


async def _runtime_row(slug: str):
    from tests.conftest import test_engine
    from app.models.runtime import Runtime

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return (await s.exec(select(Runtime).where(Runtime.slug == slug))).first()


# Finding 1 — PATCH that only moves the head ---------------------------------


@pytest.mark.asyncio
async def test_patch_host_id_only_moves_head_membership(auth_client):
    h = await _hosts(auth_client, "alpha", "beta", "gamma")
    assert (await _duo(auth_client, "duo-rt", h["alpha"], h["beta"])).status_code == 200

    resp = await auth_client.patch("/api/v1/runtimes/db/duo-rt", json={"host_id": h["gamma"]["id"]})
    assert resp.status_code == 200, resp.text
    assert {m["slug"]: m["role"] for m in resp.json()["member_hosts"]} == {"beta": "worker"}
    assert await _member_rows("duo-rt") == {"gamma": "head", "beta": "worker"}


@pytest.mark.asyncio
async def test_patch_host_id_onto_worker_box_422(auth_client):
    """Head moved onto the worker box → head == worker, refused."""
    h = await _hosts(auth_client, "alpha", "beta")
    assert (await _duo(auth_client, "duo-rt", h["alpha"], h["beta"])).status_code == 200

    resp = await auth_client.patch("/api/v1/runtimes/db/duo-rt", json={"host_id": h["beta"]["id"]})
    assert resp.status_code == 422, resp.text
    # nothing half-written: head and membership unchanged
    assert await _member_rows("duo-rt") == {"alpha": "head", "beta": "worker"}
    rt = await _runtime_row("duo-rt")
    assert str(rt.host_id) == h["alpha"]["id"]


# Finding 2 — demoting a duo to solo -------------------------------------------


@pytest.mark.asyncio
async def test_patch_nodes_one_clears_members(auth_client):
    h = await _hosts(auth_client, "alpha", "beta")
    assert (await _duo(auth_client, "duo-rt", h["alpha"], h["beta"])).status_code == 200

    resp = await auth_client.patch("/api/v1/runtimes/db/duo-rt", json={"topology": {"nodes": 1}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["member_hosts"] == []
    assert await _member_rows("duo-rt") == {}


# Finding 3 — same candidate rule as the recipe path ----------------------------


@pytest.mark.asyncio
async def test_cross_direction_duo_409(auth_client):
    h = await _hosts(auth_client, "alpha", "beta")
    assert (await _duo(auth_client, "duo-1", h["alpha"], h["beta"])).status_code == 200

    resp = await _duo(auth_client, "duo-2", h["beta"], h["alpha"])
    assert resp.status_code == 409, resp.text
    assert await _member_rows("duo-2") == {}


@pytest.mark.asyncio
async def test_same_pair_same_direction_allowed(auth_client):
    """Live today: two instances of the same pair (two recipes, one duo)."""
    h = await _hosts(auth_client, "alpha", "beta")
    assert (await _duo(auth_client, "duo-1", h["alpha"], h["beta"])).status_code == 200
    resp = await _duo(auth_client, "duo-2", h["alpha"], h["beta"])
    assert resp.status_code == 200, resp.text
    assert await _member_rows("duo-2") == {"alpha": "head", "beta": "worker"}


@pytest.mark.asyncio
async def test_worker_busy_with_foreign_running_exclusive_409(auth_client):
    """A running exclusive duo of ANOTHER head on the worker box makes it
    'not free' — duo_worker_candidates, exactly as POST
    /hosts/{id}/recipes/{slug}/start. A plain solo there does not: the duo
    start evicts it (Solo<->Duo, critic 19.10.)."""
    h = await _hosts(auth_client, "alpha", "beta", "gamma")
    solo = await auth_client.post(
        "/api/v1/runtimes/db",
        json=_runtime_body(slug="solo-beta", host_id=h["beta"]["id"], exclusive_memory=True),
    )
    assert solo.status_code == 200, solo.text
    other = await _duo(auth_client, "duo-gamma", h["gamma"], h["beta"], exclusive_memory=True)
    assert other.status_code == 200, other.text

    async def _solo_runs(rt, host=None):
        return rt.slug == "solo-beta"

    with patch("app.services.recipe_switcher.probe_running", _solo_runs):
        resp = await _duo(auth_client, "duo-rt", h["alpha"], h["beta"])
    assert resp.status_code == 200, resp.text

    async def _other_duo_runs(rt, host=None):
        return rt.slug == "duo-gamma"

    with patch("app.services.recipe_switcher.probe_running", _other_duo_runs):
        resp = await _duo(auth_client, "duo-rt-2", h["alpha"], h["beta"])
    assert resp.status_code == 409, resp.text
    assert "beta" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_worker_without_ssh_409(auth_client):
    h = await _hosts(auth_client, "alpha")
    no_ssh = (
        await auth_client.post(
            "/api/v1/hosts",
            json={"slug": "cloudy", "display_name": "Cloudy", "kind": "local"},
        )
    ).json()
    resp = await _duo(auth_client, "duo-rt", h["alpha"], no_ssh)
    assert resp.status_code == 409, resp.text


# Minor — strict nodes -----------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("nodes", ["zwei", None, -5, 0, True, 2.9, 3])
async def test_invalid_nodes_422(auth_client, nodes):
    h = await _hosts(auth_client, "alpha", "beta")
    resp = await auth_client.post(
        "/api/v1/runtimes/db",
        json=_runtime_body(
            host_id=h["alpha"]["id"],
            topology={"nodes": nodes, "worker_host_id": h["beta"]["id"]},
        ),
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_worker_host_id_on_solo_422(auth_client):
    h = await _hosts(auth_client, "alpha", "beta")
    resp = await auth_client.post(
        "/api/v1/runtimes/db",
        json=_runtime_body(
            host_id=h["alpha"]["id"],
            topology={"nodes": 1, "worker_host_id": h["beta"]["id"]},
        ),
    )
    assert resp.status_code == 422, resp.text


# Minor — legacy duo rows without worker_host_id ----------------------------------


@pytest.mark.asyncio
async def test_patch_legacy_duo_keeps_existing_worker_membership(auth_client):
    """A recipe-built duo ({nodes, recipe_slug}, worker only in runtime_hosts)
    stays editable: the recorded worker is reused."""
    h = await _hosts(auth_client, "alpha", "beta")
    assert (await _duo(auth_client, "duo-rt", h["alpha"], h["beta"])).status_code == 200

    resp = await auth_client.patch(
        "/api/v1/runtimes/db/duo-rt",
        json={"topology": {"nodes": 2, "recipe_slug": "x", "tp_total": 2}},
    )
    assert resp.status_code == 200, resp.text
    assert await _member_rows("duo-rt") == {"alpha": "head", "beta": "worker"}


@pytest.mark.asyncio
async def test_patch_duo_without_any_worker_names_the_way_out(auth_client):
    h = await _hosts(auth_client, "alpha")
    solo = await auth_client.post(
        "/api/v1/runtimes/db", json=_runtime_body(slug="rt", host_id=h["alpha"]["id"])
    )
    assert solo.status_code == 200
    resp = await auth_client.patch(
        "/api/v1/runtimes/db/rt", json={"topology": {"nodes": 2, "recipe_slug": "x"}}
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "worker_host_id" in detail and "/recipes/" in detail


# Minor — one transaction ---------------------------------------------------------


@pytest.mark.asyncio
async def test_create_duo_is_atomic_when_member_write_fails(auth_client):
    h = await _hosts(auth_client, "alpha", "beta")
    boom = AsyncMock(side_effect=RuntimeError("member write failed"))
    with patch("app.services.runtime_membership._insert_rows", boom):
        with pytest.raises(RuntimeError):
            await _duo(auth_client, "duo-rt", h["alpha"], h["beta"])
    assert await _runtime_row("duo-rt") is None
