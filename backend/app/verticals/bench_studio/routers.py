"""Benchmark Studio router — operator-facing, JWT via require_user
(same auth dependency as the core approvals/files routers)."""
from __future__ import annotations

import logging
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import require_user
from app.database import get_session
from app.models.bench import BenchChallenge, BenchEntry
from app.utils import create_tracked_task

from . import orchestrator
from .drafts import create_draft

logger = logging.getLogger("mc.bench_studio")

router = APIRouter(prefix="/api/v1/bench", tags=["bench-studio"])


# ── Schemas ───────────────────────────────────────────────────────────────


class BenchModelSpec(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    source_kind: Literal["spark", "agent"]
    spark_model: str | None = None
    agent_id: uuid.UUID | None = None
    # Custom chip tag for the branded video (e.g. "OMP · DGX SPARK").
    # None -> harness-derived default (orchestrator._build_branding_payload).
    display_tag: str | None = Field(default=None, max_length=80)


class BenchChallengeCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    prompt_template_id: uuid.UUID | None = None
    prompt_text: str | None = None
    mode: Literal["single", "side_by_side"] = "side_by_side"
    models: list[BenchModelSpec] = Field(min_length=1, max_length=6)
    series_label: str | None = Field(default=None, max_length=80)


class BenchDraftCreate(BaseModel):
    tweet_text: str = Field(min_length=1, max_length=280)
    include_speed_labels: bool = False
    board_id: uuid.UUID | None = None


def _serialize(challenge: BenchChallenge, entries: list[BenchEntry]) -> dict:
    return {
        **challenge.model_dump(),
        "entries": [e.model_dump() for e in sorted(entries, key=lambda e: e.model_label)],
    }


async def _entries_for(session: AsyncSession, challenge_id: uuid.UUID) -> list[BenchEntry]:
    return (
        await session.exec(
            select(BenchEntry).where(BenchEntry.challenge_id == challenge_id)
        )
    ).all()


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.post("/challenges", status_code=status.HTTP_201_CREATED)
async def create_challenge(
    body: BenchChallengeCreate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    # Edited text wins over template body. Only use template body as fallback when prompt_text is empty.
    prompt_text = body.prompt_text
    if body.prompt_template_id is not None:
        from app.models.prompt_template import PromptTemplate  # PR 2

        template = await session.get(PromptTemplate, body.prompt_template_id)
        if template is None:
            raise HTTPException(404, "Prompt template not found")
        # Use template body only if prompt_text is empty/None (user did not edit or left it blank)
        if not prompt_text or not prompt_text.strip():
            prompt_text = template.body
    if not prompt_text or not prompt_text.strip():
        raise HTTPException(400, "prompt_text or prompt_template_id required")

    for spec in body.models:
        if spec.source_kind == "agent" and spec.agent_id is None:
            raise HTTPException(400, f"model {spec.label!r}: agent_id required")

    series_no = None
    if body.series_label:
        existing = (
            await session.exec(
                select(BenchChallenge.series_no).where(
                    BenchChallenge.series_label == body.series_label
                )
            )
        ).all()
        series_no = max([n for n in existing if n is not None], default=0) + 1

    challenge = BenchChallenge(
        title=body.title,
        prompt_template_id=body.prompt_template_id,
        prompt_text=prompt_text,
        mode=body.mode,
        series_label=body.series_label,
        series_no=series_no,
    )
    session.add(challenge)
    await session.flush()
    entries = [
        BenchEntry(
            challenge_id=challenge.id,
            model_label=spec.label,
            source_kind=spec.source_kind,
            spark_model=spec.spark_model,
            agent_id=spec.agent_id,
            display_tag=(spec.display_tag or "").strip() or None,
        )
        for spec in body.models
    ]
    for e in entries:
        session.add(e)
    await session.commit()
    await session.refresh(challenge)

    create_tracked_task(
        orchestrator.start_challenge(challenge.id),
        name=f"start_challenge({challenge.id})"
    )
    logger.info("bench challenge %s created (%d entries)", challenge.id, len(entries))
    return _serialize(challenge, entries)


@router.get("/challenges")
async def list_challenges(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    challenges = (
        await session.exec(
            select(BenchChallenge).order_by(BenchChallenge.created_at.desc())  # type: ignore[attr-defined]
        )
    ).all()
    all_entries = (await session.exec(select(BenchEntry))).all()
    by_challenge: dict[uuid.UUID, list[BenchEntry]] = {}
    for e in all_entries:
        by_challenge.setdefault(e.challenge_id, []).append(e)
    return [_serialize(c, by_challenge.get(c.id, [])) for c in challenges]


@router.get("/challenges/{challenge_id}")
async def get_challenge(
    challenge_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    challenge = await session.get(BenchChallenge, challenge_id)
    if challenge is None:
        raise HTTPException(404, "Challenge not found")
    entries = await _entries_for(session, challenge_id)
    # Poll-fallback for failed agent tasks (they never fire task_done):
    await orchestrator.reconcile_challenge(session, challenge, entries)
    return _serialize(challenge, entries)


@router.post("/challenges/{challenge_id}/draft", status_code=status.HTTP_201_CREATED)
async def create_challenge_draft(
    challenge_id: uuid.UUID,
    body: BenchDraftCreate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    challenge = await session.get(BenchChallenge, challenge_id)
    if challenge is None:
        raise HTTPException(404, "Challenge not found")
    from app.services.x_publisher import validate_draft

    warnings = validate_draft(body.tweet_text).warnings
    approval = await create_draft(
        session,
        challenge,
        tweet_text=body.tweet_text,
        include_speed_labels=body.include_speed_labels,
        board_id=body.board_id,
    )
    return {
        "approval_id": str(approval.id),
        "challenge_status": challenge.status,
        "warnings": warnings,
    }


@router.post("/challenges/{challenge_id}/rerender")
async def rerender_challenge(
    challenge_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    challenge = await session.get(BenchChallenge, challenge_id)
    if challenge is None:
        raise HTTPException(404, "Challenge not found")
    if challenge.status not in ("review", "drafted", "failed", "rendering", "composing"):
        raise HTTPException(
            409,
            f"Challenge is {challenge.status!r} — rerender only from review/drafted/failed/rendering/composing.",
        )
    create_tracked_task(
        orchestrator.rerender_challenge(challenge.id),
        name=f"rerender_challenge({challenge.id})"
    )
    return {"ok": True}


@router.post("/entries/{entry_id}/retry")
async def retry_entry(
    entry_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    entry = await session.get(BenchEntry, entry_id)
    if entry is None:
        raise HTTPException(404, "Entry not found")
    if entry.status != "failed":
        raise HTTPException(409, f"Entry is {entry.status!r} — retry only from failed.")
    create_tracked_task(
        orchestrator.retry_entry(entry.id),
        name=f"retry_entry({entry.id})"
    )
    return {"ok": True}
