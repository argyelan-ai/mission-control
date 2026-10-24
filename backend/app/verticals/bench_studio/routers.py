"""Benchmark Studio router — operator-facing, JWT via require_user
(same auth dependency as the core approvals/files routers). Exception: the
entry HTML view route uses require_bench_view, which additionally accepts a
short-lived resource-scoped view-token (see auth.create_bench_view_token) —
that route's URL is meant to be copied/shared/opened on a phone.

Bench W1 kill list (24.10.2026, concept 30 W1.8): the legacy create
(POST /challenges, the "Neue Challenge" dialog's 3-6 model grid), the
Spark-only GET /spark-models and /effort-levels*, and the X-draft path
(POST /challenges/{id}/draft -> Approval(x_post) -> x_publisher) are gone.
Tests start through POST /bench/tests (w1_routes); MC posts nothing to X.
What stays here: reading old challenges, stop/archive/delete, entry
rename/view/rerender/retry, and the challenge-wide rerender/recompose —
which hand W1 tests to the W1 runner (run 1 only, W1 templates)."""
from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import create_bench_view_token, require_bench_view, require_user
from app.database import get_session
from app.models.bench import BenchChallenge, BenchEntry
from app.utils import create_tracked_task
from app.verticals import hooks

from . import orchestrator

logger = logging.getLogger("mc.bench_studio")

router = APIRouter(prefix="/api/v1/bench", tags=["bench-studio"])

# Longest entry label create and PATCH accept.
LABEL_MAX_LEN = 80

BENCH_CHALLENGE_RUN_CLAIM_TTL_S = 1800  # self-heal net if the bg task dies without releasing


async def _claim_challenge_run(challenge_id: uuid.UUID) -> None:
    """Atomic per-challenge claim (SET NX EX) taken right before scheduling
    a render/compose background task.

    All three run-starting endpoints (challenge-wide rerender, recompose,
    per-entry rerender) end up mutating the SAME challenge row
    (composed_video_path / status) — the per-entry rerender's rate limit is
    keyed by entry_id, so it alone can't stop two DIFFERENT entries' buttons
    on the SAME challenge from being clicked in quick succession and racing
    each other (2026-07-15 review finding). The background task releases
    this claim in a `finally`
    (orchestrator._release_challenge_run_claim) — the TTL here is only a
    self-heal net for a task that dies without releasing.

    Redis outage -> fail-open (logged): the pre-existing challenge.status
    guard in each endpoint is the fallback, same trade-off as the per-entry
    rate limit."""
    from app.redis_client import RedisKeys, get_redis

    try:
        redis = await get_redis()
        claimed = await redis.set(
            RedisKeys.bench_challenge_run_claim(str(challenge_id)),
            "1", nx=True, ex=BENCH_CHALLENGE_RUN_CLAIM_TTL_S,
        )
        if not claimed:
            raise HTTPException(
                409, "Challenge already has a render/compose run in progress."
            )
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 — a Redis outage must never block the endpoint
        logger.warning(
            "bench challenge %s run-claim check skipped (redis unavailable)", challenge_id
        )


# ── Schemas ───────────────────────────────────────────────────────────────


class BenchChallengeUpdate(BaseModel):
    """Operator edit after a run — only presentation fields."""
    title: str | None = Field(default=None, min_length=1, max_length=200)


class BenchEntryUpdate(BaseModel):
    """Operator edit after a run — model name + chip tag (both feed the
    branded video). Empty-string display_tag clears the override."""
    # pydantic v2 reserves the "model_" namespace — model_label is a domain
    # name here (mirrors bench_entries.model_label), so opt out.
    model_config = {"protected_namespaces": ()}

    model_label: str | None = Field(default=None, min_length=1, max_length=LABEL_MAX_LEN)
    display_tag: str | None = Field(default=None, max_length=80)


def _serialize(
    challenge: BenchChallenge,
    entries: list[BenchEntry],
    task_states: dict[uuid.UUID, dict] | None = None,
) -> dict:
    """task_states (detail endpoint only): derived view of the fleet task
    behind each running agent entry (orchestrator.entry_task_states) —
    attached as entry["task_state"], None when absent."""
    states = task_states or {}
    return {
        **challenge.model_dump(),
        "entries": [
            {**_entry_dict(e), "task_state": states.get(e.id)}
            for e in sorted(entries, key=orchestrator.entry_order_key)
        ],
    }


def _entry_dict(entry: BenchEntry) -> dict:
    """An entry as the UI gets it: ``model_label`` as shown — an effort word
    that create appended before round-2 Q2 ("DeepSeek · Off") is cut, the
    level is the pill next to it (orchestrator.display_label)."""
    return {**entry.model_dump(), "model_label": orchestrator.entry_display_label(entry)}


async def _entries_for(session: AsyncSession, challenge_id: uuid.UUID) -> list[BenchEntry]:
    return (
        await session.exec(
            select(BenchEntry).where(BenchEntry.challenge_id == challenge_id)
        )
    ).all()


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.get("/challenges")
async def list_challenges(
    include_archived: bool = False,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    stmt = select(BenchChallenge).order_by(BenchChallenge.created_at.desc())  # type: ignore[attr-defined]
    if not include_archived:
        stmt = stmt.where(BenchChallenge.archived_at == None)  # noqa: E711 — SQLAlchemy IS NULL
    challenges = (await session.exec(stmt)).all()
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
    # Read-only (W0.8): failed/aborted/deleted agent tasks are healed by the
    # vertical's background sweep (orchestrator.reconcile_loop), so the list
    # shows them too and a GET never writes.
    # Why a running agent entry waits (blocked + question, last sign of
    # life) — derived, read-only, never persisted.
    task_states = await orchestrator.entry_task_states(session, entries)
    return {
        **_serialize(challenge, entries, task_states),
        # Extension point (ADR-044): overlay verticals contribute extra
        # operator action buttons for this challenge (e.g. a private
        # catalog_publisher's "Publish"). Detail-only — the list endpoint
        # stays cheap and doesn't run providers for every row.
        "actions": await hooks.collect_challenge_actions(session, challenge, entries),
    }


# Every generated artifact is a single self-contained index.html — inline
# CSS/JS, no external requests (hard requirement baked into the generation
# prompt, orchestrator.GENERATION_SYSTEM_PROMPT / AGENT_BRIEF_TEMPLATE). So
# there is no sibling-asset case to serve, only the one file per entry.
#
# The content is model-generated and therefore untrusted. Serving it as
# text/html on the app's own origin would otherwise let its JS read the
# operator's JWT out of localStorage (same-origin storage is shared by
# scheme+host+port, not by path). Content-Security-Policy: sandbox (without
# allow-same-origin) forces the response into a unique opaque origin even
# when opened as a plain top-level tab — no access to the real origin's
# storage/cookies, no top-level navigation, no popups. connect-src/default-src
# 'none' additionally blocks any network call the artifact might still try
# (defense in depth on top of the "no external requests" generation rule).
_ARTIFACT_CSP = (
    "sandbox allow-scripts; "
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data:; font-src data:; connect-src 'none'; frame-ancestors 'self'"
)


@router.post("/challenges/{challenge_id}/entries/{entry_id}/view-token")
async def mint_bench_entry_view_token(
    challenge_id: uuid.UUID,
    entry_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Mints a short-lived, resource-scoped token for the /view route below —
    only an operator with a full session may mint one. Frontend fetches this
    right before opening the link so the shareable/copyable URL never carries
    the operator's long-lived session JWT (see require_bench_view)."""
    entry = await session.get(BenchEntry, entry_id)
    if entry is None or entry.challenge_id != challenge_id:
        raise HTTPException(404, "Entry not found")
    expires_minutes = 30
    token = create_bench_view_token(
        str(current_user.id), str(challenge_id), str(entry_id), expires_minutes=expires_minutes
    )
    return {"token": token, "expires_in": expires_minutes * 60}


@router.get("/challenges/{challenge_id}/entries/{entry_id}/view", response_class=HTMLResponse)
async def view_bench_entry(
    challenge_id: uuid.UUID,
    entry_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _auth=Depends(require_bench_view),
):
    """Serve a rendered entry's index.html as a real page — interactive,
    openable from any device. Auth accepts a normal operator session OR a
    short-lived view-token scoped to this exact entry (see view-token above
    and require_bench_view) — never a bare session JWT in the URL, since
    this link is meant to be copied/shared/opened on a phone."""
    entry = await session.get(BenchEntry, entry_id)
    if entry is None or entry.challenge_id != challenge_id:
        raise HTTPException(404, "Entry not found")
    if not entry.artifact_path:
        raise HTTPException(404, "No artifact for this entry")

    root = orchestrator.SHARED_DELIVERABLES.resolve()
    target = Path(entry.artifact_path).resolve()
    if target == root or not target.is_relative_to(root):
        raise HTTPException(400, "Artifact path escapes the shared-deliverables root")
    if not target.is_file():
        raise HTTPException(404, "Artifact file not found on disk")

    html = target.read_text(encoding="utf-8", errors="replace")
    return HTMLResponse(
        content=html,
        headers={
            "Content-Security-Policy": _ARTIFACT_CSP,
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
            # The view URL itself carries a resource-scoped token — never let
            # it leak onward as a Referer header to whatever the artifact's
            # (untrusted, model-generated) markup might still try to load.
            "Referrer-Policy": "no-referrer",
        },
    )


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
    await _claim_challenge_run(challenge_id)
    if _is_w1(challenge):
        # W1: record run 1 again (seeded Math.random -> same video) and
        # recompose with the W1 templates — never every repetition.
        create_tracked_task(
            _w1_then_release(challenge.id, "rerender"), name=f"bench_w1_rerender({challenge.id})"
        )
        return {"ok": True}
    create_tracked_task(
        orchestrator.rerender_challenge(challenge.id),
        name=f"rerender_challenge({challenge.id})"
    )
    return {"ok": True}


def _is_w1(challenge: BenchChallenge) -> bool:
    return (challenge.protocol_version or "v0") == "v1"


async def _w1_then_release(challenge_id: uuid.UUID, what: str) -> None:
    from . import w1_runner

    try:
        if what == "rerender":
            await w1_runner.rerender(challenge_id)
        else:
            await w1_runner.recompose(challenge_id)
    finally:
        await orchestrator._release_challenge_run_claim(challenge_id)


# ── Operator lifecycle: stop / archive / delete (2026-07-12) ──────────────

# A challenge counts as "mid-run" in these states — stop first, then delete.
RUNNING_STATUSES = ("generating", "rendering", "composing")
# Only settled challenges may be archived (review is the human gate = settled).
ARCHIVABLE_STATUSES = ("review", "drafted", "published", "failed")


@router.patch("/challenges/{challenge_id}")
async def update_challenge(
    challenge_id: uuid.UUID,
    body: BenchChallengeUpdate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Operator edit (title) after a run — 409 while mid-run. Follow with
    POST .../recompose to rebuild the branded video with the new title."""
    challenge = await session.get(BenchChallenge, challenge_id)
    if challenge is None:
        raise HTTPException(404, "Challenge not found")
    if challenge.status in RUNNING_STATUSES:
        raise HTTPException(
            409, f"Challenge is {challenge.status!r} — edit only when not mid-run."
        )
    if body.title is not None:
        challenge.title = body.title.strip() or challenge.title
        session.add(challenge)
        await session.commit()
    entries = await _entries_for(session, challenge_id)
    return _serialize(challenge, entries)


@router.patch("/entries/{entry_id}")
async def update_entry(
    entry_id: uuid.UUID,
    body: BenchEntryUpdate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Operator edit (model_label / display_tag) after a run — 409 while the
    challenge is mid-run. display_tag="" clears the override (harness default
    applies again)."""
    entry = await session.get(BenchEntry, entry_id)
    if entry is None:
        raise HTTPException(404, "Entry not found")
    challenge = await session.get(BenchChallenge, entry.challenge_id)
    if challenge is not None and challenge.status in RUNNING_STATUSES:
        raise HTTPException(
            409, f"Challenge is {challenge.status!r} — edit only when not mid-run."
        )
    fields = body.model_dump(exclude_unset=True)
    if "model_label" in fields and fields["model_label"]:
        new_label = fields["model_label"].strip() or entry.model_label
        siblings = await _entries_for(session, entry.challenge_id)

        def key(e: BenchEntry, label: str) -> tuple[str, str]:
            return orchestrator.entry_label_key(
                label, e.source_kind, (e.metrics or {}).get("effort_requested")
            )

        # Same rule as create: the label may repeat when the effort differs.
        if any(
            e.id != entry.id and key(e, e.model_label) == key(entry, new_label)
            for e in siblings
        ):
            raise HTTPException(
                409,
                f"Another entry of this challenge is already called {new_label!r} "
                "with the same effort.",
            )
        entry.model_label = new_label
    if "display_tag" in fields:
        entry.display_tag = (fields["display_tag"] or "").strip() or None
    session.add(entry)
    await session.commit()
    await session.refresh(entry)
    return _entry_dict(entry)


@router.post("/challenges/{challenge_id}/recompose")
async def recompose_challenge(
    challenge_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Rebuild ONLY the branded compose from the existing recordings — no
    re-record, much faster than rerender. For challenges with 1 (solo) or 2
    (side-by-side) entries that already have video_path (409/422 otherwise;
    2026-07-13, single-video-branding)."""
    challenge = await session.get(BenchChallenge, challenge_id)
    if challenge is None:
        raise HTTPException(404, "Challenge not found")
    if challenge.status in RUNNING_STATUSES:
        raise HTTPException(
            409, f"Challenge is {challenge.status!r} — wait for the run to settle."
        )
    entries = await _entries_for(session, challenge_id)
    recorded = [e for e in entries if e.video_path]
    if len(recorded) not in (1, 2):
        raise HTTPException(
            422,
            "recompose needs 1 or 2 recorded entries — use rerender for everything else.",
        )
    await _claim_challenge_run(challenge_id)
    if _is_w1(challenge):
        create_tracked_task(
            _w1_then_release(challenge.id, "recompose"), name=f"bench_w1_recompose({challenge.id})"
        )
        return {"ok": True}
    create_tracked_task(
        orchestrator.recompose_challenge(challenge.id),
        name=f"recompose_challenge({challenge.id})",
    )
    return {"ok": True}


@router.post("/challenges/{challenge_id}/stop")
async def stop_challenge(
    challenge_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Operator stop: running entries -> failed ('stopped by operator'),
    challenge -> failed, open fleet tasks stopped via the Tasks-UI stop
    mechanism (run_control='stopped' — no container restarts)."""
    challenge = await session.get(BenchChallenge, challenge_id)
    if challenge is None:
        raise HTTPException(404, "Challenge not found")
    if challenge.status not in RUNNING_STATUSES:
        raise HTTPException(
            409,
            f"Challenge is {challenge.status!r} — stop only while running "
            f"({'/'.join(RUNNING_STATUSES)}).",
        )
    if _is_w1(challenge):
        from . import w1_tests

        await w1_tests.stop_test(session, challenge, str(current_user.id))
    else:
        await orchestrator.stop_challenge(session, challenge, str(current_user.id))
    entries = await _entries_for(session, challenge_id)
    return _serialize(challenge, entries)


@router.post("/challenges/{challenge_id}/archive")
async def archive_challenge(
    challenge_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    challenge = await session.get(BenchChallenge, challenge_id)
    if challenge is None:
        raise HTTPException(404, "Challenge not found")
    if challenge.status not in ARCHIVABLE_STATUSES:
        raise HTTPException(
            409,
            f"Challenge is {challenge.status!r} — archive only from "
            f"{'/'.join(ARCHIVABLE_STATUSES)}.",
        )
    if challenge.archived_at is None:
        from datetime import datetime, timezone

        challenge.archived_at = datetime.now(timezone.utc)
        session.add(challenge)
        await session.commit()
    entries = await _entries_for(session, challenge_id)
    # Archiving ends the challenge for good: an entry task still parked in
    # 'blocked' would keep its approval reminding the operator.
    for entry in entries:
        if entry.source_kind == "agent":
            await orchestrator.retire_blocked_entry_task(
                session, entry.task_id, reason="bench_challenge_archived",
                actor_user_id=current_user.id,
            )
    return _serialize(challenge, entries)


@router.post("/challenges/{challenge_id}/unarchive")
async def unarchive_challenge(
    challenge_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    challenge = await session.get(BenchChallenge, challenge_id)
    if challenge is None:
        raise HTTPException(404, "Challenge not found")
    if challenge.archived_at is not None:
        challenge.archived_at = None
        session.add(challenge)
        await session.commit()
    entries = await _entries_for(session, challenge_id)
    return _serialize(challenge, entries)


@router.delete("/challenges/{challenge_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_challenge(
    challenge_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Hard-delete challenge + entries + the artifact directory
    /shared-deliverables/bench-<id>/ (path-containment guarded). Linked fleet
    tasks stay untouched (audit trail; bench_entries.task_id is SET NULL /
    entries are deleted here anyway). Mid-run challenges must be stopped
    first (409)."""
    challenge = await session.get(BenchChallenge, challenge_id)
    if challenge is None:
        raise HTTPException(404, "Challenge not found")
    if challenge.status in RUNNING_STATUSES:
        raise HTTPException(
            409,
            f"Challenge is {challenge.status!r} — stop it before deleting.",
        )
    # Delete entries explicitly: the DB-level FK cascade
    # (bench_entries.challenge_id ondelete=CASCADE, migration 0154) covers
    # Postgres, but SQLite test runs don't enforce FKs — same rows either way.
    entries = await _entries_for(session, challenge_id)
    for e in entries:
        await session.delete(e)
    await session.delete(challenge)
    await session.commit()
    orchestrator.delete_challenge_artifacts(challenge_id)
    logger.info("bench challenge %s deleted (%d entries)", challenge_id, len(entries))


BENCH_ENTRY_RERENDER_COOLDOWN_S = 60


@router.post("/entries/{entry_id}/rerender")
async def rerender_entry(
    entry_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Per-entry rerender (2026-07-15): re-record just this entry, then
    recompose the challenge — cheaper than the challenge-wide rerender when
    only one model's video looks off. Rate-limited per entry (60s cooldown
    via Redis SET NX EX) so a double-click can't fan out two overlapping
    render+compose runs for the same entry."""
    entry = await session.get(BenchEntry, entry_id)
    if entry is None:
        raise HTTPException(404, "Entry not found")
    if not entry.artifact_path or entry.status not in ("generated", "rendered", "failed"):
        raise HTTPException(
            409,
            f"Entry is {entry.status!r} — rerender only from generated/rendered/failed "
            "with a recorded artifact.",
        )
    challenge = await session.get(BenchChallenge, entry.challenge_id)
    if challenge is not None and challenge.status in RUNNING_STATUSES:
        raise HTTPException(
            409, f"Challenge is {challenge.status!r} — wait for the run to settle."
        )
    if challenge is not None and _is_w1(challenge):
        # W1 records run 1 only — the challenge-wide rerender does exactly that.
        raise HTTPException(409, "W1 test: use the test's rerender (run 1 is the only recording).")

    from app.redis_client import RedisKeys, get_redis

    cooldown_key = RedisKeys.bench_entry_rerender_cooldown(str(entry_id))
    try:
        redis = await get_redis()
        claimed = await redis.set(
            cooldown_key, "1", nx=True, ex=BENCH_ENTRY_RERENDER_COOLDOWN_S
        )
        if not claimed:
            ttl = await redis.ttl(cooldown_key)
            retry_after = ttl if ttl and ttl > 0 else BENCH_ENTRY_RERENDER_COOLDOWN_S
            raise HTTPException(
                429,
                f"Rerender already running for this entry — try again in {retry_after}s.",
                headers={"Retry-After": str(retry_after)},
            )
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 — a Redis outage must never block the endpoint
        logger.warning(
            "bench entry %s rerender: rate-limit check skipped (redis unavailable)", entry_id
        )

    # Challenge-level claim (409 if another run is already in flight for
    # THIS challenge) — the cooldown above only rate-limits repeat clicks on
    # THIS entry, it can't stop two different entries' rerender buttons on
    # the same challenge from racing each other's render+compose.
    await _claim_challenge_run(entry.challenge_id)
    create_tracked_task(
        orchestrator.rerender_entry(entry.id, entry.challenge_id),
        name=f"rerender_entry({entry.id})"
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
    challenge = await session.get(BenchChallenge, entry.challenge_id)
    if challenge is not None and _is_w1(challenge):
        # Attempts of a W1 run are counted by the runner; a new try of the
        # whole plan is "Nochmal" (POST /bench/tests/{id}/rerun).
        raise HTTPException(409, "W1 test: use Nochmal (rerun) — attempts are counted by the runner.")
    create_tracked_task(
        orchestrator.retry_entry(entry.id),
        name=f"retry_entry({entry.id})"
    )
    return {"ok": True}
