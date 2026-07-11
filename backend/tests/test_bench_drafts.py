"""bench_studio drafts — publish tail via CORE Approval + ContentPipeline.

Asserts the 'no second lifecycle' contract: draft creation produces exactly
one Approval(action_type='x_post') + one ContentPipeline row; publishing is
done by the CORE approval hook, the vertical only flips its status via the
x_post_resolved_hooks registry.
"""
import uuid
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("app.verticals.bench_studio")

from fastapi import HTTPException
from sqlmodel import select

from app.models.approval import Approval
from app.models.bench import BenchChallenge, BenchEntry
from app.models.board import Board
from app.models.content import ContentPipeline
from app.verticals.bench_studio import drafts
from app.services.x_publisher import DraftValidation


@pytest.fixture(autouse=True)
def _validate_media_ok(monkeypatch):
    """PR-1 validate_media passes by default; individual tests override."""
    monkeypatch.setattr(
        drafts.x_publisher, "validate_media",
        lambda paths: DraftValidation(ok=True),
        raising=False,
    )


async def _seed_review_challenge(session, *, mode="side_by_side", composed=True):
    board = Board(name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
    session.add(board)
    ch = BenchChallenge(
        title="Spark vs Claude",
        prompt_text="one-shot page",
        mode=mode,
        status="review",
        composed_video_path="/shared-deliverables/bench-x/grid.mp4" if composed else None,
    )
    session.add(ch)
    await session.commit()
    await session.refresh(board)
    await session.refresh(ch)
    entries = [
        BenchEntry(challenge_id=ch.id, model_label="A", source_kind="spark",
                   status="rendered", video_path="/sd/a.mp4",
                   metrics={"duration_ms": 42000, "tok_per_s": 87.0}),
        BenchEntry(challenge_id=ch.id, model_label="B", source_kind="agent",
                   status="rendered", video_path="/sd/b.mp4",
                   metrics={"duration_ms": 61000}),
    ]
    for e in entries:
        session.add(e)
    await session.commit()
    return board, ch, entries


@pytest.mark.asyncio
async def test_create_draft_creates_pipeline_and_approval(session):
    board, ch, _ = await _seed_review_challenge(session)

    approval = await drafts.create_draft(
        session, ch, tweet_text="Spark vs Claude — one prompt, two worlds.",
        board_id=board.id,
    )

    assert approval.action_type == "x_post"
    assert approval.status == "pending"
    assert approval.payload["text"] == "Spark vs Claude — one prompt, two worlds."
    assert approval.payload["media_paths"] == ["/shared-deliverables/bench-x/grid.mp4"]
    assert approval.payload["bench_challenge_id"] == str(ch.id)

    pipeline_id = uuid.UUID(approval.payload["content_pipeline_id"])
    pipeline = await session.get(ContentPipeline, pipeline_id)
    assert pipeline is not None
    assert pipeline.content_type == "social"
    assert pipeline.status == "review"
    assert pipeline.final_content == "Spark vs Claude — one prompt, two worlds."

    await session.refresh(ch)
    assert ch.status == "drafted"
    assert ch.content_pipeline_id == pipeline_id

    # No second lifecycle: exactly ONE pending approval, of the CORE type.
    all_approvals = (await session.exec(select(Approval))).all()
    assert len(all_approvals) == 1


@pytest.mark.asyncio
async def test_create_draft_speed_labels_recomposes(session, monkeypatch):
    board, ch, _ = await _seed_review_challenge(session)
    compose_mock = AsyncMock(return_value="/shared-deliverables/bench-x/grid-speeds.mp4")
    monkeypatch.setattr(drafts, "compose_challenge", compose_mock)

    approval = await drafts.create_draft(
        session, ch, tweet_text="with speeds", include_speed_labels=True,
        board_id=board.id,
    )

    compose_mock.assert_awaited_once()
    assert compose_mock.await_args.kwargs["speed_labels"] is True
    assert compose_mock.await_args.kwargs["output_name"] == "grid-speeds.mp4"
    assert approval.payload["media_paths"] == [
        "/shared-deliverables/bench-x/grid-speeds.mp4"
    ]


@pytest.mark.asyncio
async def test_create_draft_single_mode_uses_entry_video(session):
    board, ch, entries = await _seed_review_challenge(session, mode="single", composed=False)
    approval = await drafts.create_draft(
        session, ch, tweet_text="solo run", board_id=board.id
    )
    assert approval.payload["media_paths"] == ["/sd/a.mp4"]


@pytest.mark.asyncio
async def test_create_draft_rejects_wrong_status(session):
    board, ch, _ = await _seed_review_challenge(session)
    ch.status = "generating"
    session.add(ch)
    await session.commit()
    with pytest.raises(HTTPException) as exc:
        await drafts.create_draft(session, ch, tweet_text="x", board_id=board.id)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_create_draft_rejects_overlong_text(session):
    board, ch, _ = await _seed_review_challenge(session)
    with pytest.raises(HTTPException) as exc:
        await drafts.create_draft(session, ch, tweet_text="x" * 281, board_id=board.id)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_create_draft_without_video_422(session):
    board = Board(name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
    session.add(board)
    ch = BenchChallenge(title="t", prompt_text="p", status="review")
    session.add(ch)
    await session.commit()
    await session.refresh(board)
    await session.refresh(ch)
    with pytest.raises(HTTPException) as exc:
        await drafts.create_draft(session, ch, tweet_text="x", board_id=board.id)
    assert exc.value.status_code == 422


# ── on_x_post_resolved ────────────────────────────────────────────────────


async def _approval_for(session, ch, board):
    approval = Approval(
        board_id=board.id,
        action_type="x_post",
        description="d",
        payload={"text": "t", "bench_challenge_id": str(ch.id)},
        status="approved",
    )
    session.add(approval)
    await session.commit()
    await session.refresh(approval)
    return approval


@pytest.mark.asyncio
async def test_resolved_ok_flips_to_published(session):
    board, ch, _ = await _seed_review_challenge(session)
    ch.status = "drafted"
    session.add(ch)
    await session.commit()
    approval = await _approval_for(session, ch, board)

    await drafts.on_x_post_resolved(
        session, approval, "approved",
        {"ok": True, "tweet_id": "1", "url": "https://x.com/i/status/1"},
    )
    await session.refresh(ch)
    assert ch.status == "published"


@pytest.mark.asyncio
async def test_resolved_failed_post_keeps_drafted_with_error(session):
    board, ch, _ = await _seed_review_challenge(session)
    ch.status = "drafted"
    session.add(ch)
    await session.commit()
    approval = await _approval_for(session, ch, board)

    await drafts.on_x_post_resolved(
        session, approval, "approved",
        {"ok": False, "error_type": "rate_limited", "error": "429"},
    )
    await session.refresh(ch)
    assert ch.status == "drafted"
    assert "rate_limited" in ch.error


@pytest.mark.asyncio
async def test_resolved_rejected_keeps_drafted(session):
    board, ch, _ = await _seed_review_challenge(session)
    ch.status = "drafted"
    session.add(ch)
    await session.commit()
    approval = await _approval_for(session, ch, board)

    await drafts.on_x_post_resolved(session, approval, "rejected", None)
    await session.refresh(ch)
    assert ch.status == "drafted"


@pytest.mark.asyncio
async def test_resolved_ignores_foreign_approvals(session):
    board = Board(name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
    session.add(board)
    await session.commit()
    await session.refresh(board)
    approval = Approval(
        board_id=board.id, action_type="x_post", description="d",
        payload={"text": "plain agent draft"}, status="approved",
    )
    session.add(approval)
    await session.commit()
    # Must be a silent no-op:
    await drafts.on_x_post_resolved(session, approval, "approved", {"ok": True})
