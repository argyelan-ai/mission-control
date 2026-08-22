"""Scope-Tests für Gruppen-Threads (Gruppenchat V1, PR A).

Ein Gruppen-Thread (kind="group") gehört zu genau einer AgentGroup
(agent_groups.thread_id, unique); Teilnahme steht in group_members.

`thread_scope.message_threads_for_agent` muss Gruppen-Threads für
Mitglieder liefern — damit funktionieren Zustellung (/me/poll, /me/inbox)
UND Antwort-Recht (POST /threads/{id}/messages) aus EINER Regel, beide
Richtungen (der Docstring von thread_scope warnt exakt vor diesem
Drift-Bug). Für Nicht-Mitglieder bleibt der Thread unsichtbar.
"""
import uuid

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.group import AgentGroup, GroupMember
from app.models.thread import Thread
from app.services.thread_scope import (
    message_threads_for_agent,
    thread_agent_may_write_to,
)


async def _make_agent(session: AsyncSession, name: str) -> Agent:
    agent = Agent(
        name=name,
        slug=name.lower(),
        agent_runtime="cli-bridge",
        comm_v2=True,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


async def _make_group(
    session: AsyncSession, members: list[Agent], *, lead: Agent | None = None
) -> tuple[AgentGroup, Thread]:
    """Gruppe + zugehöriger Thread(kind="group") mit den Mitgliedern."""
    thread = Thread(kind="group", title="Testgruppe")
    session.add(thread)
    await session.commit()
    await session.refresh(thread)

    group = AgentGroup(
        thread_id=thread.id,
        name="Testgruppe",
        goal="Eine Frage klären",
        lead_agent_id=(lead or members[0]).id,
    )
    session.add(group)
    await session.commit()
    await session.refresh(group)

    for agent in members:
        role = "lead" if agent.id == (lead or members[0]).id else "member"
        session.add(GroupMember(group_id=group.id, agent_id=agent.id, role=role))
    await session.commit()
    return group, thread


@pytest.mark.asyncio
async def test_member_sees_group_thread_in_scope(async_session: AsyncSession):
    """Mitglied: der Gruppen-Thread erscheint in message_threads_for_agent
    (Zustell-Scope) — mit task=None, denn eine Gruppe hat keinen Task."""
    a, b = await _make_agent(async_session, "Alpha"), await _make_agent(async_session, "Beta")
    group, thread = await _make_group(async_session, [a, b])

    pairs = await message_threads_for_agent(a, async_session)
    thread_ids = [t.id for t, _task in pairs]
    assert thread.id in thread_ids
    task_for_group = next(task for t, task in pairs if t.id == thread.id)
    assert task_for_group is None


@pytest.mark.asyncio
async def test_non_member_does_not_see_group_thread(async_session: AsyncSession):
    a, b = await _make_agent(async_session, "Alpha"), await _make_agent(async_session, "Beta")
    outsider = await _make_agent(async_session, "Gamma")
    _group, thread = await _make_group(async_session, [a, b])

    pairs = await message_threads_for_agent(outsider, async_session)
    assert thread.id not in [t.id for t, _task in pairs]


@pytest.mark.asyncio
async def test_member_may_write_non_member_may_not(async_session: AsyncSession):
    """Antwort-Recht = dieselbe Regel: Mitglied darf, Nicht-Mitglied kriegt None
    (Router antwortet 404 — kein Ausprobieren fremder Threads)."""
    a, b = await _make_agent(async_session, "Alpha"), await _make_agent(async_session, "Beta")
    outsider = await _make_agent(async_session, "Gamma")
    _group, thread = await _make_group(async_session, [a, b])

    assert (await thread_agent_may_write_to(async_session, a, thread.id)) is not None
    assert (await thread_agent_may_write_to(async_session, outsider, thread.id)) is None


@pytest.mark.asyncio
async def test_removed_member_loses_scope_in_both_directions(async_session: AsyncSession):
    """Sabotage-Probe aus dem Plan: Mitglied entfernen → weder Zustellung noch
    Antwort-Recht. Beide Pfade laufen über dieselbe Funktion — der Test
    dokumentiert genau das."""
    a, b = await _make_agent(async_session, "Alpha"), await _make_agent(async_session, "Beta")
    group, thread = await _make_group(async_session, [a, b])

    member_row = await async_session.get(GroupMember, (group.id, b.id))
    assert member_row is not None
    await async_session.delete(member_row)
    await async_session.commit()

    pairs = await message_threads_for_agent(b, async_session)
    assert thread.id not in [t.id for t, _task in pairs]
    assert (await thread_agent_may_write_to(async_session, b, thread.id)) is None


@pytest.mark.asyncio
async def test_closed_group_thread_out_of_scope(async_session: AsyncSession):
    """Geschlossene Gruppe (Thread.closed_at gesetzt) wird nicht mehr zugestellt."""
    import datetime as dt

    a, b = await _make_agent(async_session, "Alpha"), await _make_agent(async_session, "Beta")
    _group, thread = await _make_group(async_session, [a, b])

    thread.closed_at = dt.datetime.now(tz=dt.timezone.utc)
    async_session.add(thread)
    await async_session.commit()

    pairs = await message_threads_for_agent(a, async_session)
    assert thread.id not in [t.id for t, _task in pairs]
