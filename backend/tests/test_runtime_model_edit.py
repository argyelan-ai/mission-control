"""Editable model_identifier on cloud runtimes (PATCH /runtimes/db/{slug}).

A manual model_identifier edit must propagate like the watcher's drift
detection (ADR-054): flag bound cli-bridge agents for re-sync, invalidate the
resolver cache, and emit runtime.model_changed (source "manual_edit").
Non-model edits and no-op edits must NOT trigger propagation.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.models.agent import Agent
from app.models.runtime import Runtime


async def _mk_rt(session, *, slug="edit-rt", model="claude-opus-4-7"):
    rt = Runtime(
        slug=slug,
        display_name=slug.upper(),
        runtime_type="cloud",
        endpoint="https://api.anthropic.com",
        model_identifier=model,
        enabled=True,
    )
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


async def _mk_agent(session, rt, *, name="Boss", agent_runtime="cli-bridge"):
    agent = Agent(
        name=name, role="developer", agent_runtime=agent_runtime, runtime_id=rt.id
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


@pytest.mark.asyncio
async def test_model_edit_flags_agents_and_emits_event(async_session, auth_client):
    rt = await _mk_rt(async_session, model="claude-opus-4-7")
    agent = await _mk_agent(async_session, rt)

    with (
        patch("app.routers.runtimes.invalidate_cached_model", new=AsyncMock()) as inval,
        patch("app.routers.runtimes.activity.emit_event", new=AsyncMock()) as emit,
    ):
        resp = await auth_client.patch(
            f"/api/v1/runtimes/db/{rt.slug}",
            json={"model_identifier": "claude-opus-4-8"},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["model_identifier"] == "claude-opus-4-8"

    await async_session.refresh(rt)
    await async_session.refresh(agent)
    assert rt.model_identifier == "claude-opus-4-8"
    assert agent.pending_runtime_sync is True

    inval.assert_awaited_once_with(rt.slug)
    emit.assert_awaited_once()
    call = emit.await_args
    assert call.args[1] == "runtime.model_changed"
    assert call.kwargs["detail"]["old_model"] == "claude-opus-4-7"
    assert call.kwargs["detail"]["new_model"] == "claude-opus-4-8"
    assert call.kwargs["detail"]["source"] == "manual_edit"


@pytest.mark.asyncio
async def test_model_edit_noop_does_not_propagate(async_session, auth_client):
    rt = await _mk_rt(async_session, model="claude-opus-4-8")
    agent = await _mk_agent(async_session, rt)

    with (
        patch("app.routers.runtimes.invalidate_cached_model", new=AsyncMock()) as inval,
        patch("app.routers.runtimes.activity.emit_event", new=AsyncMock()) as emit,
    ):
        resp = await auth_client.patch(
            f"/api/v1/runtimes/db/{rt.slug}",
            json={"model_identifier": "claude-opus-4-8"},
        )

    assert resp.status_code == 200, resp.text
    await async_session.refresh(agent)
    assert agent.pending_runtime_sync is False
    inval.assert_not_awaited()
    emit.assert_not_awaited()


@pytest.mark.asyncio
async def test_other_field_edit_does_not_propagate(async_session, auth_client):
    rt = await _mk_rt(async_session, model="claude-opus-4-8")
    agent = await _mk_agent(async_session, rt)

    with (
        patch("app.routers.runtimes.invalidate_cached_model", new=AsyncMock()) as inval,
        patch("app.routers.runtimes.activity.emit_event", new=AsyncMock()) as emit,
    ):
        resp = await auth_client.patch(
            f"/api/v1/runtimes/db/{rt.slug}",
            json={"display_name": "Renamed Cloud"},
        )

    assert resp.status_code == 200, resp.text
    await async_session.refresh(rt)
    await async_session.refresh(agent)
    assert rt.display_name == "Renamed Cloud"
    assert rt.model_identifier == "claude-opus-4-8"
    assert agent.pending_runtime_sync is False
    inval.assert_not_awaited()
    emit.assert_not_awaited()
