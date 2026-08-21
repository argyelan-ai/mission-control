"""Gruppen-Service — einzige Schreibstelle für Gruppen (Gruppenchat V1, PR A).

Erzwingt die Invarianten, die das Datenmodell allein nicht halten kann:
- goal ist Pflicht (eine Gruppe ohne Ziel gibt es nicht),
- mindestens 2 Mitglieder, alle comm_v2-fähig und nicht archiviert,
- der Lead ist immer Mitglied und nicht entfernbar (erst wechseln),
- Marks Nachrichten bekommen aufgelöste Mentions (fold-tolerant gegen die
  Mitglieder; "@alle" → alle; keine Mention → Lead) — ohne Mentions würde
  der Zustell-Filter (routers/agents._group_message_visible_to) die
  Nachricht niemandem zustellen.

Das Ergebnis-Dokument (Plan §4.4) entsteht hier als Skelett unter
references/groups/<slug>/result.md + ReferenceFile-Row beim Lead: Backend-
und Agenten-Container mounten ~/.mc 1:1, der Lead editiert später mit seinen
normalen Datei-Tools — kein eigener Schreib-Endpoint.
"""
from __future__ import annotations

import os
import uuid

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.group import (
    GROUP_LIFECYCLES,
    AgentGroup,
    GroupMember,
)
from app.models.reference_file import ReferenceFile
from app.models.thread import Message, Thread
from app.services import reference_ingest
from app.services.chat_inbound import _fold, parse_handles
from app.services.messaging import post_message
from app.utils import slugify


class GroupValidationError(ValueError):
    """Ungültige Eingabe — der Router antwortet 422."""


class GroupMemberNotCapable(Exception):
    """Mitglied ohne comm_v2 (oder archiviert) — der Router antwortet 409,
    Spiegel von input_not_supported im Sessions-Chat."""

    def __init__(self, agent_names: list[str]):
        self.agent_names = agent_names
        super().__init__(
            "Nicht gruppenfähig (comm_v2 fehlt oder archiviert): "
            + ", ".join(agent_names)
        )


_DOC_SKELETON = """# {name}

> Lebendes Ergebnis-Dokument dieser Gruppe. Es schreibt NUR der Lead-Agent —
> im Synthese-Turn am Ende jeder Runde. Jede Runde hinterlässt einen
> Snapshot (Versions-Verlauf in der Gruppen-Ansicht).

## Ziel

{goal}

## Stand

_Noch keine Runde gelaufen._
"""


def _canonical_handle(agent: Agent) -> str:
    """Der Slug ist die kanonische Anrede; Fallback: Name in Slug-Form."""
    return agent.slug or slugify(agent.name or "") or str(agent.id)


async def _load_capable_agents(
    session: AsyncSession, member_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Agent]:
    agents = (
        await session.exec(select(Agent).where(Agent.id.in_(member_ids)))  # type: ignore[union-attr]
    ).all()
    by_id = {a.id: a for a in agents}
    missing = [str(i) for i in member_ids if i not in by_id]
    if missing:
        raise GroupValidationError(f"Unbekannte Agenten: {', '.join(missing)}")
    incapable = [
        a.name
        for a in agents
        if not getattr(a, "comm_v2", False) or a.archived_at is not None
    ]
    if incapable:
        raise GroupMemberNotCapable(incapable)
    return by_id


async def create_group(
    session: AsyncSession,
    *,
    goal: str,
    member_ids: list[uuid.UUID],
    name: str | None = None,
    lead_agent_id: uuid.UUID | None = None,
    lifecycle: str = "one_shot",
    max_rounds: int = 3,
    max_duration_minutes: int | None = None,
    budget_usd: float | None = None,
    budget_tokens: int | None = None,
) -> AgentGroup:
    goal = (goal or "").strip()
    if not goal:
        raise GroupValidationError("goal ist Pflicht — eine Gruppe ohne Ziel gibt es nicht")
    ids = list(dict.fromkeys(member_ids or []))
    if len(ids) < 2:
        raise GroupValidationError("Eine Gruppe braucht mindestens 2 Mitglieder")
    if lifecycle not in GROUP_LIFECYCLES:
        raise GroupValidationError(f"lifecycle muss eines von {GROUP_LIFECYCLES} sein")
    if max_rounds < 1:
        raise GroupValidationError("max_rounds muss >= 1 sein (harter Deckel, Pflicht)")

    by_id = await _load_capable_agents(session, ids)
    if lead_agent_id is None:
        # Default: das Board-Lead-Mitglied, sonst das erste Mitglied.
        lead_agent_id = next(
            (i for i in ids if by_id[i].is_board_lead), ids[0]
        )
    if lead_agent_id not in by_id:
        raise GroupValidationError("Der Lead muss Mitglied der Gruppe sein")

    display_name = (name or "").strip() or goal[:60]

    thread = Thread(kind="group", title=display_name)
    session.add(thread)
    await session.commit()
    await session.refresh(thread)

    group = AgentGroup(
        thread_id=thread.id,
        name=display_name,
        goal=goal,
        lifecycle=lifecycle,
        lead_agent_id=lead_agent_id,
        max_rounds=max_rounds,
        max_duration_minutes=max_duration_minutes,
        budget_usd=budget_usd,
        budget_tokens=budget_tokens,
        status="idle",
    )
    session.add(group)
    await session.commit()
    await session.refresh(group)

    for agent_id in ids:
        session.add(
            GroupMember(
                group_id=group.id,
                agent_id=agent_id,
                role="lead" if agent_id == lead_agent_id else "member",
            )
        )
    await session.commit()

    # Dokument-Skelett + ReferenceFile-Row (Plan §4.4). id-Suffix im Slug:
    # zwei Gruppen dürfen denselben Namen tragen, ihre Dokumente nie denselben Pfad.
    doc_slug = f"{slugify(display_name) or 'gruppe'}-{group.id.hex[:6]}"
    rel_path = f"groups/{doc_slug}/result.md"
    abs_path = os.path.join(reference_ingest.references_root(), rel_path)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    content = _DOC_SKELETON.format(name=display_name, goal=goal)
    with open(abs_path, "w", encoding="utf-8") as fh:
        fh.write(content)

    group.result_doc_rel_path = rel_path
    session.add(group)
    session.add(
        ReferenceFile(
            agent_id=lead_agent_id,
            rel_path=rel_path,
            original_name="result.md",
            mime="text/markdown",
            size=len(content.encode("utf-8")),
            note=f"Ergebnis-Dokument der Gruppe «{display_name}»",
            uploaded_by="system",
        )
    )
    await session.commit()
    await session.refresh(group)
    return group


async def group_member_agents(session: AsyncSession, group: AgentGroup) -> list[Agent]:
    """Mitglieder in Beitritts-Reihenfolge (stabil für speaker_order, PR B)."""
    rows = (
        await session.exec(
            select(GroupMember, Agent)
            .join(Agent, Agent.id == GroupMember.agent_id)  # type: ignore[arg-type]
            .where(GroupMember.group_id == group.id)
            .order_by(GroupMember.added_at.asc())  # type: ignore[union-attr]
        )
    ).all()
    return [agent for _member, agent in rows]


def _member_mention_hits(members: list[Agent], text: str) -> tuple[list[str], list[str]]:
    """(Treffer als kanonische Slugs, explizite Roh-Handles) — fold-tolerant
    gegen Slug UND Name, in Nennungs-Reihenfolge, dedupliziert."""
    explicit, candidates = parse_handles(text or "")
    by_fold: dict[str, str] = {}
    for agent in members:
        canonical = _canonical_handle(agent)
        for key in (_fold(agent.slug or ""), _fold(agent.name or "")):
            if key:
                by_fold.setdefault(key, canonical)
    hits: list[str] = []
    for handle in candidates:
        canonical = by_fold.get(_fold(handle))
        if canonical is not None and canonical not in hits:
            hits.append(canonical)
    return hits, explicit


def resolve_user_mentions(
    members: list[Agent], text: str, *, lead_agent_id: uuid.UUID | None
) -> list[str]:
    """Mentions einer Operator-Nachricht → kanonische Mitglieds-Slugs.

    "@alle" schlägt alles. Kein Treffer → der Lead (die Nachricht muss
    IRGENDWEN erreichen — Zustellung ist mention-gefiltert).
    """
    hits, explicit = _member_mention_hits(members, text)
    if any(_fold(h) == "alle" for h in explicit):
        return [_canonical_handle(a) for a in members]
    if hits:
        return hits
    lead = next((a for a in members if a.id == lead_agent_id), None)
    return [_canonical_handle(lead)] if lead is not None else []


def resolve_agent_mentions(members: list[Agent], text: str) -> list[str]:
    """Mentions eines AGENTEN-Posts — bewusst enger als beim Operator:
    kein Lead-Default (unaufgefordert weckt niemanden — Sturm-Schutz) und
    kein "@alle" (ein Agent kann nicht die ganze Gruppe wecken; das Wort an
    alle erteilt nur die Runden-Engine)."""
    hits, _explicit = _member_mention_hits(members, text)
    return hits


async def post_user_message(
    session: AsyncSession,
    group: AgentGroup,
    text: str,
    *,
    message_type: str = "message",
) -> Message:
    """Marks Nachricht in den Gruppen-Thread, mit aufgelösten Mentions.

    mirror_to_telegram=False: Gruppen spiegeln in V1 bewusst NICHT in die
    Chat-Kanäle (Plan §4.5) — eine autonome Runde würde Slack/Telegram fluten.
    """
    members = await group_member_agents(session, group)
    mentions = resolve_user_mentions(
        members, text, lead_agent_id=group.lead_agent_id
    )
    return await post_message(
        session,
        thread_id=group.thread_id,
        sender_type="user",
        message_type=message_type,
        body=text,
        mentions=mentions,
        mirror_to_telegram=False,
    )


async def add_member(
    session: AsyncSession, group: AgentGroup, agent_id: uuid.UUID, *, role: str = "member"
) -> GroupMember:
    await _load_capable_agents(session, [agent_id])
    existing = await session.get(GroupMember, (group.id, agent_id))
    if existing is not None:
        return existing
    member = GroupMember(group_id=group.id, agent_id=agent_id, role=role)
    session.add(member)
    await session.commit()
    return member


async def remove_member(
    session: AsyncSession, group: AgentGroup, agent_id: uuid.UUID
) -> None:
    if agent_id == group.lead_agent_id:
        raise GroupValidationError(
            "Der Lead ist nicht entfernbar — erst einen neuen Lead wählen"
        )
    member = await session.get(GroupMember, (group.id, agent_id))
    if member is None:
        return
    await session.delete(member)
    await session.commit()
