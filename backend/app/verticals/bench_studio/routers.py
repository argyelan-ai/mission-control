"""Benchmark Studio router — operator-facing, JWT via require_user
(same auth dependency as the core approvals/files routers). Exception: the
entry HTML view route uses require_bench_view, which additionally accepts a
short-lived resource-scoped view-token (see auth.create_bench_view_token) —
that route's URL is meant to be copied/shared/opened on a phone."""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Literal

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
from .drafts import create_draft

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


class BenchModelSpec(BaseModel):
    # May arrive empty: the dialog's default row "Aktives Modell (auto)"
    # sends "" on purpose and create fills it from the model it resolves.
    # "Not empty" is checked in create_challenge AFTER that resolution — a
    # min_length here refused the default row with a Pydantic 422 before
    # the fill-in could run (round-2 user test t2 MUSS-1).
    label: str = Field(default="", max_length=LABEL_MAX_LEN)
    source_kind: Literal["spark", "agent"]
    spark_model: str | None = None
    agent_id: uuid.UUID | None = None
    # Custom chip tag for the branded video (e.g. "OMP · DGX SPARK").
    # None -> harness-derived default (orchestrator._build_branding_payload).
    display_tag: str | None = Field(default=None, max_length=80)
    # Effort (thinking level) for the direct-API path (bench W0.6). None /
    # "default" = send nothing, the server default governs. Any other level
    # must be one the effort probe found for the served model (422
    # otherwise). Agent rows only take "default": a bench task opens a new
    # omp session that starts on its own default and the chat switch does
    # not reach it — the level is recorded from the session instead (W0.7,
    # operator decision D5).
    effort: Literal["default", "off", "on", "low", "medium", "high", "max"] | None = None


class BenchChallengeCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    prompt_template_id: uuid.UUID | None = None
    prompt_text: str | None = None
    mode: Literal["single", "side_by_side"] = "side_by_side"
    models: list[BenchModelSpec] = Field(min_length=1, max_length=6)
    series_label: str | None = Field(default=None, max_length=80)
    # Video length in seconds. None -> legacy RECORD_DURATION_S fallback
    # (orchestrator.py). 5..60 mirrors the mc-playwright /record bound
    # (RecordRequest.duration_s) plus the timeout budget headroom noted there.
    record_duration_s: int | None = Field(default=None, ge=5, le=60)


class BenchDraftCreate(BaseModel):
    tweet_text: str = Field(min_length=1, max_length=280)
    include_speed_labels: bool = False
    board_id: uuid.UUID | None = None


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


@router.get("/spark-models")
async def list_spark_models(current_user=Depends(require_user)):
    """Bench #21: live model list for the vanilla (direct-API) row in the
    New-Challenge dialog. Always 200 — an unreachable Spark is a state the
    dialog renders (offline warning + free-text fallback), not an error."""
    return await orchestrator.spark_models_status()


async def _served_model_or_none(model: str | None) -> str | None:
    """``model`` or, when empty, the Spark's active model (None when the box
    is unreachable). A /v1/models GET only — never a completion."""
    served = (model or "").strip()
    if served:
        return served
    status_now = await orchestrator.spark_models_status()
    if not status_now["reachable"] or not status_now["active"]:
        return None
    return status_now["active"]


@router.get("/effort-levels")
async def get_effort_levels(
    model: str | None = None,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Bench W0.6: which effort levels the direct-API row may offer for
    ``model`` (empty = the Spark's active model) — from the probe cache
    ONLY (per runtime, served model and serving_since). Read-only: no DB
    write and no completion request to the box (round-1 review: opening the
    dialog used to probe). Never checked for this engine run =
    ``status: "unchecked"``; the dialog then offers an explicit check
    (``POST /effort-levels/check``). Always 200."""
    from . import effort_probe

    served = await _served_model_or_none(model)
    if served is None:
        return effort_probe.serialize(None, None, reachable=False)
    row = await effort_probe.stored_probe(session, served)
    if row is None:
        return effort_probe.serialize(None, served, status="unchecked")
    return effort_probe.serialize(row, served, reachable=effort_probe.reached(row))


class EffortCheckRequest(BaseModel):
    # Empty / None = the Spark's active model.
    model: str | None = Field(default=None, max_length=256)


@router.post("/effort-levels/check")
async def check_effort_levels(
    body: EffortCheckRequest,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Run the effort probe now (2-4 tiny requests against the box) and
    store it — the operator's explicit "check" in the dialog. 409 while the
    Spark is busy (a bench run generating there, or an agent on a Spark
    runtime working on a task). Box unreachable = 200 ``reachable: false``."""
    from . import effort_probe

    reason = await effort_probe.spark_busy_reason(session)
    if reason:
        raise HTTPException(409, reason)
    served = await _served_model_or_none(body.model)
    if served is None:
        return effort_probe.serialize(None, None, reachable=False)
    row = await effort_probe.get_or_probe(session, served, refresh=True)
    return effort_probe.serialize(row, served, reachable=effort_probe.reached(row))


def _unique_labels(specs: list[BenchModelSpec], efforts: list[str]) -> None:
    """Every entry of a challenge needs to be told apart (round-1 user test:
    "DeepSeek" Off vs Max became two identical rows in the UI and video).

    Since round-2 Q2 the effort is its own field and the label is never
    rewritten: two rows of the same model with different efforts are two
    different entries (the UI and the video show the effort next to the
    name). The same label AND the same effort is refused — the box answers
    deterministically, so the second row would repeat the first, and nobody
    could tell the two apart in the video."""
    seen: dict[tuple[str, str], int] = {}
    for row, (spec, effort) in enumerate(zip(specs, efforts), start=1):
        key = orchestrator.entry_label_key(spec.label, spec.source_kind, effort)
        if key in seen:
            raise HTTPException(
                422,
                f"Model row {row} has the same label and the same effort as row "
                f"{seen[key]} ({spec.label.strip()!r}) — choose another effort or "
                "give this row its own label.",
            )
        seen[key] = row


async def _validate_effort(session: AsyncSession, spec: BenchModelSpec) -> str:
    """The effort level to store for ``spec`` — 422 when it cannot run."""
    level = spec.effort or "default"
    if level == "default":
        return level
    if spec.source_kind == "agent":
        raise HTTPException(
            422,
            f"model {spec.label!r}: effort cannot be set on the agent path yet — "
            "the agent's session level is recorded instead",
        )
    from app.services.reasoning_params import UnsupportedEffortError

    from . import effort_probe

    probe = await effort_probe.get_or_probe(session, spec.spark_model or "")
    try:
        effort_probe.level_fields(probe, level)
    except UnsupportedEffortError as exc:
        raise HTTPException(422, f"model {spec.label!r}: {exc}") from exc
    return level


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

    # Bench #21 vanilla "auto" option: empty spark_model -> freeze the live
    # active model now, so the entry/outro never end up pointing at nothing
    # regardless of later model switches on the box (422 if Spark can't be
    # reached to resolve it). Resolved at most ONCE per request — a
    # multi-model side_by_side challenge with several auto specs must not
    # fire N sequential probes against the same box for the same answer
    # (review finding).
    resolved_spark_model: str | None = None
    for row, spec in enumerate(body.models, start=1):
        if spec.source_kind == "agent" and spec.agent_id is None:
            raise HTTPException(400, f"model {spec.label!r}: agent_id required")
        if spec.source_kind == "spark" and not (spec.spark_model or "").strip():
            if resolved_spark_model is None:
                resolved_spark_model = await orchestrator.resolve_spark_model_or_422()
            spec.spark_model = resolved_spark_model
        if not spec.label.strip():
            if spec.source_kind == "spark":
                # The model this row will really run (resolved above for the
                # auto row, typed for a fixed one) — cut to the label limit,
                # the model id itself stays whole.
                spec.label = (spec.spark_model or "").strip()[:LABEL_MAX_LEN].rstrip()
            else:
                raise HTTPException(
                    422,
                    f"Model row {row}: the label is empty — give this agent row a name.",
                )
    efforts = [await _validate_effort(session, spec) for spec in body.models]
    _unique_labels(body.models, efforts)

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
        record_duration_s=body.record_duration_s,
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
            # The requested effort lives in metrics until the W1 schema PR
            # moves it into a column (concept §7: same field names).
            metrics={"effort_requested": effort},
            # The dialog's row order is the order everywhere (t2 SOLL-1).
            position=position,
        )
        for position, (spec, effort) in enumerate(zip(body.models, efforts))
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
    # An open X-Post approval still points at the current video path
    # (Approval.payload.media_paths, frozen at draft time) — rerender would
    # rename AND delete that file (_cleanup_old_compose), breaking the
    # approve-then-post flow days later (2026-07-13 incident).
    if await orchestrator.pending_x_post_approval(session, challenge_id) is not None:
        raise HTTPException(
            409,
            "Open X-Post approval references the current video — approve or reject it first.",
        )
    await _claim_challenge_run(challenge_id)
    create_tracked_task(
        orchestrator.rerender_challenge(challenge.id),
        name=f"rerender_challenge({challenge.id})"
    )
    return {"ok": True}


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
    # Same guard as rerender: a pending X-Post approval's media_paths still
    # point at the current file — recompose would rename AND delete it.
    if await orchestrator.pending_x_post_approval(session, challenge_id) is not None:
        raise HTTPException(
            409,
            "Open X-Post approval references the current video — approve or reject it first.",
        )
    await _claim_challenge_run(challenge_id)
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
    # Same guard as challenge-wide rerender/recompose: a pending X-Post
    # approval's media_paths still point at the current video — this would
    # rename AND delete that file (_cleanup_old_compose).
    if await orchestrator.pending_x_post_approval(session, entry.challenge_id) is not None:
        raise HTTPException(
            409,
            "Open X-Post approval references the current video — approve or reject it first.",
        )

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
    create_tracked_task(
        orchestrator.retry_entry(entry.id),
        name=f"retry_entry({entry.id})"
    )
    return {"ok": True}
