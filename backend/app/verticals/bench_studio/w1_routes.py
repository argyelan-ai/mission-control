"""Bench W1 routes — CONTRACT STUBS (24.10.2026).

Every route here returns the contract fixture (``w1_fixtures/*.json``)
validated through ``w1_schemas`` and sets the ``X-Bench-Contract-Stub``
header. Package A replaces each stub body with the real implementation and
removes its ``_stub(...)`` call; the integration gate requires
``grep -c "_stub(response" w1_routes.py`` == 0, no ``STUB_HEADER`` use left
and no 501 stub route.

Paths, methods, status codes, query parameters and response models are the
contract — they do not change without the lead (CONTRACT.md).
"""
from __future__ import annotations

import json
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.auth import require_user

from . import w1_schemas as s

router = APIRouter(prefix="/api/v1/bench", tags=["bench-studio-w1"])

STUB_HEADER = "X-Bench-Contract-Stub"
_FIXTURES = Path(__file__).resolve().parent / "w1_fixtures"


@lru_cache(maxsize=None)
def _fixture(name: str):
    return json.loads((_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _stub(response: Response, name: str):
    """Fixture data + the stub marker header (removed by package A)."""
    response.headers[STUB_HEADER] = "1"
    return _fixture(name)


_DETAIL_BY_ID = {
    "a0000000-0000-4000-8000-000000000001": "test_running",
    "a0000000-0000-4000-8000-000000000002": "test_done_green",
    "a0000000-0000-4000-8000-000000000003": "test_done_truncated",
    "a0000000-0000-4000-8000-000000000004": "test_needs_look_red",
}


def _detail_fixture(test_id: uuid.UUID) -> str:
    name = _DETAIL_BY_ID.get(str(test_id))
    if name is None:
        raise HTTPException(404, "Test not found")
    return name


# ── Plan + contestants (P3 sheet) ─────────────────────────────────────────


@router.get("/plan", response_model=s.BenchPlan)
async def get_plan(
    response: Response,
    runtime_id: uuid.UUID | None = None,
    preset: s.PresetId = "standard",
    template_id: uuid.UUID | None = None,
    rerun_of: uuid.UUID | None = None,
    current_user=Depends(require_user),
):
    """Everything the test sheet shows, pre-filled from live runtimes,
    recipes, probes, harness bindings, prices and history. Without
    ``runtime_id`` the subject is the most recently started live runtime.
    ``rerun_of`` = "Nochmal": the same plan against the model live NOW.
    Read-only: never probes, never sends a completion."""
    return _stub(response, "plan_standard")


@router.get("/contestants", response_model=s.ContestantList)
async def list_contestants(response: Response, current_user=Depends(require_user)):
    """All testable models: local (one row per engine) + cloud. Used by
    "+ Gegner hinzufügen" and "Cloud-Modelle ›"."""
    return _stub(response, "contestants")


# ── Tests (create, list, detail, actions) ─────────────────────────────────


@router.post(
    "/tests",
    response_model=s.BenchTestDetail,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"description": "detail = CostConfirmation (money must be confirmed)"}},
)
async def create_test(
    body: s.BenchTestCreate, response: Response, current_user=Depends(require_user)
):
    """Start a test from the sheet. Picks are re-resolved server-side
    against the live runtimes (a stale sheet never freezes a wrong model).
    A paid run without a matching ``cost_confirmed_usd`` → 409 with a
    ``CostConfirmation`` detail (step 1; step 2 over $1)."""
    return _stub(response, "test_running")


@router.get("/tests", response_model=s.BenchTestList)
async def list_tests(
    response: Response,
    section: s.Section | None = None,
    model: str | None = Query(default=None, max_length=256),
    device: str | None = Query(default=None, max_length=128),
    harness: s.Harness | None = None,
    run_class: s.RunClass | None = None,
    include_archived: bool = False,
    current_user=Depends(require_user),
):
    """The list (P7) with human states — orphans healed by the background
    sweep, never by this GET (read-only)."""
    return _stub(response, "test_list")


@router.get("/tests/{test_id}", response_model=s.BenchTestDetail)
async def get_test(test_id: uuid.UUID, response: Response, current_user=Depends(require_user)):
    """Result page + live run (poll every 2 s while waiting/running)."""
    return _stub(response, _detail_fixture(test_id))


@router.post(
    "/tests/{test_id}/rerun",
    response_model=s.BenchTestDetail,
    status_code=status.HTTP_201_CREATED,
)
async def rerun_test(
    test_id: uuid.UUID,
    body: s.RerunRequest,
    response: Response,
    current_user=Depends(require_user),
):
    """"Nochmal" (budget same) or "re-run with a larger budget" from the
    private truncation warning (budget larger → suggested_max_tokens)."""
    _detail_fixture(test_id)
    return _stub(response, "test_running")


@router.post("/tests/{test_id}/stop", response_model=s.BenchTestDetail)
async def stop_test(test_id: uuid.UUID, response: Response, current_user=Depends(require_user)):
    return _stub(response, _detail_fixture(test_id))


@router.post("/tests/{test_id}/harvest", response_model=s.BenchTestDetail)
async def harvest_test(test_id: uuid.UUID, response: Response, current_user=Depends(require_user)):
    """"Zahlen nachladen": harvest usage again, write a new receipt version,
    recompose — the fix button next to a red R1 reason."""
    return _stub(response, _detail_fixture(test_id))


# ── X-Kit (P6) ────────────────────────────────────────────────────────────


@router.get("/tests/{test_id}/kit", response_model=s.XKit)
async def get_kit(test_id: uuid.UUID, response: Response, current_user=Depends(require_user)):
    name = _detail_fixture(test_id)
    return _stub(response, "kit_red" if name == "test_needs_look_red" else "kit_green")


@router.post("/tests/{test_id}/kit/download-token", response_model=s.DownloadToken)
async def kit_download_token(
    test_id: uuid.UUID, response: Response, current_user=Depends(require_user)
):
    """Short-lived token for the download links (an <a href> cannot send
    the Bearer header; never the session JWT in a URL)."""
    _detail_fixture(test_id)
    response.headers[STUB_HEADER] = "1"
    return {"token": "stub-token", "expires_in": 1800}


@router.get("/tests/{test_id}/kit/files/{name}")
async def kit_file(
    test_id: uuid.UUID,
    name: Literal["video.mp4", "card.png", "kit.zip"],
    token: str = Query(min_length=1),
):
    """The artefact itself with ``Content-Disposition: attachment;
    filename="<model>-<prompt>-<date>.<ext>"`` (iOS "Save" path). Auth =
    the download token (resource-scoped, like the entry view token).
    Red gate → 409. Stub: 501 until package A builds it."""
    raise HTTPException(501, "bench W1 contract stub: kit files are built by package A")


# ── Entries: Runtimes cards + auto test after a recipe switch ─────────────


@router.get("/runtime-summary", response_model=list[s.RuntimeTestSummary])
async def runtime_summary(response: Response, current_user=Depends(require_user)):
    return _stub(response, "runtime_summary")


@router.post(
    "/auto-tests",
    response_model=s.AutoTestResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_auto_test(
    body: s.AutoTestCreate, response: Response, current_user=Depends(require_user)
):
    """Called by the recipe switch dialog after a successful switch with
    "Danach automatisch testen" on. Creates a Standard test in state
    ``waiting`` (model_loading → model_fresh) that the vertical's background
    loop starts ≥ 15 min after serving_since on a quiet box; a later switch
    of the same host supersedes it (aborted: superseded_by_switch)."""
    return _stub(response, "auto_test")
