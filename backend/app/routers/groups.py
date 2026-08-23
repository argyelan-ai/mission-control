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

import logging

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import require_user
from app.database import get_session
from app.models.agent import Agent
from app.models.group import AgentGroup, GroupMember
from app.models.memory import BoardMemory
from app.models.thread import Message
from app.redis_client import RedisKeys
from app.services import group_service, reference_ingest
from app.services.group_service import (
    GroupMemberNotCapable,
    GroupRunningError,
    GroupValidationError,
)
from app.services.memory_indexing import index_memory
from app.services.sse import broadcast

logger = logging.getLogger(__name__)

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
    # Der Lead muss wechselbar sein: fällt er aus (hängendes CLI, archiviert),
    # steckt die Gruppe sonst fest — er ist nicht entfernbar, und nur er darf
    # urteilen und das Ergebnis-Dokument schreiben. Live-Befund 21.08.2026.
    lead_agent_id: uuid.UUID | None = None
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
        "archived_at": group.archived_at.isoformat() if group.archived_at else None,
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
    include_archived: bool = False,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    # Archivierte Gruppen sind nicht geloescht, nur weggeraeumt — die Liste
    # zeigt sie auf Wunsch wieder (Operator-Wunsch 22.08.2026).
    query = select(AgentGroup).order_by(AgentGroup.created_at.desc())  # type: ignore[union-attr]
    if not include_archived:
        query = query.where(AgentGroup.archived_at.is_(None))  # type: ignore[union-attr]
    groups = (await session.exec(query)).all()
    out = []
    for group in groups:
        members = await _members_with_agents(session, group)
        # Vorschau der letzten Nachricht + Avatare: die Sidebar-Zeile zeigt
        # beides (Hermes-Vorbild), soll dafür aber nicht pro Gruppe einen
        # zweiten Request brauchen.
        last = (
            await session.exec(
                select(Message)
                .where(Message.thread_id == group.thread_id)
                .order_by(Message.seq.desc())  # type: ignore[union-attr]
                .limit(1)
            )
        ).first()
        agent_names = {str(a.id): a.name for _m, a in members}
        last_preview = None
        if last is not None:
            sender = "System"
            if last.sender_type == "user":
                sender = "Operator"
            elif last.sender_id is not None:
                sender = agent_names.get(str(last.sender_id), "Agent")
            body = (last.body or "").strip().replace("\n", " ")
            last_preview = {
                "body": body[:160] + ("…" if len(body) > 160 else ""),
                "sender": sender,
                "created_at": last.created_at.isoformat() if last.created_at else None,
            }
        out.append(
            {
                "id": str(group.id),
                "thread_id": str(group.thread_id),
                "name": group.name,
                "goal": group.goal,
                "status": group.status,
                "lifecycle": group.lifecycle,
                "member_count": len(members),
                "member_avatars": [
                    {"id": str(a.id), "emoji": a.emoji, "name": a.name} for _m, a in members
                ],
                "last_message": last_preview,
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

    new_lead = changes.pop("lead_agent_id", None)
    if new_lead is not None and new_lead != group.lead_agent_id:
        member = await session.get(GroupMember, (group.id, new_lead))
        if member is None:
            raise HTTPException(
                status_code=422, detail="Der neue Lead muss Mitglied der Gruppe sein"
            )
        # Rollen mitziehen: es gibt genau einen Lead, und der alte fällt auf
        # „member" zurück — sonst trüge die Mitgliederliste zwei Leads.
        if group.lead_agent_id is not None:
            old = await session.get(GroupMember, (group.id, group.lead_agent_id))
            if old is not None and old.role == "lead":
                old.role = "member"
                session.add(old)
        member.role = "lead"
        session.add(member)
        group.lead_agent_id = new_lead

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


@router.get("/groups/{group_id}/stream")
async def group_stream(
    group_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """SSE-Strom eines Gruppenraums (Kanal `mc:events:group:{id}`).

    Trägt group.message_posted / round_started / turn_started /
    round_completed / doc_updated / gate_requested / status_changed /
    member_changed — die Live-Ansicht im Frontend hängt hier dran.
    """
    from app.services.sse import make_sse_response

    await _get_group_or_404(session, group_id)
    return make_sse_response([RedisKeys.group_events(str(group_id))])


@router.post("/groups/{group_id}/start")
async def start_group_endpoint(
    group_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    from app.services import group_runner as runner_service

    group = await _get_group_or_404(session, group_id)
    try:
        group = await runner_service.start_group(session, group)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return await _serialize_group(session, group)


@router.post("/groups/{group_id}/pause")
async def pause_group_endpoint(
    group_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    from app.services import group_runner as runner_service

    group = await _get_group_or_404(session, group_id)
    try:
        group = await runner_service.pause_group(session, group)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return await _serialize_group(session, group)


@router.post("/groups/{group_id}/stop")
async def stop_group_endpoint(
    group_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    from app.services import group_runner as runner_service

    group = await _get_group_or_404(session, group_id)
    group = await runner_service.stop_group(session, group)
    return await _serialize_group(session, group)


@router.get("/groups/{group_id}/rounds")
async def group_rounds(
    group_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    from app.models.group import GroupRound

    group = await _get_group_or_404(session, group_id)
    rounds = (
        await session.exec(
            select(GroupRound)
            .where(GroupRound.group_id == group.id)
            .order_by(GroupRound.created_at.asc())  # type: ignore[union-attr]
        )
    ).all()
    return {
        "rounds": [
            {
                "id": str(r.id),
                "round_no": r.round_no,
                "kind": r.kind,
                # Die UI setzt den Runden-Trenner exakt an diese seq — ohne
                # das Feld müsste sie den Brief-Text parsen (fragil).
                "brief_seq": r.brief_seq,
                "outcome": r.outcome,
                "report": r.report,
                "pending_speakers": r.pending_speakers,
                "has_doc_snapshot": r.doc_snapshot is not None,
                "tokens_used": r.tokens_used,
                "cost_usd": r.cost_usd,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            }
            for r in rounds
        ]
    }


@router.get("/groups/{group_id}/document")
async def group_document(
    group_id: uuid.UUID,
    version: int | None = None,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Aktuelles Ergebnis-Dokument (Datei) — oder mit ?version=n der
    Snapshot der Runde n (Versions-Blätterer im UI)."""
    from app.models.group import GroupRound

    group = await _get_group_or_404(session, group_id)
    if not group.result_doc_rel_path:
        raise HTTPException(status_code=404, detail="Gruppe hat kein Ergebnis-Dokument")

    if version is not None:
        snap = (
            await session.exec(
                select(GroupRound)
                .where(
                    GroupRound.group_id == group.id,
                    GroupRound.round_no == version,
                    GroupRound.doc_snapshot != None,  # noqa: E711
                )
                .order_by(GroupRound.created_at.desc())  # type: ignore[union-attr]
                .limit(1)
            )
        ).first()
        if snap is None:
            raise HTTPException(
                status_code=404, detail=f"Kein Dokument-Snapshot für Runde {version}"
            )
        return {
            "rel_path": group.result_doc_rel_path,
            "content": snap.doc_snapshot,
            "version": version,
        }

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
        "version": None,
        "mtime": os.path.getmtime(abs_path),
    }


# ── Abschluss: archivieren, löschen, ins Gedächtnis übernehmen ─────────────
# Operator-Wunsch 22.08.2026. Drei Stufen mit klar verschiedener Bedeutung,
# plus eine Memory-Übernahme, die von allen dreien unabhängig ist.


class MemorizeGroupPayload(BaseModel):
    title: str | None = None
    memory_type: str = "research"
    tags: list[str] = []


@router.post("/groups/{group_id}/archive")
async def archive_group(
    group_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    group = await _get_group_or_404(session, group_id)
    group = await group_service.archive_group(session, group)
    return await _serialize_group(session, group)


@router.post("/groups/{group_id}/unarchive")
async def unarchive_group(
    group_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    group = await _get_group_or_404(session, group_id)
    group = await group_service.unarchive_group(session, group)
    return await _serialize_group(session, group)


@router.post("/groups/{group_id}/memorize", status_code=status.HTTP_201_CREATED)
async def memorize_group_result(
    group_id: uuid.UUID,
    payload: MemorizeGroupPayload,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Das Ergebnis-Dokument als BoardMemory ablegen — dieselbe Ablage, die
    Tasks nutzen, damit dieselbe Einbettung greift.

    Bewusst ein eigener Aufruf und kein Anhängsel des Löschens: eine
    Erkenntnis soll ihr Arbeitsmaterial überleben können.
    """
    group = await _get_group_or_404(session, group_id)
    content = await group_service.read_result_document(group)
    if not content.strip():
        raise HTTPException(
            status_code=422, detail="Es gibt noch kein Ergebnis zum Übernehmen"
        )

    memory = BoardMemory(
        title=(payload.title or group.name or "Gruppen-Ergebnis")[:200],
        # Das Ziel wandert mit: ohne die Frage ist die Antwort in einem halben
        # Jahr nicht mehr einzuordnen — und die Einbettung findet sie schlechter.
        content=f"# {group.name}\n\n**Ziel:** {group.goal}\n\n---\n\n{content}",
        tags=list(payload.tags),
        source="group",
        memory_type=payload.memory_type,
        auto_generated=True,
    )
    session.add(memory)
    await session.commit()
    await session.refresh(memory)

    # Einbetten best effort: der Eintrag steht schon in der Datenbank, ein
    # ausgefallener Embedding-Dienst darf ihn nicht wieder wegnehmen.
    # index_memory kümmert sich selbst um Wiederholungen.
    try:
        await index_memory(memory)
    except Exception as exc:  # pragma: no cover - Netz-/Dienst-Sonderfall
        logger.warning("Einbettung des Gruppen-Ergebnisses %s fehlgeschlagen: %s", memory.id, exc)

    return {"memory_id": str(memory.id), "title": memory.title}


@router.delete("/groups/{group_id}")
async def delete_group(
    group_id: uuid.UUID,
    scope: str = "all",
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """`scope=chat` löscht nur den Verlauf (Ergebnis bleibt), `scope=all` alles.

    Zwei Antwortformen, weil zwei Dinge zurückkommen: bei `chat` gibt es die
    Gruppe noch, bei `all` nicht mehr.
    """
    if scope not in ("all", "chat"):
        raise HTTPException(status_code=422, detail="scope muss 'all' oder 'chat' sein")
    group = await _get_group_or_404(session, group_id)
    try:
        if scope == "chat":
            group = await group_service.delete_group_chat(session, group)
            await broadcast(
                RedisKeys.group_events(str(group.id)),
                "group.group_changed",
                {"group_id": str(group.id), "reason": "chat_deleted"},
            )
            return await _serialize_group(session, group)
        await group_service.delete_group_completely(session, group)
    except GroupRunningError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
