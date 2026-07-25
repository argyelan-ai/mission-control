"""Compat-matrix route + writable api_key_secret_id (ADR-056)."""
import pytest

from app.models.runtime import Runtime
from app.models.secret import Secret


async def _mk_rt(session, *, slug, runtime_type, model="row-model"):
    rt = Runtime(
        slug=slug, display_name=slug, runtime_type=runtime_type,
        endpoint="http://spark:8000/v1", model_identifier=model, enabled=True,
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
