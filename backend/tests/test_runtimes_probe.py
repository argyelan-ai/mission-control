"""Phase 16 Plan 03 — Tests for POST /api/v1/runtimes/{id}/probe-model.

D-18/D-19/D-21:
  - Endpoint re-uses Phase-15 probe_runtime_model + ensure_runtime_model_identifier.
  - Response schema: {slug, old_model_identifier, new_model_identifier, changed}.
  - Idempotent: a second call with the same probe result returns changed=false.
  - Multi-model responses are reduced to data[0].id.
  - Non-probeable runtime_types (e.g. "cloud") → 422.
  - Slug-or-UUID lookup, 404 for unknown IDs.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.models.runtime import Runtime


async def _mk_rt(
    session,
    *,
    slug: str = "probe-rt",
    runtime_type: str = "lmstudio",
    endpoint: str = "http://localhost:1234/v1",
    model_identifier: str | None = None,
    enabled: bool = True,
) -> Runtime:
    rt = Runtime(
        slug=slug,
        display_name=slug.upper(),
        runtime_type=runtime_type,
        endpoint=endpoint,
        model_identifier=model_identifier,
        enabled=enabled,
    )
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


# ── Test 1: Probe success → DB updated, changed=true ─────────────────────


@pytest.mark.asyncio
async def test_probe_model_changes_identifier(async_session, auth_client):
    rt = await _mk_rt(
        async_session,
        slug="lms-probe",
        runtime_type="lmstudio",
        model_identifier="qwen-3-coder",
    )

    async def fake_probe(_runtime):
        return "qwen-3-coder-next"

    with patch(
        "app.routers.runtimes.probe_runtime_model",
        side_effect=fake_probe,
    ):
        resp = await auth_client.post(f"/api/v1/runtimes/{rt.slug}/probe-model")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["slug"] == "lms-probe"
    assert body["old_model_identifier"] == "qwen-3-coder"
    assert body["new_model_identifier"] == "qwen-3-coder-next"
    assert body["changed"] is True

    # DB row updated
    await async_session.refresh(rt)
    assert rt.model_identifier == "qwen-3-coder-next"


# ── Test 2: Probe identical → changed=false, DB unchanged ────────────────


@pytest.mark.asyncio
async def test_probe_model_unchanged(async_session, auth_client):
    rt = await _mk_rt(
        async_session,
        slug="vllm-stable",
        runtime_type="vllm_docker",
        model_identifier="glm-5.1",
    )

    with patch(
        "app.routers.runtimes.probe_runtime_model",
        side_effect=lambda _r: "glm-5.1",
    ):
        resp = await auth_client.post(f"/api/v1/runtimes/{rt.slug}/probe-model")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["old_model_identifier"] == "glm-5.1"
    assert body["new_model_identifier"] == "glm-5.1"
    assert body["changed"] is False

    await async_session.refresh(rt)
    assert rt.model_identifier == "glm-5.1"


# ── Test 3: Non-probeable runtime_type → 422 ────────────────────────────


@pytest.mark.asyncio
async def test_probe_model_rejects_non_probeable_type(async_session, auth_client):
    rt = await _mk_rt(
        async_session,
        slug="anthropic-cloud",
        runtime_type="cloud",
        endpoint="https://api.anthropic.com",
        model_identifier="claude-sonnet-4",
    )

    resp = await auth_client.post(f"/api/v1/runtimes/{rt.slug}/probe-model")
    assert resp.status_code == 422
    detail = resp.json().get("detail", "")
    assert "probe" in detail.lower() or "unterstützt" in detail.lower() or "unterstuetzt" in detail.lower() or "support" in detail.lower()


# ── Test 4: Unknown slug → 404 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_probe_model_unknown_slug_returns_404(async_session, auth_client):
    resp = await auth_client.post("/api/v1/runtimes/no-such-runtime/probe-model")
    assert resp.status_code == 404


# ── Test 5: UUID lookup works ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_probe_model_works_with_uuid(async_session, auth_client):
    rt = await _mk_rt(
        async_session,
        slug="lms-uuid",
        runtime_type="lmstudio",
        model_identifier="qwen-old",
    )

    with patch(
        "app.routers.runtimes.probe_runtime_model",
        side_effect=lambda _r: "qwen-new",
    ):
        resp = await auth_client.post(f"/api/v1/runtimes/{rt.id}/probe-model")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["slug"] == "lms-uuid"
    assert body["new_model_identifier"] == "qwen-new"
    assert body["changed"] is True


# ── Test 6: Idempotent — second call → changed=false ────────────────────


@pytest.mark.asyncio
async def test_probe_model_idempotent(async_session, auth_client):
    rt = await _mk_rt(
        async_session,
        slug="lms-idem",
        runtime_type="lmstudio",
        model_identifier=None,
    )

    with patch(
        "app.routers.runtimes.probe_runtime_model",
        side_effect=lambda _r: "fresh-model",
    ):
        first = await auth_client.post(f"/api/v1/runtimes/{rt.slug}/probe-model")
        second = await auth_client.post(f"/api/v1/runtimes/{rt.slug}/probe-model")

    assert first.status_code == 200
    assert first.json()["changed"] is True
    assert first.json()["new_model_identifier"] == "fresh-model"

    assert second.status_code == 200
    body2 = second.json()
    assert body2["old_model_identifier"] == "fresh-model"
    assert body2["new_model_identifier"] == "fresh-model"
    assert body2["changed"] is False
