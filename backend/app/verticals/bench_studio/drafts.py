"""Draft creation — the publish tail of a challenge.

NO second lifecycle (spec §3, ADR-065): this module only creates the existing
core objects — one ContentPipeline row + one Approval(action_type="x_post",
payload.media_paths). Posting happens in the CORE approval hook
(_handle_x_post_resolution -> x_publisher.post_media, PR 1); this vertical
reacts to the outcome via the x_post_resolved_hooks registry and flips its
challenge to `published`.

Two-stage gate (spec §4): Studio review ("do I like the result?") is separate
from the post approval ("should this go out?"). A run may never be posted —
the gallery is history.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.approval import Approval
from app.models.bench import BenchChallenge, BenchEntry
from app.models.board import Board
from app.models.content import ContentPipeline
from app.services import x_publisher

from .orchestrator import compose_challenge

logger = logging.getLogger("mc.bench_studio")


async def create_draft(
    session: AsyncSession,
    challenge: BenchChallenge,
    *,
    tweet_text: str,
    include_speed_labels: bool = False,
    board_id: uuid.UUID | None = None,
) -> Approval:
    """Validate + create the ContentPipeline row and the x_post Approval.

    Raises HTTPException (409/400/422) on validation problems — the router
    passes these through unchanged.
    """
    if challenge.status not in ("review", "drafted"):
        raise HTTPException(
            409,
            f"Challenge is {challenge.status!r} — drafts only from review/drafted.",
        )

    # Guard: reject if a pending x_post Approval already exists for this challenge.
    # Filter pending x_post approvals in SQL; match bench_challenge_id in Python
    # (JSON column — avoids DB-specific JSON operators).
    pending_x_posts = (
        await session.exec(
            select(Approval).where(
                Approval.action_type == "x_post",
                Approval.status == "pending",
            )
        )
    ).all()
    for existing in pending_x_posts:
        payload = existing.payload or {}
        if payload.get("bench_challenge_id") == str(challenge.id):
            raise HTTPException(
                409,
                "pending x_post approval exists for this challenge — resolve it first",
            )

    validation = x_publisher.validate_draft(tweet_text)
    if not validation.ok:
        raise HTTPException(400, "; ".join(validation.errors))

    entries = (
        await session.exec(
            select(BenchEntry).where(BenchEntry.challenge_id == challenge.id)
        )
    ).all()
    rendered = sorted(
        [e for e in entries if e.status == "rendered" and e.video_path],
        key=lambda e: e.model_label,
    )

    media_path = challenge.composed_video_path
    if include_speed_labels and challenge.mode == "side_by_side" and len(rendered) >= 2:
        # Re-compose with metric overlays ("DeepSeek · 42 s · 87 tok/s") —
        # separate output so the clean grid stays available.
        media_path = await compose_challenge(
            session, challenge, rendered, speed_labels=True, output_name="grid-speeds.mp4"
        )
    if media_path is None and rendered:
        media_path = rendered[0].video_path
    if media_path is None:
        raise HTTPException(422, "No video available — render first, then draft.")

    media_validation = x_publisher.validate_media([media_path])
    if not media_validation.ok:
        raise HTTPException(400, "; ".join(media_validation.errors))

    if board_id is None:
        board = (await session.exec(select(Board))).first()
        if board is None:
            raise HTTPException(
                422, "No board exists — ContentPipeline/Approval need a board."
            )
        board_id = board.id

    # Reuse the existing pipeline row if one is already linked to this challenge.
    # This prevents a second pipeline row from being orphaned on re-draft.
    pipeline: ContentPipeline | None = None
    if challenge.content_pipeline_id is not None:
        pipeline = await session.get(ContentPipeline, challenge.content_pipeline_id)

    if pipeline is not None:
        # Update in-place: reset to "review" state with the new tweet text.
        pipeline.title = challenge.title
        pipeline.final_content = tweet_text
        pipeline.status = "review"
        pipeline.published_url = None
        pipeline.published_platform = None
        pipeline.published_at = None
        session.add(pipeline)
        await session.flush()
    else:
        pipeline = ContentPipeline(
            board_id=board_id,
            title=challenge.title,
            content_type="social",
            status="review",  # core hook sets "published" after a successful post
            brief=challenge.prompt_text[:2000],
            final_content=tweet_text,
        )
        session.add(pipeline)
        await session.flush()  # need pipeline.id before commit

    approval = Approval(
        board_id=board_id,
        action_type="x_post",
        description=(
            f"Benchmark Studio: X post for challenge {challenge.title!r}: "
            f"{tweet_text[:160]}"
        ),
        payload={
            "text": tweet_text,
            "media_paths": [media_path],
            "content_pipeline_id": str(pipeline.id),
            "bench_challenge_id": str(challenge.id),
        },
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        status="pending",
    )
    session.add(approval)

    challenge.content_pipeline_id = pipeline.id
    challenge.status = "drafted"
    session.add(challenge)
    await session.commit()
    await session.refresh(approval)

    # SSE best-effort (same pattern as routers/x_posts.py)
    try:
        from app.redis_client import RedisKeys, get_redis

        redis = await get_redis()
        await redis.publish(
            RedisKeys.approvals_events(),
            (
                f'{{"type":"approval.created","approval_id":"{approval.id}",'
                f'"action_type":"x_post"}}'
            ),
        )
    except Exception:  # noqa: BLE001
        pass
    return approval


async def on_x_post_resolved(session, approval, resolution_status: str, result) -> None:
    """x_post_resolved_hook (core registry, Task 2): flip the challenge to
    `published` when its draft was approved AND the post succeeded.
    Self-filters via payload.bench_challenge_id — silent no-op otherwise."""
    payload = approval.payload or {}
    challenge_id = payload.get("bench_challenge_id")
    if not challenge_id:
        return
    try:
        challenge = await session.get(BenchChallenge, uuid.UUID(challenge_id))
    except ValueError:
        return
    if challenge is None:
        return

    if resolution_status == "approved" and result and result.get("ok"):
        challenge.status = "published"
        challenge.error = None
    elif resolution_status == "approved":
        # Post attempted but failed — stay drafted, surface the error.
        err = result or {}
        challenge.error = (
            f"post failed: {err.get('error_type')}: {err.get('error')}"[:2000]
        )
    # rejected: stays drafted — Mark can edit + re-draft (gallery = history).
    session.add(challenge)
    await session.commit()
