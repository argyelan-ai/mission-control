"""Benchmark Studio orchestrator — the production state machine.

Challenge lifecycle:  generating -> rendering -> composing -> review
                      (-> drafted -> published via drafts.py + core hooks)
Entry lifecycle:      pending -> generating -> generated -> rendered | failed

Design rules (spec §7):
  - Every step writes status + error to the DB — nothing hangs silently.
  - Partial failures never block: the grid is composed from surviving entries.
  - Spark entries generate inline (one Spark GPU — sequential); agent entries
    go through the normal fleet dispatch (auto_dispatch_task) and are
    collected by the task_done hook. NO new dispatch mechanism.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import HTTPException
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.bench import BenchChallenge, BenchEntry

logger = logging.getLogger("mc.bench_studio")

# Same volume + env convention as services/visual_verifier.py and the
# mc-playwright sidecar (docker/mc-playwright/service.py).
SHARED_DELIVERABLES = Path(os.environ.get("SHARED_DELIVERABLES", "/shared-deliverables"))
PLAYWRIGHT_BASE = os.environ.get("MC_PLAYWRIGHT_URL", "http://mc-playwright:8790")

RECORD_DURATION_S = 10       # spec §4: default 10 s
# 2026-07-15: the mc-playwright sidecar's /record now ignores this and
# always captures at a fixed 1440x810 @2x device scale (deterministic
# frame-pipe capture, see docker/mc-playwright/service.py RECORD_VIEWPORT).
# Still sent — RecordRequest.viewport is a required enum field and sending
# it is harmless — but it no longer controls the output resolution.
RECORD_VIEWPORT = "desktop"
# Deterministic capture measured at ~12x video length live (10s video ->
# ~122s capture+encode, docker/mc-playwright's per-frame CDP screenshot +
# JS eval cost dominates, not the encode). 180s was tuned for the old
# real-time record_video path and now clips mid-capture; 900s covers up to
# the max 60s duration_s with headroom.
RECORD_TIMEOUT_S = 900.0
# Compose now overlays/encodes 3840x2160 branded frames + 2K plain-grid
# inputs (2026-07-15 resolution bump) — slower than the old 1920x1080/2K
# path. Bumped again for Bench #18 (configurable up to 60s recordings): the
# sidecar's own branded-compose ffmpeg timeout scales up to 1100s
# (media.compose_branded_timeout_s) for a 60s input, so the client-side
# HTTP timeout must cover that plus request/response overhead.
COMPOSE_TIMEOUT_S = 1200.0
# Reasoning models (Laguna) spend minutes thinking before the HTML comes out
# (~10k tokens at ~25 tok/s) — 300s cut those runs off mid-generation.
SPARK_TIMEOUT_S = 900.0
SPARK_MAX_TOKENS = 16384

GENERATION_SYSTEM_PROMPT = (
    "You are a one-shot frontend generator. Output a single complete "
    "index.html document and nothing else — no explanations, no markdown "
    "prose around it. Inline all CSS and JavaScript. The page must work "
    "offline (no external network requests)."
)

_FENCE_RE = re.compile(r"```(?:html)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _safe_label(label: str) -> str:
    """Filesystem-safe directory name for a model label."""
    return re.sub(r"[^a-zA-Z0-9_-]", "-", label)[:60] or "model"


def challenge_dir(challenge_id: uuid.UUID) -> Path:
    """Artifact root for one challenge: /shared-deliverables/bench-<id>/."""
    return SHARED_DELIVERABLES / f"bench-{challenge_id}"


def _versioned_output_name(prefix: str = "grid") -> str:
    """Cache-busting filename for a compose output: '<prefix>-<8 hex>.mp4'.

    Every compose gets a fresh name so the browser never keeps serving a
    stale cached video for the same challenge after a recompose/rerender —
    the old fixed 'grid.mp4' name was indistinguishable to the browser's
    HTTP cache from run to run (incident 2026-07-13, hit Mark twice)."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}.mp4"


async def pending_x_post_approval(session: AsyncSession, challenge_id: uuid.UUID):
    """The pending x_post Approval referencing this challenge, if any — None
    otherwise. Filter pending x_post approvals in SQL; match
    bench_challenge_id in Python (JSON column, avoids DB-specific operators).

    Shared guard: drafts.create_draft uses it to reject a second draft while
    one is still pending; routers.rerender_challenge/recompose_challenge use
    it (inverted) to block overwriting the video a pending post approval
    still points at — Approval.payload.media_paths freezes a path at draft
    time, and a later recompose/rerender both renames AND deletes the old
    file (_cleanup_old_compose), so approving the post days later would try
    to post a file that no longer exists (2026-07-13 incident)."""
    from app.models.approval import Approval

    pending = (
        await session.exec(
            select(Approval).where(
                Approval.action_type == "x_post",
                Approval.status == "pending",
            )
        )
    ).all()
    for approval in pending:
        payload = approval.payload or {}
        if payload.get("bench_challenge_id") == str(challenge_id):
            return approval
    return None


def _cleanup_old_compose(old_path: str | None, new_path: str | None) -> None:
    """Best-effort removal of a superseded composed video. Versioned
    filenames mean the previous file is otherwise orphaned on disk forever
    after every recompose/rerender."""
    if not old_path or old_path == new_path:
        return
    try:
        old = Path(old_path)
        if old.exists():
            old.unlink()
    except OSError:
        logger.warning("bench compose cleanup: failed to remove %s", old_path)


async def _release_challenge_run_claim(challenge_id: uuid.UUID) -> None:
    """Releases the per-challenge run-claim the router took (SET NX EX,
    routers._claim_challenge_run) before scheduling this background task —
    called from a `finally` in rerender_challenge/recompose_challenge/
    rerender_entry so the claim is freed on every exit path (success,
    handled failure, or a crash the outer except re-raises through).
    Best-effort: a stuck claim self-heals via its TTL, so a Redis outage
    here must never raise out of a background task."""
    try:
        from app.redis_client import RedisKeys, get_redis

        redis = await get_redis()
        await redis.delete(RedisKeys.bench_challenge_run_claim(str(challenge_id)))
    except Exception:  # noqa: BLE001
        logger.warning(
            "bench challenge %s run-claim release skipped (redis unavailable)", challenge_id
        )


def _trim_leading_prose(text: str) -> str:
    """Cut any prose before the first <!doctype or <html tag.

    Finds the earliest occurrence of either marker and trims to it.
    No-ops when both are absent or the earliest is already at position 0.
    """
    lower = text.lower()
    best = len(text)
    for marker in ("<!doctype", "<html"):
        idx = lower.find(marker)
        if idx >= 0:
            best = min(best, idx)
    if 0 < best < len(text):
        return text[best:]
    return text


def extract_html(raw: str | None) -> str:
    """Extract the HTML document from a model response.

    Strips markdown code fences; falls back to cutting leading prose before
    <!doctype / <html; otherwise returns the trimmed raw text.
    """
    text = (raw or "").strip()
    m = _FENCE_RE.search(text)
    if m:
        # Fence found: extract content, then apply prose-trim inside the fence
        # (some models emit prose before the DOCTYPE even inside a fence).
        inner = m.group(1).strip()
        return _trim_leading_prose(inner).strip()
    # No fence: trim leading prose before the first <!doctype or <html tag.
    text = _trim_leading_prose(text)
    return text.strip()


async def _spark_generate(prompt: str, model_override: str | None) -> tuple[str, dict]:
    """One-shot HTML generation against the Spark vLLM endpoint.

    Reuses SparkClient for URL + active-model resolution, but calls
    /chat/completions directly because SparkClient.complete() drops the
    usage block — the studio needs tokens/tok_per_s metrics (spec §3).

    Returns (content, metrics). Raises on HTTP errors — the caller converts
    that into entry.status = "failed".
    """
    from app.services.spark_client import SparkClient

    spark = SparkClient(timeout=SPARK_TIMEOUT_S)
    model = model_override or await spark._resolve_llm_model()
    started = time.monotonic()
    payload: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": GENERATION_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": SPARK_MAX_TOKENS,
        "temperature": 0.7,
    }
    # Qwen3 thinking mode returns content=null; disable it there (same guard
    # as SparkClient.complete). Other models (Laguna/poolside) need thinking
    # for benchmark-grade output — omit the kwarg so the serving default
    # governs and the reasoning parser strips the think block from content.
    if "qwen" in model.lower():
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    async with httpx.AsyncClient(timeout=spark.timeout) as cli:
        resp = await cli.post(f"{spark.llm_url}/chat/completions", json=payload)
        resp.raise_for_status()
    duration_ms = int((time.monotonic() - started) * 1000)
    data = resp.json()
    content = data["choices"][0]["message"]["content"] or ""

    # The actually-served model per the response body wins over the
    # requested name — the resolver can be stale by the time the request
    # lands (recipe swap mid-flight). Falls back to the requested model
    # since some backends echo it verbatim or omit the field.
    metrics: dict = {"duration_ms": duration_ms, "model": data.get("model") or model}
    usage = data.get("usage") or {}
    if usage:
        tokens_out = usage.get("completion_tokens")
        metrics["tokens_in"] = usage.get("prompt_tokens")
        metrics["tokens_out"] = tokens_out
        if tokens_out and duration_ms:
            metrics["tok_per_s"] = round(tokens_out / (duration_ms / 1000), 1)
        # OpenAI-compatible cached-prompt-tokens extension (vLLM prefix
        # caching) — Task 5: billed separately from "fresh" input tokens.
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        if cached:
            metrics["cache_read_tokens"] = cached
    return content, metrics


SPARK_MODELS_PROBE_TIMEOUT_S = 3.0  # Bench #21: the dialog must never hang on an unreachable box.
# Covers the /v1/models GET (bounded by SPARK_MODELS_PROBE_TIMEOUT_S itself)
# PLUS the _resolve_llm_model() leg after it, which can fall through to its
# own live re-probe (runtime_model_resolver._probe_live_spark_model) on a
# DEFAULT — unbounded-by-us — httpx timeout. Wrapping the whole body is the
# only way to guarantee this function itself never outlives ~4s (review
# finding, Bench #21).
_SPARK_MODELS_STATUS_TOTAL_TIMEOUT_S = 4.0


async def _probe_spark_models() -> dict:
    """The actual probe body — see spark_models_status for the timeout
    wrapper and docstring."""
    from app.services.spark_client import SparkClient

    spark = SparkClient(timeout=SPARK_MODELS_PROBE_TIMEOUT_S)
    try:
        async with httpx.AsyncClient(timeout=spark.timeout) as cli:
            resp = await cli.get(f"{spark.llm_url}/models")
            resp.raise_for_status()
            models = sorted({m["id"] for m in resp.json().get("data", [])})
    except (httpx.HTTPError, KeyError, ValueError):
        return {"reachable": False, "models": [], "active": None}
    active = await spark._resolve_llm_model()
    return {"reachable": True, "models": models, "active": active or None}


async def spark_models_status() -> dict:
    """Live probe of the Spark vLLM server for the bench dialog's vanilla
    model picker (Bench #21) — GET /v1/models with a short timeout so the
    dialog never hangs. Reuses SparkClient for URL + active-model
    resolution (same source of truth _spark_generate uses).

    Never raises: an unreachable Spark is a normal, expected state here
    (operator might be mid-benchmark on something else), not a router 500 —
    a timeout on the whole body (see _SPARK_MODELS_STATUS_TOTAL_TIMEOUT_S)
    is treated exactly like an unreachable box.
    """
    try:
        return await asyncio.wait_for(_probe_spark_models(), timeout=_SPARK_MODELS_STATUS_TOTAL_TIMEOUT_S)
    except (TimeoutError, asyncio.TimeoutError):
        return {"reachable": False, "models": [], "active": None}


async def resolve_spark_model_or_422() -> str:
    """Resolves the live active Spark model for a create-time spec with an
    empty/None spark_model (routers.create_challenge, Bench #21 vanilla
    "auto" option) — freezes it into the entry so the outro/label never end
    up empty regardless of later model switches on the box.

    Raises HTTPException(422) when Spark can't be reached right now and
    there is nothing to freeze.
    """
    status = await spark_models_status()
    if not status["reachable"] or not status["active"]:
        raise HTTPException(422, "Spark nicht erreichbar — Modell nicht auflösbar")
    return status["active"]


async def _record_spark_usage_event(session: AsyncSession, entry: BenchEntry, metrics: dict) -> None:
    """Task 5: writes one ModelUsageEvent per spark (vanilla) generation —
    the direct-API path has no fleet Task, so it never reaches
    model_usage_events through the normal harvester (token_harvester.py)
    the way agent entries do (their rows piggyback on the transcript-derived
    harvest). This is the sole feed for vanilla entries.

    task_id is deliberately left NULL: ModelUsageEvent.task_id is a hard FK
    to tasks.id, and spark entries are never dispatched as a Task (unlike
    dispatch_agent_entry) — writing entry.id/challenge_id there would
    violate the constraint in Postgres (SQLite tests wouldn't catch it).
    The outro's token display for spark entries instead reads
    entry.metrics directly (_build_branding_payload) — it doesn't need this
    row at all; this table exists for cost dashboards / budget warnings
    (cost_collector) to see vanilla usage too.

    message_uuid gets a fresh random discriminator every call by design: a
    rerender/regenerate is a genuinely new API call (new tokens spent), so
    it must land as a new row, never dedup onto the previous attempt.

    Best-effort — must never fail the bench run; every error is caught and
    logged. Deliberately does NOT call session.rollback() on failure: this
    session is shared with the caller (generate_spark_entry) and a full
    rollback() expires every object it's tracking (entry, challenge) —
    forcing an implicit re-load on next attribute access, which blows up
    with sqlalchemy's MissingGreenlet outside an awaited context. Only the
    scoped SAVEPOINT (begin_nested) around the actual insert is rolled back
    on failure, same pattern as token_harvester's per-row inserts.
    """
    tokens_in = metrics.get("tokens_in")
    tokens_out = metrics.get("tokens_out")
    if tokens_in is None and tokens_out is None:
        return  # vLLM response had no usage block — nothing to record
    try:
        from app.models.model_usage import ModelPrice, ModelUsageEvent
        from app.services.token_harvester import _compute_cost_usd, match_price

        model = metrics.get("model") or entry.spark_model or "unknown"
        cache_read_tokens = int(metrics.get("cache_read_tokens") or 0)
        input_tokens = max(int(tokens_in or 0) - cache_read_tokens, 0)
        output_tokens = int(tokens_out or 0)
        ts = datetime.now(timezone.utc)

        prices = (await session.exec(select(ModelPrice))).all()
        price_info = match_price(model, ts, prices)
        cost_usd = (
            _compute_cost_usd(price_info, input_tokens, output_tokens, cache_read_tokens, 0)
            if price_info is not None
            else None
        )

        event = ModelUsageEvent(
            agent_id=None,
            task_id=None,
            harness="vanilla",
            model=model,
            provider="vllm",
            session_id=f"bench-{entry.challenge_id}",
            message_uuid=f"vanilla:{entry.challenge_id}:{entry.id}:{uuid.uuid4().hex[:8]}",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=0,
            cost_usd=cost_usd,
            ts=ts,
            source_file=f"bench-entry:{entry.id}",
        )
        async with session.begin_nested():
            session.add(event)
        await session.commit()
    except Exception:  # noqa: BLE001 — usage tracking must never fail the bench run
        logger.warning("bench entry %s: spark usage event write failed", entry.id, exc_info=True)


async def generate_spark_entry(
    session: AsyncSession, entry: BenchEntry, prompt: str
) -> None:
    """Spark path: direct one-shot call, write index.html, capture metrics.

    Never raises — failures land as entry.status='failed' + error text.
    """
    entry.status = "generating"
    session.add(entry)
    await session.commit()
    try:
        content, metrics = await _spark_generate(prompt, entry.spark_model)
        # Tokens were spent the moment the API call returned, independent of
        # whether the HTML below turns out to be valid — record usage now,
        # not after the extract_html/write-to-disk steps that can still fail.
        await _record_spark_usage_event(session, entry, metrics)
        html = extract_html(content)
        if not html:
            raise ValueError("model returned no HTML content")
        out_dir = challenge_dir(entry.challenge_id) / _safe_label(entry.model_label)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "index.html"
        out_path.write_text(html, encoding="utf-8")
        entry.artifact_path = str(out_path)
        entry.metrics = {**(entry.metrics or {}), **metrics}
        entry.status = "generated"
        entry.error = None
    except Exception as exc:  # noqa: BLE001 — every failure must land in the DB
        entry.status = "failed"
        entry.error = f"generation failed: {exc}"[:2000]
        logger.warning("bench entry %s spark generation failed: %s", entry.id, exc)
    session.add(entry)
    await session.commit()


# ── Agent dispatch path ───────────────────────────────────────────────────

AGENT_BRIEF_TEMPLATE = """One-shot benchmark task (Benchmark Studio).

Build EXACTLY ONE self-contained `index.html` for the prompt below, then
register it as a deliverable on this task (deliverable_type "file", path
pointing at the index.html). Hard requirements:
- A single complete HTML document, all CSS/JS inline, no external requests.
- One shot: do NOT ask clarifying questions, do NOT create subtasks.
- After registering the deliverable, set the task status to review.

PROMPT:
{prompt}
"""


async def dispatch_agent_entry(
    session: AsyncSession, entry: BenchEntry, challenge: BenchChallenge
) -> None:
    """Agent path: normal fleet dispatch as a Task with a strict brief.

    Uses the same programmatic creation path as agent delegation
    (routers/agent_scoped.py): persist Task -> asyncio.create_task(
    auto_dispatch_task). The task_done hook collects the artifact later.
    """
    from app.models.agent import Agent
    from app.models.task import Task
    from app.services.dispatch import auto_dispatch_task

    agent = await session.get(Agent, entry.agent_id) if entry.agent_id else None
    if agent is None or agent.board_id is None:
        entry.status = "failed"
        entry.error = "agent missing or not assigned to a board"
        session.add(entry)
        await session.commit()
        return

    task = Task(
        id=uuid.uuid4(),
        board_id=agent.board_id,
        title=f"[Bench] {challenge.title} — {entry.model_label}"[:200],
        description=AGENT_BRIEF_TEMPLATE.format(prompt=challenge.prompt_text),
        status="inbox",
        priority="medium",
        assigned_agent_id=agent.id,
        is_auto_created=True,
        auto_reason=f"bench_studio challenge {challenge.id}",
        # Operator decision 2026-07-12 (agent/Lead reviewer) + 2026-07-15
        # (Mark's manual approve, too): bench results are judged by the
        # human in the Bench Studio UI (challenge status 'review' + video) —
        # never by an agent reviewer / the board lead (burns frontier tokens
        # for zero benefit), and not via a second manual `mc approve` gate
        # either. human_review_required=True still matters: it routes the
        # task to handle_human_review_handoff instead of dispatching a Rex-
        # style agent reviewer. The bench_studio task_review_hook
        # (on_task_review, registered below) intercepts there and finalizes
        # review -> done immediately, so this never actually waits for Mark.
        human_review_required=True,
    )
    session.add(task)
    # Flush the Task INSERT before linking it: there is no ORM relationship
    # between Task and BenchEntry, so the unit of work has no dependency edge
    # and may emit the bench_entries UPDATE before the tasks INSERT — a
    # ForeignKeyViolation on Postgres (invisible in SQLite tests, no FK
    # enforcement there).
    await session.flush()
    entry.task_id = task.id
    entry.status = "generating"
    session.add(entry)
    await session.commit()
    asyncio.create_task(auto_dispatch_task(task.id, task.board_id))


async def task_work_duration_ms(session: AsyncSession, task_id: uuid.UUID) -> int | None:
    """Real WORK time of a bench agent task, derived from task_events.

    task.completed_at - task.dispatched_at is wrong for bench entries: the
    span includes the human review wait, and review handoffs / re-dispatches
    reset dispatched_at (task_lifecycle._handle_review et al.) — so the value
    is often inflated or missing entirely (verified on the live
    cherry-blossom/horror-forest runs, 2026-07-12).

    Correct span: first `* -> in_progress` event (work start) to the FIRST
    `in_progress -> review` event by the working agent (work end — the
    one-shot artifact exists at that moment; everything after is review
    ping-pong). Returns None when either endpoint is missing.
    """
    from app.models.task import TaskEvent

    events = (
        await session.exec(
            select(TaskEvent)
            .where(TaskEvent.task_id == task_id)
            .order_by(TaskEvent.created_at)  # type: ignore[arg-type]
        )
    ).all()
    start = next((e for e in events if e.to_status == "in_progress"), None)
    end = next(
        (
            e
            for e in events
            if e.from_status == "in_progress"
            and e.to_status == "review"
            and e.changed_by == "agent"
        ),
        None,
    )
    if start is None or end is None or end.created_at <= start.created_at:
        return None
    return int((end.created_at - start.created_at).total_seconds() * 1000)


async def task_cost_usd(session: AsyncSession, task_id: uuid.UUID) -> float | None:
    """Attributed LLM cost of a task: sum of model_usage_events.cost_usd.

    Coverage (2026-07-12 audit of the live data): model_usage_events has
    per-task attribution columns and 118k+ priced rows, but only host-harness
    rows carry task_id today (token_harvester's workspace/branch heuristic);
    cli-bridge and sparky harvests are unattributed, and the Grok TUI reports
    no token usage at all (known gap). So this returns None for most agent
    entries until harvester attribution improves — the outro then shows "—".
    """
    from sqlalchemy import func

    from app.models.model_usage import ModelUsageEvent

    total = (
        await session.exec(
            select(func.sum(ModelUsageEvent.cost_usd)).where(
                ModelUsageEvent.task_id == task_id
            )
        )
    ).one()
    return float(total) if total is not None else None


async def task_token_usage(
    session: AsyncSession, task_id: uuid.UUID
) -> tuple[int, int] | None:
    """Attributed token usage of a task: (sum input_tokens, sum output_tokens)
    from model_usage_events. Same coverage caveat as task_cost_usd — most
    agent entries have no attributed rows today, so this returns None and
    the outro shows "—" (see task_cost_usd docstring for the harvester gap).
    """
    from sqlalchemy import func

    from app.models.model_usage import ModelUsageEvent

    row = (
        await session.exec(
            select(
                func.sum(ModelUsageEvent.input_tokens),
                func.sum(ModelUsageEvent.output_tokens),
            ).where(ModelUsageEvent.task_id == task_id)
        )
    ).one()
    input_sum, output_sum = row
    if input_sum is None and output_sum is None:
        return None
    return (int(input_sum or 0), int(output_sum or 0))


async def on_task_review(session: AsyncSession, task) -> bool:
    """task_review_hook (core registry): bench agent tasks skip human review
    entirely — review -> done fires immediately once the one-shot lands, so
    on_task_done (artifact collection below) runs right away instead of
    waiting on `mc approve`. The human still judges the result, just in the
    Bench Studio UI (challenge status 'review' + rendered video), not via
    the generic Task review gate. Self-filters — False (no-op) for tasks
    without a bench entry, so the normal human-review flow still applies to
    every other task on the board.

    Operator decision 2026-07-15 — supersedes the 2026-07-12 decision (see
    dispatch_agent_entry below) that only ruled out an agent/Lead reviewer;
    it did not yet rule out Mark's manual approve step, which this closes.
    """
    if task.status != "review":
        # Idempotency guard (2026-07-15 review): the registry can fire this
        # hook more than once for the same task (e.g. a retry after a
        # transient error elsewhere in the caller's chain) — re-finalizing
        # an already-done task would overwrite completed_at/review_decision
        # with fresh values and re-run every side effect a second time.
        return False

    result = await session.exec(select(BenchEntry).where(BenchEntry.task_id == task.id))
    if result.first() is None:
        return False

    from app.services.task_lifecycle import system_finalize_task_done

    await system_finalize_task_done(
        session, task, task.board_id,
        old_status="review",
        reason="bench_studio_auto_finalize",
    )
    return True


async def on_task_done(session: AsyncSession, task) -> None:
    """task_done_hook (core registry, ADR-044): collect the index.html
    deliverable of a bench agent task. Self-filters — silent no-op for tasks
    without a bench entry."""
    from app.models.deliverable import TaskDeliverable

    result = await session.exec(select(BenchEntry).where(BenchEntry.task_id == task.id))
    entry = result.first()
    if entry is None or entry.status not in ("pending", "generating"):
        return

    rows = (
        await session.exec(
            select(TaskDeliverable).where(TaskDeliverable.task_id == task.id)
        )
    ).all()
    # Resolve through fs_service: deliverable paths are stored in the AGENT's
    # view (e.g. /deliverables/<task>/... which is ~/.mc/deliverables/<slug>/...
    # on the host → /deliverables/<slug>/... in the backend container). A naive
    # Path(d.path).exists() misses the slug segment and always fails for
    # docker-agent deliverables (2026-07-12 incident).
    from app.services.fs_service import resolve_deliverable

    html_src: Path | None = None
    for d in rows:
        if d.path and d.path.endswith(".html"):
            resolved = await resolve_deliverable(d, session, target="container")
            # Resolver returns None for unknown prefixes — fall back to the raw
            # path for already-backend-local paths (sidecar dirs, tests).
            for candidate in filter(None, (resolved, d.path)):
                if Path(candidate).exists():
                    html_src = Path(candidate)
                    break
            if html_src:
                break

    if html_src is None:
        entry.status = "failed"
        entry.error = "task done, but no index.html deliverable found (or path not readable)"
    else:
        out_dir = challenge_dir(entry.challenge_id) / _safe_label(entry.model_label)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "index.html"
        shutil.copyfile(html_src, out_path)
        entry.artifact_path = str(out_path)
        metrics = dict(entry.metrics or {})
        # Real work time from task_events (see task_work_duration_ms — the
        # completed_at - dispatched_at span includes review wait and gets
        # reset by review handoffs). Fallback: old timestamp diff.
        duration_ms = await task_work_duration_ms(session, task.id)
        if duration_ms is None and task.completed_at and task.dispatched_at:
            duration_ms = int(
                (task.completed_at - task.dispatched_at).total_seconds() * 1000
            )
        if duration_ms is not None:
            metrics["duration_ms"] = duration_ms
        entry.metrics = metrics
        entry.status = "generated"
        entry.error = None
    session.add(entry)
    await session.commit()
    await maybe_advance(session, entry.challenge_id)


# ── Render + Compose ─────────────────────────────────────────────────────


async def record_entry(entry: BenchEntry, challenge: BenchChallenge) -> dict:
    """POST /record on mc-playwright (PR 1) for one entry. Raises on failure.

    Uses the challenge's operator-chosen record_duration_s (Bench #18) when
    set, else the legacy RECORD_DURATION_S default — same fallback the
    NewChallengeDialog documents (None -> 10s)."""
    out_dir = str(challenge_dir(entry.challenge_id) / _safe_label(entry.model_label))
    duration_s = (
        challenge.record_duration_s
        if challenge.record_duration_s is not None
        else RECORD_DURATION_S
    )
    async with httpx.AsyncClient(timeout=RECORD_TIMEOUT_S) as cli:
        resp = await cli.post(
            f"{PLAYWRIGHT_BASE}/record",
            json={
                "html_path": entry.artifact_path,
                "duration_s": duration_s,
                "viewport": RECORD_VIEWPORT,
                "output_dir": out_dir,
            },
        )
        resp.raise_for_status()
        return resp.json()


def format_speed_label(metrics: dict) -> str:
    """'42 s · 87 tok/s' — optional per-model overlay for the grid (spec §4)."""
    parts: list[str] = []
    duration_ms = metrics.get("duration_ms")
    if duration_ms:
        parts.append(f"{duration_ms / 1000:.0f} s")
    tok_per_s = metrics.get("tok_per_s")
    if tok_per_s:
        parts.append(f"{tok_per_s:.0f} tok/s")
    return " · ".join(parts)


def _first_prompt_line(prompt_text: str, max_len: int = 110) -> str:
    """First non-empty line of the prompt, truncated for the frame's
    single-line PROMPT footer (spec: video-branding, 2026-07-12)."""
    for line in (prompt_text or "").splitlines():
        line = line.strip()
        if line:
            return line[:max_len]
    return ""


def _format_outro_time(duration_ms: int | None) -> str:
    if not duration_ms:
        return "—"
    return f"{duration_ms / 60000:.1f} min"


def _format_outro_cost(cost_usd: float | None, source_kind: str) -> str:
    """Outro cost cell. Spark entries run on the local DGX — no per-token
    cost, shown as "local" (clearer than a fake $0). Agent entries show the
    attributed sum when model_usage_events has one, else "—" (most agent
    runs today — see task_cost_usd coverage note)."""
    if source_kind == "spark":
        return "local"
    if cost_usd is None:
        return "—"
    return f"${cost_usd:.2f}"


def _format_tok_count(n: int) -> str:
    """Compact human-readable token count: exact below 1000, k/M above
    (1 decimal), matching the outro table's terse style."""
    if n < 1_000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1_000:.1f}k"
    return f"{n / 1_000_000:.1f}M"


def _format_outro_tokens(usage: tuple[int, int] | None) -> str:
    """'12.4k → 1.8k' (input -> output), or "—" when no model_usage_events
    are attributed to the task (same coverage gap as cost — see
    task_token_usage)."""
    if usage is None:
        return "—"
    input_tokens, output_tokens = usage
    return f"{_format_tok_count(input_tokens)} → {_format_tok_count(output_tokens)}"


def _format_outro_size(artifact_path: str | None) -> str:
    if not artifact_path:
        return "—"
    try:
        size_kb = Path(artifact_path).stat().st_size / 1024
        return f"{size_kb:.0f} KB"
    except OSError:
        return "—"


async def _build_branding_payload(
    session: AsyncSession, challenge: BenchChallenge, ordered: list[BenchEntry]
) -> dict:
    """Video-branding payload (spec: bench-video-branding, 2026-07-12; single-
    video-branding 2026-07-13) for the mc-playwright /compose branded path —
    fills the argyelan frame + outro templates. Called for 1 entry (single
    mode, or a side_by_side run degraded to 1 survivor) or 2 entries
    (side_by_side). `models`/`outro_rows` are built generically off `ordered`
    so the length just flows through — mc-playwright picks frame.html vs
    frame_single.html based on len(models)."""
    from app.models.agent import Agent

    # run_label: zero-padded 3-digit count of bench_challenges created
    # at-or-before this one — a stable per-series run number.
    prior_ids = (
        await session.exec(
            select(BenchChallenge.id).where(
                BenchChallenge.created_at <= challenge.created_at
            )
        )
    ).all()
    run_label = f"{len(prior_ids):03d}"

    models: list[dict] = []
    outro_rows: list[dict] = []
    for entry in ordered:
        if entry.display_tag:
            # Operator override (bench_entries.display_tag) always wins.
            tag = entry.display_tag
        elif entry.source_kind == "spark":
            tag = "VLLM · SPARK"
        else:
            # Agent entries: harness-derived default (e.g. omp -> "OMP",
            # grok -> "GROK"); agent name as fallback when no harness is set.
            tag = "AGENT"
            if entry.agent_id is not None:
                agent = await session.get(Agent, entry.agent_id)
                if agent is not None:
                    if agent.harness:
                        tag = agent.harness.upper()
                    elif agent.name:
                        tag = agent.name.upper()
        models.append({"label": entry.model_label, "tag": tag})

        # Time: stored metrics first; for agent entries without a stored
        # value (old runs, hook races) derive defensively from task_events.
        duration_ms = (entry.metrics or {}).get("duration_ms")
        if duration_ms is None and entry.task_id is not None:
            duration_ms = await task_work_duration_ms(session, entry.task_id)

        cost_usd = None
        if entry.source_kind != "spark" and entry.task_id is not None:
            cost_usd = await task_cost_usd(session, entry.task_id)

        # Token usage: spark entries have no fleet Task (task_id is always
        # NULL for them, see _record_spark_usage_event), so they can never
        # be attributed via task_token_usage/model_usage_events — read the
        # tokens_in/out _spark_generate already captured into entry.metrics
        # directly instead (Task 5). Agent entries keep the existing
        # task-attributed sum.
        token_usage = None
        if entry.source_kind == "spark":
            tokens_in = (entry.metrics or {}).get("tokens_in")
            tokens_out = (entry.metrics or {}).get("tokens_out")
            if tokens_in is not None and tokens_out is not None:
                token_usage = (int(tokens_in), int(tokens_out))
        elif entry.task_id is not None:
            token_usage = await task_token_usage(session, entry.task_id)

        outro_rows.append({
            "name": entry.model_label,
            "time": _format_outro_time(duration_ms),
            "size": _format_outro_size(entry.artifact_path),
            "cost": _format_outro_cost(cost_usd, entry.source_kind),
            "tokens": _format_outro_tokens(token_usage),
        })

    return {
        "title": challenge.title,
        "run_label": run_label,
        "prompt_line": _first_prompt_line(challenge.prompt_text),
        "models": models,
        "outro_rows": outro_rows,
    }


async def compose_challenge(
    session: AsyncSession,
    challenge: BenchChallenge,
    rendered: list[BenchEntry],
    *,
    speed_labels: bool = False,
    output_name: str | None = None,
) -> str:
    """POST /compose on mc-playwright (PR 1) — grid video with model labels,
    or (1 or 2 rendered entries) the branded frame + outro video (spec:
    bench-video-branding, 2026-07-12; single-video-branding 2026-07-13).
    Returns the composed video_path. Raises on failure.

    output_name defaults to a fresh versioned filename (_versioned_output_name)
    so every compose produces a distinct file — callers that need a stable
    name (drafts.py's "grid-speeds.mp4") pass it explicitly."""
    if output_name is None:
        output_name = _versioned_output_name()
    ordered = sorted(rendered, key=lambda e: e.model_label)
    duration_s = (
        challenge.record_duration_s
        if challenge.record_duration_s is not None
        else RECORD_DURATION_S
    )
    payload: dict = {
        "inputs": [e.video_path for e in ordered],
        "labels": [e.model_label for e in ordered],
        "layout": "grid",
        "output_path": str(challenge_dir(challenge.id) / output_name),
        # Bench #18: scales the sidecar's branded-compose ffmpeg timeout
        # (media.compose_branded_timeout_s) — a no-op for the plain grid
        # path, which doesn't read this field.
        "duration_s": duration_s,
    }
    if speed_labels:
        payload["speed_labels"] = [format_speed_label(e.metrics or {}) for e in ordered]
    # speed_labels re-compose (drafts.py "grid-speeds.mp4" for X posts with
    # per-model metric overlays) stays on the plain grid path — branding is
    # only for the primary review composition, not the metrics variant.
    # 1 or 2 entries -> branded (solo/side-by-side); 3-4 entries (side_by_side
    # models list beyond the branded pair) fall through to the plain grid
    # path unchanged (pre-existing behaviour, regression-safe).
    if not speed_labels and len(ordered) in (1, 2):
        payload["branding"] = await _build_branding_payload(session, challenge, ordered)
    async with httpx.AsyncClient(timeout=COMPOSE_TIMEOUT_S) as cli:
        resp = await cli.post(f"{PLAYWRIGHT_BASE}/compose", json=payload)
        resp.raise_for_status()
        # ComposeResponse (service.py) names the field output_path — NOT
        # video_path like /record's RecordResponse (KeyError incident
        # 2026-07-12, first live side-by-side compose).
        return resp.json()["output_path"]


async def _render_and_compose(
    session: AsyncSession, challenge: BenchChallenge, generated: list[BenchEntry]
) -> None:
    """rendering -> composing -> review. Partial failures shrink the grid."""
    challenge.status = "rendering"
    challenge.error = None
    session.add(challenge)
    await session.commit()

    rendered: list[BenchEntry] = []
    for entry in generated:
        try:
            result = await record_entry(entry, challenge)
            entry.video_path = result.get("video_path")
            entry.screenshot_path = result.get("screenshot_path")
            entry.status = "rendered"
            entry.error = None
            rendered.append(entry)
        except Exception as exc:  # noqa: BLE001
            entry.status = "failed"
            entry.error = f"render failed: {exc}"[:2000]
            logger.warning("bench entry %s render failed: %s", entry.id, exc)
        session.add(entry)
        await session.commit()

    if not rendered:
        challenge.status = "failed"
        challenge.error = "all entries failed during rendering"
        session.add(challenge)
        await session.commit()
        return

    # side_by_side always composes (2 rendered -> branded pair, degraded to
    # 1 survivor -> branded solo instead of the raw recording); single mode
    # composes its 1 entry into the branded solo frame (2026-07-13,
    # single-video-branding — previously single mode never composed and
    # shipped the raw, unbranded recording).
    should_compose = (
        (challenge.mode == "side_by_side" and len(rendered) >= 1)
        or (challenge.mode == "single" and len(rendered) == 1)
    )
    if should_compose:
        challenge.status = "composing"
        session.add(challenge)
        await session.commit()
        try:
            challenge.composed_video_path = await compose_challenge(session, challenge, rendered)
        except Exception as exc:  # noqa: BLE001
            challenge.status = "failed"
            challenge.error = f"compose failed: {exc}"[:2000]
            session.add(challenge)
            await session.commit()
            return

    challenge.status = "review"
    session.add(challenge)
    await session.commit()


async def maybe_advance(session: AsyncSession, challenge_id: uuid.UUID) -> None:
    """Advance once every entry finished generating.

    generating -> (rendering -> composing ->) review, or -> failed when every
    entry failed. Idempotent: only fires while the challenge is 'generating'.
    """
    challenge = await session.get(BenchChallenge, challenge_id)
    if challenge is None or challenge.status != "generating":
        return
    entries = (
        await session.exec(
            select(BenchEntry).where(BenchEntry.challenge_id == challenge_id)
        )
    ).all()
    if not entries or any(e.status in ("pending", "generating") for e in entries):
        return

    generated = [e for e in entries if e.status == "generated"]
    if not generated:
        challenge.status = "failed"
        challenge.error = "all entries failed during generation"
        session.add(challenge)
        await session.commit()
        return

    await _render_and_compose(session, challenge, generated)


async def reconcile_challenge(
    session: AsyncSession, challenge: BenchChallenge, entries: list[BenchEntry]
) -> None:
    """GET-time fallback sweep: agent tasks that ended in `failed` never fire
    the task_done hook — mark their entries failed so nothing hangs silently
    (spec §7, lesson from the Grok review: uncaught -> eternal in_progress).

    Also self-heals entries whose task was deleted (FK SET NULL on
    tasks.id): task_id=None means the task_done hook can never fire for that
    entry again, so it would otherwise hang at `generating` forever."""
    from app.models.task import Task

    changed = False
    for entry in entries:
        if entry.status != "generating" or entry.source_kind != "agent":
            continue
        if entry.task_id is None:
            entry.status = "failed"
            entry.error = "agent task deleted"
            session.add(entry)
            changed = True
            continue
        task = await session.get(Task, entry.task_id)
        if task is not None and task.status == "failed":
            entry.status = "failed"
            entry.error = "agent task failed"
            session.add(entry)
            changed = True
    if changed:
        await session.commit()
        await maybe_advance(session, challenge.id)


# ── Operator lifecycle: stop + artifact cleanup (2026-07-12) ───────────────

STOPPED_BY_OPERATOR = "stopped by operator"


async def stop_challenge(
    session: AsyncSession, challenge: BenchChallenge, user_id: str
) -> None:
    """Operator stop for a running challenge.

    Non-terminal entries (pending/generating) -> failed with a stop marker;
    rendered/generated entries keep their state. The challenge itself goes to
    `failed` (deliberate reuse: every existing gate — entry retry, rerender,
    drafts — already treats `failed` correctly; a new "stopped" status would
    have to be threaded through all of them for zero benefit).

    Open fleet tasks of stopped agent entries are stopped through the same
    mechanism as the Tasks-UI stop button (services.operations.stop_task_run:
    run_control="stopped", poll.sh sees state="stopped" and ends the session
    cleanly — no container restarts). Tasks without an active run are
    skipped silently (best effort, audit trail untouched).
    """
    from app.services.operations import stop_task_run

    entries = (
        await session.exec(
            select(BenchEntry).where(BenchEntry.challenge_id == challenge.id)
        )
    ).all()

    stop_task_ids: list[uuid.UUID] = []
    for entry in entries:
        if entry.status in ("pending", "generating"):
            if (
                entry.source_kind == "agent"
                and entry.task_id is not None
                and entry.status == "generating"
            ):
                stop_task_ids.append(entry.task_id)
            entry.status = "failed"
            entry.error = STOPPED_BY_OPERATOR
            session.add(entry)

    challenge.status = "failed"
    challenge.error = STOPPED_BY_OPERATOR
    session.add(challenge)
    await session.commit()

    for task_id in stop_task_ids:
        try:
            await stop_task_run(
                session, task_id, user_id, reason="bench challenge stopped"
            )
            await session.commit()
        except HTTPException as exc:
            # 409 = no active run (already done/failed) — nothing to stop.
            logger.info("bench stop: task %s not stopped (%s)", task_id, exc.detail)
        except Exception:  # noqa: BLE001 — stop must never fail the endpoint
            logger.exception("bench stop: task %s stop failed", task_id)
            await session.rollback()


def delete_challenge_artifacts(challenge_id: uuid.UUID) -> None:
    """Removes /shared-deliverables/bench-<id>/ with path-containment guard
    (same style as the sidecar's _require_shared_path): the resolved target
    must live strictly below the shared-deliverables root — never delete
    outside it, never delete the root itself."""
    root = SHARED_DELIVERABLES.resolve()
    target = challenge_dir(challenge_id).resolve()
    if target == root or not target.is_relative_to(root):
        logger.warning("bench delete: refusing artifact cleanup outside root: %s", target)
        return
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)


# ── Background entrypoints (own session — called via asyncio.create_task) ─


async def start_challenge(challenge_id: uuid.UUID) -> None:
    """Background fan-out. Spark entries generate inline (sequential — one
    Spark GPU); agent entries dispatch fleet tasks. Never raises."""
    from app.database import engine

    async with AsyncSession(engine, expire_on_commit=False) as session:
        try:
            challenge = await session.get(BenchChallenge, challenge_id)
            if challenge is None:
                return
            entries = (
                await session.exec(
                    select(BenchEntry).where(BenchEntry.challenge_id == challenge_id)
                )
            ).all()
            for entry in entries:
                if entry.status != "pending":
                    continue
                if entry.source_kind == "spark":
                    await generate_spark_entry(session, entry, challenge.prompt_text)
                elif entry.source_kind == "agent":
                    await dispatch_agent_entry(session, entry, challenge)
                else:
                    entry.status = "failed"
                    entry.error = f"unknown source_kind {entry.source_kind!r}"
                    session.add(entry)
                    await session.commit()
            await maybe_advance(session, challenge_id)
        except Exception:  # noqa: BLE001 — spec §7: nothing hangs silently
            logger.exception("bench challenge %s fan-out crashed", challenge_id)
            try:
                # A failed flush leaves the session in pending-rollback state —
                # without this the failure write below raises PendingRollbackError
                # and the challenge hangs in 'generating' forever.
                await session.rollback()
                challenge = await session.get(BenchChallenge, challenge_id)
                if challenge is not None and challenge.status == "generating":
                    challenge.status = "failed"
                    challenge.error = "fan-out crashed — see backend logs"
                    session.add(challenge)
                    await session.commit()
            except Exception:
                logger.exception("bench challenge %s failure write failed", challenge_id)


async def rerender_challenge(challenge_id: uuid.UUID) -> None:
    """Background: re-run render + compose from the existing artifacts."""
    from app.database import engine

    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            try:
                challenge = await session.get(BenchChallenge, challenge_id)
                if challenge is None:
                    return
                entries = (
                    await session.exec(
                        select(BenchEntry).where(BenchEntry.challenge_id == challenge_id)
                    )
                ).all()
                candidates = [
                    e
                    for e in entries
                    if e.artifact_path and e.status in ("generated", "rendered", "failed")
                ]
                if not candidates:
                    challenge.status = "failed"
                    challenge.error = "nothing to render — no entry has an artifact"
                    session.add(challenge)
                    await session.commit()
                    return
                old_video = challenge.composed_video_path
                for e in candidates:
                    e.status = "generated"
                    e.error = None
                    session.add(e)
                challenge.composed_video_path = None
                session.add(challenge)
                await session.commit()
                await _render_and_compose(session, challenge, candidates)
                _cleanup_old_compose(old_video, challenge.composed_video_path)
            except Exception:  # noqa: BLE001
                logger.exception("bench challenge %s rerender crashed", challenge_id)
                try:
                    await session.rollback()  # clear pending-rollback state (see start_challenge)
                    challenge = await session.get(BenchChallenge, challenge_id)
                    if challenge is not None:
                        challenge.status = "failed"
                        challenge.error = "rerender crashed — see backend logs"
                        session.add(challenge)
                        await session.commit()
                except Exception:
                    logger.exception("bench challenge %s rerender failure write failed", challenge_id)
    finally:
        await _release_challenge_run_claim(challenge_id)


async def recompose_challenge(challenge_id: uuid.UUID) -> None:
    """Background: rebuild ONLY the branded compose from the existing
    recordings (no re-record — much faster than rerender). Used after
    title/label/tag edits: the recordings are untouched, only the branded
    frame/outro overlays change. Works for 1 recorded entry (solo frame) or
    2 (side-by-side frame) — any other count needs rerender instead
    (2026-07-13, single-video-branding)."""
    from app.database import engine

    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            try:
                challenge = await session.get(BenchChallenge, challenge_id)
                if challenge is None:
                    return
                entries = (
                    await session.exec(
                        select(BenchEntry).where(BenchEntry.challenge_id == challenge_id)
                    )
                ).all()
                candidates = [e for e in entries if e.video_path]
                if len(candidates) not in (1, 2):
                    challenge.status = "failed"
                    challenge.error = (
                        "recompose needs 1 or 2 recorded entries — use rerender instead"
                    )
                    session.add(challenge)
                    await session.commit()
                    return
                old_video = challenge.composed_video_path
                challenge.status = "composing"
                challenge.error = None
                session.add(challenge)
                await session.commit()
                challenge.composed_video_path = await compose_challenge(
                    session, challenge, candidates
                )
                challenge.status = "review"
                session.add(challenge)
                await session.commit()
                _cleanup_old_compose(old_video, challenge.composed_video_path)
            except Exception:  # noqa: BLE001
                logger.exception("bench challenge %s recompose crashed", challenge_id)
                try:
                    await session.rollback()
                    challenge = await session.get(BenchChallenge, challenge_id)
                    if challenge is not None:
                        challenge.status = "failed"
                        challenge.error = "recompose crashed — see backend logs"
                        session.add(challenge)
                        await session.commit()
                except Exception:
                    logger.exception("bench challenge %s recompose failure write failed", challenge_id)
    finally:
        await _release_challenge_run_claim(challenge_id)


async def rerender_entry(entry_id: uuid.UUID, challenge_id: uuid.UUID) -> None:
    """Background: re-record ONLY this entry from its existing artifact, then
    recompose the whole challenge from every entry that still has a video
    (2026-07-15, per-entry rerender). Cheaper than rerender_challenge when a
    single model's recording looks off — other entries' recordings are
    untouched. Reuses compose_challenge (no copy) like rerender_challenge/
    recompose_challenge above.

    Takes challenge_id explicitly (the router already knows it — it's what
    the run-claim was taken on) rather than deriving it from the entry row:
    deriving it requires a DB read that can fail (entry deleted between the
    router's claim and this task's first read), which used to skip the
    claim-release finally below and leak the claim for its full 30-minute
    TTL (2026-07-15 review fix, verify-work finding)."""
    from app.database import engine

    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            try:
                entry = await session.get(BenchEntry, entry_id)
                if entry is None:
                    return
                challenge = await session.get(BenchChallenge, entry.challenge_id)
                if challenge is None:
                    return

                old_video = challenge.composed_video_path
                challenge.status = "rendering"
                challenge.error = None
                session.add(challenge)
                entry.status = "generated"
                entry.error = None
                session.add(entry)
                await session.commit()

                try:
                    result = await record_entry(entry, challenge)
                    entry.video_path = result.get("video_path")
                    entry.screenshot_path = result.get("screenshot_path")
                    entry.status = "rendered"
                    entry.error = None
                except Exception as exc:  # noqa: BLE001
                    entry.status = "failed"
                    entry.error = f"render failed: {exc}"[:2000]
                    logger.warning("bench entry %s rerender failed: %s", entry.id, exc)
                session.add(entry)
                await session.commit()

                entries = (
                    await session.exec(
                        select(BenchEntry).where(BenchEntry.challenge_id == challenge.id)
                    )
                ).all()
                candidates = [e for e in entries if e.video_path]
                if not candidates:
                    challenge.status = "failed"
                    challenge.error = "entry rerender failed and no other recording survives"
                    session.add(challenge)
                    await session.commit()
                    return

                challenge.status = "composing"
                session.add(challenge)
                await session.commit()
                try:
                    challenge.composed_video_path = await compose_challenge(
                        session, challenge, candidates
                    )
                except Exception as exc:  # noqa: BLE001
                    challenge.status = "failed"
                    challenge.error = f"compose failed: {exc}"[:2000]
                    session.add(challenge)
                    await session.commit()
                    return

                challenge.status = "review"
                session.add(challenge)
                await session.commit()
                _cleanup_old_compose(old_video, challenge.composed_video_path)
            except Exception:  # noqa: BLE001
                logger.exception("bench entry %s rerender crashed", entry_id)
                try:
                    await session.rollback()  # clear pending-rollback state (see start_challenge)
                    ent = await session.get(BenchEntry, entry_id)
                    if ent is not None:
                        ch = await session.get(BenchChallenge, ent.challenge_id)
                        if ch is not None:
                            ch.status = "failed"
                            ch.error = "entry rerender crashed — see backend logs"
                            session.add(ch)
                            await session.commit()
                except Exception:
                    logger.exception("bench entry %s rerender failure write failed", entry_id)
    finally:
        await _release_challenge_run_claim(challenge_id)


async def retry_entry(entry_id: uuid.UUID) -> None:
    """Background: retry a single failed entry from scratch, then re-advance."""
    from app.database import engine

    async with AsyncSession(engine, expire_on_commit=False) as session:
        try:
            entry = await session.get(BenchEntry, entry_id)
            if entry is None:
                return
            challenge = await session.get(BenchChallenge, entry.challenge_id)
            if challenge is None:
                return
            entry.status = "pending"
            entry.error = None
            entry.video_path = None
            entry.screenshot_path = None
            session.add(entry)
            if challenge.status in ("review", "failed", "rendering", "composing"):
                challenge.status = "generating"
                challenge.error = None
                challenge.composed_video_path = None
                session.add(challenge)
            await session.commit()
            if entry.source_kind == "spark":
                await generate_spark_entry(session, entry, challenge.prompt_text)
            else:
                await dispatch_agent_entry(session, entry, challenge)
            await maybe_advance(session, entry.challenge_id)
        except Exception:  # noqa: BLE001
            logger.exception("bench entry %s retry crashed", entry_id)
