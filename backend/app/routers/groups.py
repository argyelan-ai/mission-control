"""Gruppen-API (Gruppenchat V1, PR A) — CRUD, Mitglieder, Nachrichten, Dokument.

Eine Gruppe = Thread(kind="group") + agent_groups-Zeile + group_members
(models/group.py). Schreiblogik liegt im group_service — dieser Router
übersetzt nur HTTP ↔ Service und hält das Fehler-Vokabular:
422 = ungültige Eingabe · 409 = Mitglied nicht gruppenfähig (comm_v2 fehlt,
Spiegel von input_not_supported im Sessions-Chat) · 404 = gibt es nicht.

Runden-Steuerung (start/pause/stop) kommt mit der Engine in PR B.
"""

import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import require_user
from app.database import get_session
from app.models.agent import Agent
from app.models.group import AgentGroup, GroupMember
from app.models.thread import Message
from app.redis_client import RedisKeys
from app.services import group_service, reference_ingest
from app.services.group_service import (
    GroupMemberNotCapable,
    GroupValidationError,
)
from app.services.sse import broadcast

router = APIRouter(prefix="/api/v1", tags=["groups"])


class GroupCreate(BaseModel):
    goal: str
    member_ids: list[uuid.UUID]
    name: str | None = None
    lead_agent_id: uuid.UUID | None = None
    lifecycle: str = "one_shot"
    max_rounds: int = 3
    max_duration_minutes: int | None = None
    budget_usd: float | None = None
    budget_tokens: int | None = None


class GroupUpdate(BaseModel):
    name: str | None = None
    goal: str | None = None
    max_rounds: int | None = None
    max_duration_minutes: int | None = None
    budget_usd: float | None = None
    budget_tokens: int | None = None
    human_every_n_rounds: int | None = None
    pause_on_failed_rounds: int | None = None
    speaker_timeout_seconds: int | None = None


class MemberAdd(BaseModel):
    agent_id: uuid.UUID
    role: str = "member"


class GroupMessageCreate(BaseModel):
    text: str


def _serialize_member(member: GroupMember, agent: Agent) -> dict:
    return {
        "id": str(agent.id),
        "name": agent.name,
        "slug": agent.slug,
        "emoji": agent.emoji,
        "role": member.role,
        "archived": agent.archived_at is not None,
    }


def _serialize_message(m: Message) -> dict:
    return {
        "id": str(m.id),
        "thread_id": str(m.thread_id),
        "seq": m.seq,
        "sender_type": m.sender_type,
        "sender_id": str(m.sender_id) if m.sender_id else None,
        "message_type": m.message_type,
        "body": m.body,
        "mentions": m.mentions,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


async def _members_with_agents(
    session: AsyncSession, group: AgentGroup
) -> list[tuple[GroupMember, Agent]]:
    rows = (
        await session.exec(
            select(GroupMember, Agent)
            .join(Agent, Agent.id == GroupMember.agent_id)  # type: ignore[arg-type]
            .where(GroupMember.group_id == group.id)
            .order_by(GroupMember.added_at.asc())  # type: ignore[union-attr]
        )
    ).all()
    return list(rows)


async def _serialize_group(session: AsyncSession, group: AgentGroup) -> dict:
    members = await _members_with_agents(session, group)
    return {
        "id": str(group.id),
        "thread_id": str(group.thread_id),
        "name": group.name,
        "goal": group.goal,
        "lifecycle": group.lifecycle,
        "status": group.status,
        "lead_agent_id": str(group.lead_agent_id) if group.lead_agent_id else None,
        "max_rounds": group.max_rounds,
        "max_duration_minutes": group.max_duration_minutes,
        "budget_usd": group.budget_usd,
        "budget_tokens": group.budget_tokens,
        "rounds_completed": group.rounds_completed,
        "current_round_no": group.current_round_no,
        "result_doc_rel_path": group.result_doc_rel_path,
        "created_at": group.created_at.isoformat() if group.created_at else None,
        "members": [_serialize_member(m, a) for m, a in members],
    }


async def _get_group_or_404(session: AsyncSession, group_id: uuid.UUID) -> AgentGroup:
    group = await session.get(AgentGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Gruppe nicht gefunden")
    return group


# WICHTIG: statische Segmente VOR parametrisierten Routen (Router Ordering
# Note in CLAUDE.local.md) — sonst wird "eligible-members" als UUID geparst.
@router.get("/groups/eligible-members")
async def eligible_members(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Gruppenfähige Agenten: comm_v2 UND nicht archiviert. Die Fähigkeit
    entscheidet das Backend — die UI re-implementiert die Regel nicht."""
    agents = (
        await session.exec(
            select(Agent)
            .where(Agent.comm_v2 == True, Agent.archived_at.is_(None))  # noqa: E712
            .order_by(Agent.name.asc())  # type: ignore[union-attr]
        )
    ).all()
    return [
        {"id": str(a.id), "name": a.name, "slug": a.slug, "emoji": a.emoji}
        for a in agents
    ]


@router.get("/groups")
async def list_groups(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    groups = (
        await session.exec(
            select(AgentGroup).order_by(AgentGroup.created_at.desc())  # type: ignore[union-attr]
        )
    ).all()
    out = []
    for group in groups:
        member_count = len(
            (
                await session.exec(
                    select(GroupMember.agent_id).where(GroupMember.group_id == group.id)
                )
            ).all()
        )
        out.append(
            {
                "id": str(group.id),
                "thread_id": str(group.thread_id),
                "name": group.name,
                "goal": group.goal,
                "status": group.status,
                "lifecycle": group.lifecycle,
                "member_count": member_count,
                "rounds_completed": group.rounds_completed,
                "current_round_no": group.current_round_no,
                "max_rounds": group.max_rounds,
                "created_at": group.created_at.isoformat() if group.created_at else None,
            }
        )
    return out


@router.post("/groups", status_code=status.HTTP_201_CREATED)
async def create_group(
    payload: GroupCreate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    try:
        group = await group_service.create_group(
            session,
            goal=payload.goal,
            member_ids=payload.member_ids,
            name=payload.name,
            lead_agent_id=payload.lead_agent_id,
            lifecycle=payload.lifecycle,
            max_rounds=payload.max_rounds,
            max_duration_minutes=payload.max_duration_minutes,
            budget_usd=payload.budget_usd,
            budget_tokens=payload.budget_tokens,
        )
    except GroupMemberNotCapable as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except GroupValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return await _serialize_group(session, group)


@router.get("/groups/{group_id}")
async def group_detail(
    group_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    group = await _get_group_or_404(session, group_id)
    return await _serialize_group(session, group)


@router.patch("/groups/{group_id}")
async def update_group(
    group_id: uuid.UUID,
    payload: GroupUpdate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    group = await _get_group_or_404(session, group_id)
    changes = payload.model_dump(exclude_unset=True)
    if "goal" in changes and not (changes["goal"] or "").strip():
        raise HTTPException(status_code=422, detail="goal darf nicht leer werden")
    if "max_rounds" in changes and (changes["max_rounds"] or 0) < 1:
        raise HTTPException(status_code=422, detail="max_rounds muss >= 1 sein")
    for key, value in changes.items():
        setattr(group, key, value)
    session.add(group)
    await session.commit()
    await session.refresh(group)
    return await _serialize_group(session, group)


@router.post("/groups/{group_id}/members", status_code=status.HTTP_201_CREATED)
async def add_group_member(
    group_id: uuid.UUID,
    payload: MemberAdd,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    group = await _get_group_or_404(session, group_id)
    try:
        member = await group_service.add_member(
            session, group, payload.agent_id, role=payload.role
        )
    except GroupMemberNotCapable as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except GroupValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    await broadcast(
        RedisKeys.group_events(str(group.id)),
        "group.member_changed",
        {"group_id": str(group.id), "agent_id": str(payload.agent_id), "change": "added"},
    )
    agent = await session.get(Agent, payload.agent_id)
    return _serialize_member(member, agent)


@router.delete(
    "/groups/{group_id}/members/{agent_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def remove_group_member(
    group_id: uuid.UUID,
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    group = await _get_group_or_404(session, group_id)
    try:
        await group_service.remove_member(session, group, agent_id)
    except GroupValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    await broadcast(
        RedisKeys.group_events(str(group.id)),
        "group.member_changed",
        {"group_id": str(group.id), "agent_id": str(agent_id), "change": "removed"},
    )


@router.get("/groups/{group_id}/messages")
async def group_messages(
    group_id: uuid.UUID,
    since_seq: int | None = None,
    limit: int = 200,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    group = await _get_group_or_404(session, group_id)
    query = (
        select(Message)
        .where(Message.thread_id == group.thread_id)
        .order_by(Message.seq.asc())  # type: ignore[union-attr]
    )
    if since_seq is not None:
        query = query.where(Message.seq > since_seq)
    msgs = (await session.exec(query.limit(max(1, min(limit, 500))))).all()
    return {"messages": [_serialize_message(m) for m in msgs]}


@router.post("/groups/{group_id}/messages", status_code=status.HTTP_201_CREATED)
async def post_group_message(
    group_id: uuid.UUID,
    payload: GroupMessageCreate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    group = await _get_group_or_404(session, group_id)
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="Leere Nachricht")
    message = await group_service.post_user_message(session, group, text)
    serialized = _serialize_message(message)
    await broadcast(
        RedisKeys.group_events(str(group.id)),
        "group.message_posted",
        {"group_id": str(group.id), "message": serialized},
    )
    return serialized


@router.get("/groups/{group_id}/document")
async def group_document(
    group_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    group = await _get_group_or_404(session, group_id)
    if not group.result_doc_rel_path:
        raise HTTPException(status_code=404, detail="Gruppe hat kein Ergebnis-Dokument")
    abs_path = os.path.join(
        reference_ingest.references_root(), group.result_doc_rel_path
    )
    if not os.path.isfile(abs_path):
        # Ehrlich statt leerer 200: die Datei fehlt auf der Platte (gelöscht?).
        raise HTTPException(
            status_code=404, detail="Ergebnis-Dokument fehlt auf der Platte"
        )
    with open(abs_path, encoding="utf-8") as fh:
        content = fh.read()
    return {
        "rel_path": group.result_doc_rel_path,
        "content": content,
        "mtime": os.path.getmtime(abs_path),
    }
