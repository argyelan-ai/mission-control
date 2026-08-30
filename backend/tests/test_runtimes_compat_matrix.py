"""Compat-matrix route + writable api_key_secret_id (ADR-056)."""
import pytest

from app.models.host import Host
from app.models.runtime import Runtime
from app.models.secret import Secret


async def _mk_rt(session, *, slug, runtime_type, model="row-model", host_id=None):
    rt = Runtime(
        slug=slug, display_name=slug, runtime_type=runtime_type,
        endpoint="http://spark:8000/v1", model_identifier=model, enabled=True,
        host_id=host_id,
    )
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


@pytest.mark.asyncio
async def test_compat_matrix_route(async_session, auth_client):
    await _mk_rt(async_session, slug="cloud-a", runtime_type="openai_compatible")
    await _mk_rt(async_session, slug="anthropic-a", runtime_type="anthropic_oauth")

    resp = await auth_client.get("/api/v1/runtimes/compat-matrix")
    assert resp.status_code == 200
    data = resp.json()
    assert {h["key"] for h in data["harnesses"]} == {"claude", "openclaude", "omp", "kimi"}
    row = next(r for r in data["runtimes"] if r["slug"] == "cloud-a")
    assert row["protocol"] == "openai"
    assert set(row["compatible_harnesses"]) == {"openclaude", "omp"}
    assert "claude" in row["reasons"]
    assert "kimi" in row["reasons"]  # kimi ist protocol-fixed — openai-Runtime inkompatibel

    anthropic_row = next(r for r in data["runtimes"] if r["slug"] == "anthropic-a")
    assert anthropic_row["protocol"] == "anthropic"
    assert anthropic_row["compatible_harnesses"] == ["claude"]
    assert set(anthropic_row["reasons"]) == {"openclaude", "omp", "kimi"}


@pytest.mark.asyncio
async def test_compat_matrix_locality(async_session, auth_client):
    """Verbund-UI Phase 0 (30.08.2026): host-inplace agents can only ever run
    something physically on their own box — the picker needs to know which
    candidates even qualify as "local"."""
    host = Host(slug="worker-a", display_name="Worker A", kind="ssh", ssh_host="192.0.2.40")
    async_session.add(host)
    await async_session.commit()
    await async_session.refresh(host)

    await _mk_rt(async_session, slug="bound-vllm", runtime_type="vllm_docker", host_id=host.id)
    await _mk_rt(async_session, slug="unbound-vllm", runtime_type="vllm_docker")  # legacy fallback, no host_id yet
    await _mk_rt(async_session, slug="generic-cloud", runtime_type="cloud")
    await _mk_rt(async_session, slug="grok-cloud", runtime_type="grok")
    await _mk_rt(async_session, slug="anthropic-cloud", runtime_type="anthropic_oauth")
    await _mk_rt(async_session, slug="hermes-local", runtime_type="hermes")  # curated LOCAL type, not cloud
    # A cloud-typed row that IS bound to a real fleet host — the host binding
    # must win over the type-based default (discriminates the host-priority
    # branch specifically: every other row above would resolve the same way
    # even if that branch were deleted, since only "cloud"-classified types
    # are affected by it here).
    await _mk_rt(async_session, slug="cloud-type-but-host-bound", runtime_type="cloud", host_id=host.id)

    resp = await auth_client.get("/api/v1/runtimes/compat-matrix")
    assert resp.status_code == 200
    rows = {r["slug"]: r["locality"] for r in resp.json()["runtimes"]}
    assert rows["bound-vllm"] == "local"
    assert rows["unbound-vllm"] == "local"
    assert rows["generic-cloud"] == "cloud"
    assert rows["grok-cloud"] == "cloud"
    assert rows["anthropic-cloud"] == "cloud"
    assert rows["hermes-local"] == "local"
    assert rows["cloud-type-but-host-bound"] == "local"


@pytest.mark.asyncio
async def test_runtime_patch_sets_and_clears_api_key_secret(async_session, auth_client):
    rt = await _mk_rt(async_session, slug="secret-rt", runtime_type="openai_compatible")
    secret = Secret(key="test_runtime_key", encrypted_value="ciphertext")
    async_session.add(secret)
    await async_session.commit()
    await async_session.refresh(secret)

    resp = await auth_client.patch(
        f"/api/v1/runtimes/db/{rt.slug}", json={"api_key_secret_id": str(secret.id)}
    )
    assert resp.status_code == 200
    assert resp.json()["api_key_secret_id"] == str(secret.id)

    resp = await auth_client.patch(
        f"/api/v1/runtimes/db/{rt.slug}", json={"api_key_secret_id": None}
    )
    assert resp.status_code == 200
    assert resp.json()["api_key_secret_id"] is None
