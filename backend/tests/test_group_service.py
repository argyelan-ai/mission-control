"""group_service — Anlegen/Mitglieder/Marks Nachrichten (Gruppenchat V1, PR A).

Der Service ist die einzige Schreibstelle für Gruppen: er erzwingt das
Pflicht-Ziel, comm_v2-fähige Mitglieder, legt Thread + Gruppe + Mitglieder +
Dokument-Skelett (references/groups/<slug>/result.md, ReferenceFile-Row beim
Lead) an und löst bei Marks Nachrichten die Mentions gegen die Mitglieder auf
(fold-tolerant; "@alle" → alle; keine Mention → Lead — sonst würde die
Nachricht niemanden erreichen, siehe Mention-Filter in der Zustellung).
"""
import uuid
from pathlib import Path

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.group import AgentGroup, GroupMember
from app.models.reference_file import ReferenceFile
from app.models.thread import Thread
from app.services import group_service
from app.services.group_service import (
    GroupMemberNotCapable,
    GroupValidationError,
    add_member,
    create_group,
    post_user_message,
    remove_member,
)


@pytest.fixture(autouse=True)
def _references_in_tmp(tmp_path: Path, monkeypatch):
    """Kein Test schreibt je ins echte ~/.mc/references (Vault-Vorfall-Klasse)."""
    monkeypatch.setattr(
        "app.services.reference_ingest.references_root", lambda: str(tmp_path)
    )
    return tmp_path


async def _make_agent(
    session: AsyncSession, name: str, *, comm_v2: bool = True, is_board_lead: bool = False
) -> Agent:
    agent = Agent(
        name=name,
        slug=name.lower(),
        agent_runtime="cli-bridge",
        comm_v2=comm_v2,
        is_board_lead=is_board_lead,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


@pytest.mark.asyncio
async def test_create_group_creates_thread_members_and_doc(
    async_session: AsyncSession, _references_in_tmp: Path
):
    a = await _make_agent(async_session, "Alpha")
    b = await _make_agent(async_session, "Beta")

    group = await create_group(
        async_session,
        name="Spark-Vergleich",
        goal="DFlash2 vs vLLM entscheiden",
        member_ids=[a.id, b.id],
        lead_agent_id=a.id,
    )

    thread = await async_session.get(Thread, group.thread_id)
    assert thread is not None and thread.kind == "group"
    assert group.goal == "DFlash2 vs vLLM entscheiden"
    assert group.status == "idle"
    assert group.max_rounds == 3

    members = (
        await async_session.exec(
            select(GroupMember).where(GroupMember.group_id == group.id)
        )
    ).all()
    roles = {m.agent_id: m.role for m in members}
    assert roles == {a.id: "lead", b.id: "member"}

    # Dokument-Skelett + ReferenceFile-Row (Plan §4.4)
    assert group.result_doc_rel_path is not None
    doc = _references_in_tmp / group.result_doc_rel_path
    assert doc.is_file()
    assert "DFlash2 vs vLLM entscheiden" in doc.read_text()
    ref = (
        await async_session.exec(
            select(ReferenceFile).where(ReferenceFile.rel_path == group.result_doc_rel_path)
        )
    ).one()
    assert ref.agent_id == a.id


@pytest.mark.asyncio
async def test_create_group_requires_goal_and_two_members(async_session: AsyncSession):
    a = await _make_agent(async_session, "Alpha")
    b = await _make_agent(async_session, "Beta")

    with pytest.raises(GroupValidationError):
        await create_group(async_session, name="G", goal="  ", member_ids=[a.id, b.id])
    with pytest.raises(GroupValidationError):
        await create_group(async_session, name="G", goal="Ziel", member_ids=[a.id])


@pytest.mark.asyncio
async def test_create_group_rejects_non_comm_v2_member(async_session: AsyncSession):
    a = await _make_agent(async_session, "Alpha")
    legacy = await _make_agent(async_session, "Legacy", comm_v2=False)

    with pytest.raises(GroupMemberNotCapable) as exc:
        await create_group(
            async_session, name="G", goal="Ziel", member_ids=[a.id, legacy.id]
        )
    assert "Legacy" in str(exc.value)


@pytest.mark.asyncio
async def test_lead_defaults_to_board_lead_member(async_session: AsyncSession):
    dev = await _make_agent(async_session, "Dev")
    boss = await _make_agent(async_session, "Boss", is_board_lead=True)

    group = await create_group(
        async_session, name="G", goal="Ziel", member_ids=[dev.id, boss.id]
    )
    assert group.lead_agent_id == boss.id


@pytest.mark.asyncio
async def test_post_user_message_resolves_mentions(async_session: AsyncSession):
    a = await _make_agent(async_session, "Alpha")
    b = await _make_agent(async_session, "Free-Code")
    group = await create_group(
        async_session, name="G", goal="Ziel", member_ids=[a.id, b.id], lead_agent_id=a.id
    )

    # Explizite Mention, fold-tolerant → kanonischer Slug des Mitglieds
    msg = await post_user_message(async_session, group, "@FreeCode bitte prüfen")
    assert msg.mentions == ["free-code"]

    # "@alle" → alle Mitglieder
    msg = await post_user_message(async_session, group, "@alle Status bitte")
    assert sorted(msg.mentions) == ["alpha", "free-code"]

    # MC ist zweisprachig: die englische Oberfläche schickt "@all". Ohne
    # diesen Fall ginge die Nachricht still nur an den Lead.
    msg = await post_user_message(async_session, group, "@all status please")
    assert sorted(msg.mentions) == ["alpha", "free-code"]

    # Keine Mention → Lead (sonst erreicht die Nachricht niemanden)
    msg = await post_user_message(async_session, group, "wie sieht es aus?")
    assert msg.mentions == ["alpha"]


@pytest.mark.asyncio
async def test_add_and_remove_member(async_session: AsyncSession):
    a = await _make_agent(async_session, "Alpha")
    b = await _make_agent(async_session, "Beta")
    c = await _make_agent(async_session, "Gamma")
    group = await create_group(
        async_session, name="G", goal="Ziel", member_ids=[a.id, b.id], lead_agent_id=a.id
    )

    await add_member(async_session, group, c.id)
    members = (
        await async_session.exec(
            select(GroupMember).where(GroupMember.group_id == group.id)
        )
    ).all()
    assert {m.agent_id for m in members} == {a.id, b.id, c.id}

    await remove_member(async_session, group, c.id)
    members = (
        await async_session.exec(
            select(GroupMember).where(GroupMember.group_id == group.id)
        )
    ).all()
    assert {m.agent_id for m in members} == {a.id, b.id}

    # Lead ist nicht entfernbar — erst wechseln (Plan §4.5)
    with pytest.raises(GroupValidationError):
        await remove_member(async_session, group, a.id)


@pytest.mark.asyncio
async def test_add_member_rejects_non_comm_v2(async_session: AsyncSession):
    a = await _make_agent(async_session, "Alpha")
    b = await _make_agent(async_session, "Beta")
    legacy = await _make_agent(async_session, "Legacy", comm_v2=False)
    group = await create_group(
        async_session, name="G", goal="Ziel", member_ids=[a.id, b.id]
    )

    with pytest.raises(GroupMemberNotCapable):
        await add_member(async_session, group, legacy.id)
