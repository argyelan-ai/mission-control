"""Phase 16 — Tests for DB-backed runtime registry.

D-01/D-03: GET /runtimes reads from the DB (not JSON).
"""
from unittest.mock import patch

import pytest

from app.models.host import Host
from app.models.runtime import Runtime
from app.models.runtime_host import RuntimeHost


async def _stub_state(*_args, **_kwargs):
    """Replacement for runtime_manager.get_runtime_state — bypasses SSH."""
    return {"state": "ready", "http_reachable": True, "container_status": None}


# ── list_db_runtimes Helper ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_db_runtimes_empty(async_session):
    """Empty DB → empty list."""
    from app.services.runtime_manager import list_db_runtimes

    result = await list_db_runtimes(async_session)
    assert result == []


@pytest.mark.asyncio
async def test_list_db_runtimes_sorted_by_ui_order(async_session):
    """Two runtimes → both returned, sorted by ui_order."""
    from app.services.runtime_manager import list_db_runtimes

    rt2 = Runtime(
        slug="b-second",
        display_name="Second",
        runtime_type="lmstudio",
        endpoint="http://localhost:1235/v1",
        ui_order=2,
        enabled=True,
    )
    rt1 = Runtime(
        slug="a-first",
        display_name="First",
        runtime_type="lmstudio",
        endpoint="http://localhost:1234/v1",
        ui_order=1,
        enabled=True,
    )
    # Insert in reverse order to verify sort
    async_session.add(rt2)
    async_session.add(rt1)
    await async_session.commit()

    result = await list_db_runtimes(async_session)
    assert len(result) == 2
    assert result[0].slug == "a-first"
    assert result[1].slug == "b-second"


@pytest.mark.asyncio
async def test_list_db_runtimes_includes_disabled(async_session):
    """Disabled runtimes are returned too (filtering happens in the router)."""
    from app.services.runtime_manager import list_db_runtimes

    rt_enabled = Runtime(
        slug="enabled-rt",
        display_name="Enabled",
        runtime_type="lmstudio",
        endpoint="http://localhost:1234/v1",
        ui_order=1,
        enabled=True,
    )
    rt_disabled = Runtime(
        slug="disabled-rt",
        display_name="Disabled",
        runtime_type="lmstudio",
        endpoint="http://localhost:1235/v1",
        ui_order=2,
        enabled=False,
    )
    async_session.add(rt_enabled)
    async_session.add(rt_disabled)
    await async_session.commit()

    result = await list_db_runtimes(async_session)
    slugs = {rt.slug for rt in result}
    assert slugs == {"enabled-rt", "disabled-rt"}


# ── GET /api/v1/runtimes — DB-backed ──────────────────────────────────────


@pytest.mark.asyncio
async def test_get_runtimes_returns_enabled_from_db(async_session, auth_client):
    """GET /api/v1/runtimes returns only enabled runtimes from the DB."""
    rt = Runtime(
        slug="db-only-rt",
        display_name="DB Only Runtime",
        runtime_type="openai_compatible",
        endpoint="http://example.com/v1",
        ui_order=5,
        enabled=True,
    )
    rt_disabled = Runtime(
        slug="db-disabled-rt",
        display_name="Disabled",
        runtime_type="openai_compatible",
        endpoint="http://example.com/v1",
        ui_order=6,
        enabled=False,
    )
    async_session.add(rt)
    async_session.add(rt_disabled)
    await async_session.commit()

    with patch(
        "app.services.runtime_manager.get_runtime_state",
        side_effect=_stub_state,
    ):
        resp = await auth_client.get("/api/v1/runtimes")

    assert resp.status_code == 200, resp.text
    data = resp.json()
    slugs = {r["slug"] for r in data["runtimes"]}
    assert "db-only-rt" in slugs
    assert "db-disabled-rt" not in slugs


@pytest.mark.asyncio
async def test_get_runtimes_uses_db_not_json(async_session, auth_client):
    """A runtime that exists ONLY in the DB (slug not in JSON) shows up
    in GET /runtimes — proof that the DB is the source, not runtimes.json."""
    rt = Runtime(
        slug="phase-16-fresh-runtime",
        display_name="Phase 16 Fresh",
        runtime_type="openai_compatible",
        endpoint="http://localhost:9999/v1",
        ui_order=99,
        enabled=True,
    )
    async_session.add(rt)
    await async_session.commit()

    with patch(
        "app.services.runtime_manager.get_runtime_state",
        side_effect=_stub_state,
    ):
        resp = await auth_client.get("/api/v1/runtimes")

    assert resp.status_code == 200
    slugs = {r["slug"] for r in resp.json()["runtimes"]}
    assert "phase-16-fresh-runtime" in slugs


@pytest.mark.asyncio
async def test_get_runtime_by_slug_from_db(async_session, auth_client):
    """GET /api/v1/runtimes/{slug} returns 200 for an existing slug."""
    rt = Runtime(
        slug="single-rt",
        display_name="Single",
        runtime_type="openai_compatible",
        endpoint="http://localhost:9000/v1",
        ui_order=1,
        enabled=True,
    )
    async_session.add(rt)
    await async_session.commit()

    with patch(
        "app.services.runtime_manager.get_runtime_state",
        side_effect=_stub_state,
    ):
        resp = await auth_client.get("/api/v1/runtimes/single-rt")

    assert resp.status_code == 200
    data = resp.json()
    assert data["slug"] == "single-rt"
    assert data["display_name"] == "Single"


@pytest.mark.asyncio
async def test_get_runtime_unknown_returns_404(async_session, auth_client):
    """GET /api/v1/runtimes/{unknown} returns 404."""
    with patch(
        "app.services.runtime_manager.get_runtime_state",
        side_effect=_stub_state,
    ):
        resp = await auth_client.get("/api/v1/runtimes/no-such-runtime")

    assert resp.status_code == 404

@pytest.mark.asyncio
async def test_get_runtimes_groups_by_provider(async_session, auth_client):
    """Same-provider runtimes come back together, whatever ui_order says.

    Catalogue-bound rows are created with the default ui_order 999, so sorting
    by that alone scattered the list: the two Anthropic models sat apart and the
    Ollama ones did too (operator report, 2026-07-31). Provider membership is
    derived from the endpoint, so a newly bound model files itself next to its
    siblings without anyone maintaining ui_order.
    """
    rows = [
        ("anthropic-opus", "https://api.anthropic.com/v1/messages", 7),
        ("ollama-glm-51", "https://ollama.com/v1", 6),
        ("anthropic-opus-5", "https://api.anthropic.com/v1/messages", 999),
        ("ollama-glm-52", "https://ollama.com/v1", 999),
        ("local-vllm", "http://192.0.2.10:8000/v1", 3),
    ]
    for slug, endpoint, order in rows:
        async_session.add(Runtime(
            slug=slug, display_name=slug, runtime_type="openai_compatible",
            endpoint=endpoint, ui_order=order, enabled=True,
        ))
    await async_session.commit()

    with patch(
        "app.services.runtime_manager.get_runtime_state",
        side_effect=_stub_state,
    ):
        resp = await auth_client.get("/api/v1/runtimes")

    assert resp.status_code == 200, resp.text
    slugs = [r["slug"] for r in resp.json()["runtimes"]]

    def block(prefix):
        idx = [i for i, s in enumerate(slugs) if s.startswith(prefix)]
        return idx

    anthropic, ollama = block("anthropic-"), block("ollama-")
    assert len(anthropic) == 2 and len(ollama) == 2
    # Contiguous: no foreign slug wedged between siblings.
    assert anthropic == list(range(anthropic[0], anthropic[0] + 2)), slugs
    assert ollama == list(range(ollama[0], ollama[0] + 2)), slugs
    # The unknown local endpoint keeps its curated order and sorts after the
    # recognised cloud providers — where it sat before.
    assert slugs.index("local-vllm") > max(anthropic + ollama), slugs


@pytest.mark.asyncio
async def test_get_runtimes_reports_locality(async_session, auth_client):
    """Verbund-UI Phase 0 (30.08.2026): GET /runtimes backs the agent detail
    page's runtime picker directly — it needs `locality` per row so a
    host-inplace agent's picker can exclude candidates it could never
    physically reach."""
    host = Host(slug="worker-b", display_name="Worker B", kind="ssh", ssh_host="192.0.2.41")
    async_session.add(host)
    await async_session.commit()
    await async_session.refresh(host)

    async_session.add(Runtime(
        slug="host-bound-local", display_name="Host-bound", runtime_type="vllm_docker",
        endpoint="http://192.0.2.41:8000/v1", ui_order=1, enabled=True, host_id=host.id,
    ))
    async_session.add(Runtime(
        slug="cloud-row", display_name="Cloud", runtime_type="cloud",
        endpoint="https://api.example.com/v1", ui_order=2, enabled=True,
    ))
    await async_session.commit()

    with patch(
        "app.services.runtime_manager.get_runtime_state",
        side_effect=_stub_state,
    ):
        resp = await auth_client.get("/api/v1/runtimes")

    assert resp.status_code == 200, resp.text
    rows = {r["slug"]: r["locality"] for r in resp.json()["runtimes"]}
    assert rows["host-bound-local"] == "local"
    assert rows["cloud-row"] == "cloud"


@pytest.mark.asyncio
async def test_get_runtimes_reports_member_hosts_and_topology(async_session, auth_client):
    """Verbund-UI Phase 1b (30.08.2026): a multi-node runtime's workers show
    up in `member_hosts` (the head stays in `host`, never duplicated here),
    and `topology` passes through unchanged. A solo runtime (no rows in
    runtime_hosts) gets an empty list, not a missing key."""
    head_host = Host(slug="alpha", display_name="Alpha", kind="ssh", ssh_host="192.0.2.50")
    worker_host = Host(slug="beta", display_name="Beta", kind="agent")
    async_session.add(head_host)
    async_session.add(worker_host)
    await async_session.commit()
    await async_session.refresh(head_host)
    await async_session.refresh(worker_host)

    verbund_rt = Runtime(
        slug="glm-verbund", display_name="GLM Verbund", runtime_type="vllm_docker",
        endpoint="http://192.0.2.50:8000/v1", ui_order=1, enabled=True, host_id=head_host.id,
        topology={"nodes": 2, "tp_total": 2, "roles": ["head", "worker"]},
    )
    solo_rt = Runtime(
        slug="solo-rt", display_name="Solo", runtime_type="lmstudio",
        endpoint="http://192.0.2.51:1234/v1", ui_order=2, enabled=True,
    )
    async_session.add(verbund_rt)
    async_session.add(solo_rt)
    await async_session.commit()
    await async_session.refresh(verbund_rt)

    async_session.add(RuntimeHost(
        runtime_id=verbund_rt.id, host_id=worker_host.id, role="worker", node_rank=1,
    ))
    await async_session.commit()

    with patch(
        "app.services.runtime_manager.get_runtime_state",
        side_effect=_stub_state,
    ):
        resp = await auth_client.get("/api/v1/runtimes")

    assert resp.status_code == 200, resp.text
    rows = {r["slug"]: r for r in resp.json()["runtimes"]}

    verbund_row = rows["glm-verbund"]
    assert verbund_row["topology"] == {"nodes": 2, "tp_total": 2, "roles": ["head", "worker"]}
    assert len(verbund_row["member_hosts"]) == 1
    member = verbund_row["member_hosts"][0]
    assert member["slug"] == "beta"
    assert member["display_name"] == "Beta"
    assert member["role"] == "worker"
    assert member["node_rank"] == 1
    # The head (alpha) is NOT duplicated into member_hosts — it's already
    # in "host".
    assert all(m["slug"] != "alpha" for m in verbund_row["member_hosts"])
    assert verbund_row["host"]["slug"] == "alpha"

    solo_row = rows["solo-rt"]
    assert solo_row["member_hosts"] == []
    assert solo_row["topology"] is None
