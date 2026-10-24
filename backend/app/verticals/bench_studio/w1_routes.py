"""Bench W1 routes ("Testen in 2 Taps", contract 24.10.2026).

Paths, methods, status codes, query parameters and response models are the
contract (CONTRACT.md §2) — they do not change without the lead. The bodies
are thin: plan builder (``w1_plan``), tests (``w1_tests``), runner
(``w1_runner``), receipts + gate (``w1_receipt``), X-Kit (``w1_kit``).
"""
from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import FileResponse
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import require_user
from app.database import get_session
from app.utils import create_tracked_task

from . import w1_kit, w1_plan, w1_runner, w1_tests
from . import w1_schemas as s

router = APIRouter(prefix="/api/v1/bench", tags=["bench-studio-w1"])


# ── Plan + contestants (P3 sheet) ─────────────────────────────────────────


@router.get("/plan", response_model=s.BenchPlan)
async def get_plan(
    response: Response,
    runtime_id: uuid.UUID | None = None,
    preset: s.PresetId = "standard",
    template_id: uuid.UUID | None = None,
    rerun_of: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Everything the test sheet shows, pre-filled from live runtimes,
    recipes, probes, harness bindings, prices and history. Without
    ``runtime_id`` the subject is the most recently started live runtime.
    ``rerun_of`` = "Nochmal": the same plan against the model live NOW.
    Read-only: never probes, never sends a completion."""
    return await w1_plan.build_plan(
        session, runtime_id=runtime_id, preset=preset, template_id=template_id, rerun_of=rerun_of
    )


@router.get("/contestants", response_model=s.ContestantList)
async def list_contestants(
    response: Response,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """All testable models: local (one row per engine) + cloud. Used by
    "+ Gegner hinzufügen" and "Cloud-Modelle ›"."""
    return await w1_plan.list_contestants(session)


# ── Tests (create, list, detail, actions) ─────────────────────────────────


@router.post(
    "/tests",
    response_model=s.BenchTestDetail,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"description": "detail = CostConfirmation (money must be confirmed)"}},
)
async def create_test(
    body: s.BenchTestCreate,
    response: Response,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Start a test from the sheet. Picks are re-resolved server-side
    against the live runtimes (a stale sheet never freezes a wrong model).
    A paid run without a matching ``cost_confirmed_usd`` → 409 with a
    ``CostConfirmation`` detail (step 1; step 2 over $1)."""
    picks = [p.model_dump() for p in body.picks]
    if body.rerun_of is not None:
        old = await w1_tests.get_or_404(session, body.rerun_of)
        challenge = await w1_tests.rerun_test(
            session, old, budget=body.budget, user_id=str(current_user.id), picks=picks,
            cost_confirmed_usd=body.cost_confirmed_usd, confirm_over_cap=body.confirm_over_cap,
        )
    else:
        challenge = await w1_tests.create_test(
            session, picks=picks, preset=body.preset, trigger=body.trigger, user_id=str(current_user.id),
            template_id=body.prompt_template_id, budget=body.budget,
            cost_confirmed_usd=body.cost_confirmed_usd, confirm_over_cap=body.confirm_over_cap,
        )
    return await w1_tests.test_detail(session, challenge)


@router.get("/tests", response_model=s.BenchTestList)
async def list_tests(
    response: Response,
    section: s.Section | None = None,
    model: str | None = Query(default=None, max_length=256),
    device: str | None = Query(default=None, max_length=128),
    harness: s.Harness | None = None,
    run_class: s.RunClass | None = None,
    include_archived: bool = False,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """The list (P7) with human states — orphans healed by the background
    sweep, never by this GET (read-only)."""
    return await w1_tests.list_tests(
        session, section=section, model=model, device=device, harness=harness,
        run_class=run_class, include_archived=include_archived,
    )


@router.get("/tests/{test_id}", response_model=s.BenchTestDetail)
async def get_test(
    test_id: uuid.UUID,
    response: Response,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Result page + live run (poll every 2 s while waiting/running)."""
    challenge = await w1_tests.get_or_404(session, test_id)
    return await w1_tests.test_detail(session, challenge)


@router.post(
    "/tests/{test_id}/rerun",
    response_model=s.BenchTestDetail,
    status_code=status.HTTP_201_CREATED,
)
async def rerun_test(
    test_id: uuid.UUID,
    body: s.RerunRequest,
    response: Response,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """"Nochmal" (budget same) or "re-run with a larger budget" from the
    private truncation warning (budget larger → suggested_max_tokens).
    A paid test answers 409 CostConfirmation (D4: step 1, over the cap
    step 2); the page confirms with ``cost_confirmed_usd`` /
    ``confirm_over_cap`` on this same route, keeping the budget."""
    old = await w1_tests.get_or_404(session, test_id)
    challenge = await w1_tests.rerun_test(
        session, old, budget=body.budget, user_id=str(current_user.id),
        cost_confirmed_usd=body.cost_confirmed_usd, confirm_over_cap=body.confirm_over_cap,
    )
    return await w1_tests.test_detail(session, challenge)


@router.post("/tests/{test_id}/stop", response_model=s.BenchTestDetail)
async def stop_test(
    test_id: uuid.UUID,
    response: Response,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    challenge = await w1_tests.get_or_404(session, test_id)
    await w1_tests.stop_test(session, challenge, str(current_user.id))
    await session.refresh(challenge)
    return await w1_tests.test_detail(session, challenge)


@router.post("/tests/{test_id}/harvest", response_model=s.BenchTestDetail)
async def harvest_test(
    test_id: uuid.UUID,
    response: Response,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """"Zahlen nachladen": harvest usage again, write a new receipt version,
    recompose — the fix button next to a red R1 reason."""
    challenge = await w1_tests.get_or_404(session, test_id)
    if challenge.status in w1_tests.LIVE:
        raise HTTPException(409, "Test is still running — numbers are loaded when it ends.")
    written = await w1_runner.harvest(session, challenge)
    await session.refresh(challenge)
    if written and w1_tests.is_v1(challenge) and challenge.status in w1_tests.DONE:
        create_tracked_task(w1_runner.recompose(challenge.id), name=f"bench_w1_recompose({challenge.id})")
    return await w1_tests.test_detail(session, challenge)


# ── X-Kit (P6) ────────────────────────────────────────────────────────────


@router.get("/tests/{test_id}/kit", response_model=s.XKit)
async def get_kit(
    test_id: uuid.UUID,
    response: Response,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """English texts + file states; red = preview only (``downloadable``
    false, ``blocked_reason``). The private truncation warning is repeated
    so the sheet can stop the operator before "Alles laden"."""
    challenge = await w1_tests.get_or_404(session, test_id)
    detail = await w1_tests.test_detail(session, challenge, with_actions=False)
    return w1_kit.kit(challenge, detail)


@router.post("/tests/{test_id}/kit/download-token", response_model=s.DownloadToken)
async def kit_download_token(
    test_id: uuid.UUID,
    response: Response,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Short-lived token for the download links (an <a href> cannot send
    the Bearer header; never the session JWT in a URL)."""
    await w1_tests.get_or_404(session, test_id)
    token = w1_kit.create_download_token(str(current_user.id), str(test_id))
    return {"token": token, "expires_in": w1_kit.KIT_TOKEN_MINUTES * 60}


_MEDIA = {"video.mp4": "video/mp4", "card.png": "image/png", "kit.zip": "application/zip"}


@router.get("/tests/{test_id}/kit/files/{name}")
async def kit_file(
    test_id: uuid.UUID,
    name: Literal["video.mp4", "card.png", "kit.zip"],
    token: str = Query(min_length=1),
    session: AsyncSession = Depends(get_session),
):
    """The artefact itself with ``Content-Disposition: attachment;
    filename="<model>-<prompt>-<date>.<ext>"`` (iOS "Save" path). Auth =
    the download token (resource-scoped, like the entry view token).
    Red gate → 409."""
    if not w1_kit.verify_download_token(token, str(test_id)):
        raise HTTPException(403, "Download link expired or not for this test")
    challenge = await w1_tests.get_or_404(session, test_id)
    detail = await w1_tests.test_detail(session, challenge, with_actions=False)
    if detail["gate"]["status"] == "red":
        raise HTTPException(409, {"code": "gate_red", "reasons": detail["gate"]["reasons"]})
    if detail["gate"]["status"] == "pending":
        raise HTTPException(409, {"code": "runs_pending"})
    if name == "kit.zip":
        if not any(f["ready"] for f in w1_kit.kit_files(challenge, detail)):
            raise HTTPException(404, "Kit files are not ready yet")
        path = w1_kit.build_zip(challenge, detail)
        filename = w1_kit.download_filename(detail, "zip")
    else:
        path = w1_kit.artefact_paths(challenge, detail)[name]
        if path is None:
            raise HTTPException(404, f"{name} is not ready yet")
        filename = w1_kit.download_filename(detail, name.rsplit(".", 1)[1])
    return FileResponse(
        path,
        media_type=_MEDIA[name],
        filename=filename,
        content_disposition_type="attachment",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                 "Referrer-Policy": "no-referrer"},
    )


# ── Entries: Runtimes cards + auto test after a recipe switch ─────────────


@router.get("/runtime-summary", response_model=list[s.RuntimeTestSummary])
async def runtime_summary(
    response: Response,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """One row per enabled runtime for the Runtimes cards (P1): testable,
    the one primary "Testen" (most recently started live card, K11), the
    last test and a running one."""
    return await w1_plan.runtime_summary(session)


@router.post(
    "/auto-tests",
    response_model=s.AutoTestResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_auto_test(
    body: s.AutoTestCreate,
    response: Response,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Called by the recipe switch dialog after a successful switch with
    "Danach automatisch testen" on. Creates a Standard test in state
    ``waiting`` (model_loading → model_fresh) that the vertical's background
    loop starts ≥ 15 min after serving_since on a quiet box; a later switch
    of the same host supersedes it (aborted: superseded_by_switch)."""
    return await w1_tests.create_auto_test(
        session, host_id=body.host_id, recipe_slug=body.recipe_slug, user_id=str(current_user.id)
    )
