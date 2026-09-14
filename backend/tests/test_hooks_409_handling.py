"""409-Handling in the task_review hook registry (PR #478 review, m9).

``run_task_review_hooks()`` used a single broad ``except Exception:
logger.exception(...)`` for every hook failure — including a
``HTTPException(409)`` from a hook that itself calls ``lock_and_set()``
(e.g. ``system_finalize_task_done``) and lost the race. That's expected and
retryable, not a broken hook, but the log line made it indistinguishable
from one. This only changes what gets logged (and at what level) — the
return-value contract ("not handled" on any hook failure, real or a lost
race) is unchanged; see test_bench_review_hook.py's
``test_run_task_review_hooks_swallows_error_task_stays_review`` for that
existing contract with a non-HTTPException failure.

Standalone (no ``pytest.importorskip("app.verticals.bench_studio")``,
unlike test_bench_review_hook.py) — this exercises the hook *registry*,
not any specific vertical's hook.
"""
from __future__ import annotations

import logging
import uuid

import pytest
from fastapi import HTTPException

from app.verticals import hooks


@pytest.mark.asyncio
async def test_409_from_hook_logged_as_info_not_exception(session, make_board, make_task, caplog):
    """A hook that raises HTTPException(409) (lost a lock_and_set() race)
    must be logged at INFO with an explicit 'lost a lock_and_set() race'
    message, not via logger.exception() (which reads as an unhandled bug
    and would be indistinguishable from test_run_task_review_hooks_swallows
    _error_task_stays_review's genuine RuntimeError case)."""
    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    task = await make_task(board.id, title="races on review", status="review")

    async def _raced(_session, _task):
        raise HTTPException(status_code=409, detail={"error": "invalid_transition"})

    hooks.task_review_hooks.append(_raced)
    try:
        with caplog.at_level(logging.INFO, logger="mc.verticals.hooks"):
            handled = await hooks.run_task_review_hooks(session, task)
    finally:
        hooks.task_review_hooks.remove(_raced)

    assert handled is False, "a lost race is 'not handled' -- falls back to normal review flow"

    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any("lock_and_set() race" in r.getMessage() for r in info_records), (
        f"expected an explicit INFO log naming the race, got: "
        f"{[r.getMessage() for r in caplog.records]}"
    )
    exception_records = [r for r in caplog.records if r.exc_info]
    assert exception_records == [], (
        "a lost race must not be logged via logger.exception() -- that's "
        "reserved for genuine hook bugs (non-409 exceptions)"
    )


@pytest.mark.asyncio
async def test_non_409_http_exception_still_logged_as_exception(session, make_board, make_task, caplog):
    """A non-409 HTTPException from a hook (e.g. a genuine 500-ish failure)
    must still go through the loud logger.exception() path, not be
    mistaken for the expected/retryable 409 case."""
    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    task = await make_task(board.id, title="genuinely broken", status="review")

    async def _broken(_session, _task):
        raise HTTPException(status_code=422, detail="not a race")

    hooks.task_review_hooks.append(_broken)
    try:
        with caplog.at_level(logging.INFO, logger="mc.verticals.hooks"):
            handled = await hooks.run_task_review_hooks(session, task)
    finally:
        hooks.task_review_hooks.remove(_broken)

    assert handled is False
    exception_records = [r for r in caplog.records if r.exc_info]
    assert len(exception_records) == 1, (
        f"expected exactly one logger.exception() call, got: "
        f"{[r.getMessage() for r in caplog.records]}"
    )
