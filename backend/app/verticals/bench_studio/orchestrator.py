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
RECORD_VIEWPORT = "desktop"  # 1440x900 (mc-playwright VIEWPORTS)
RECORD_TIMEOUT_S = 180.0
COMPOSE_TIMEOUT_S = 300.0
SPARK_TIMEOUT_S = 300.0
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
    async with httpx.AsyncClient(timeout=spark.timeout) as cli:
        resp = await cli.post(
            f"{spark.llm_url}/chat/completions",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": GENERATION_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": SPARK_MAX_TOKENS,
                "temperature": 0.7,
                # Qwen3 thinking mode returns content=null; disable it
                # (same guard as SparkClient.complete).
                "chat_template_kwargs": {"enable_thinking": False},
            },
        )
        resp.raise_for_status()
    duration_ms = int((time.monotonic() - started) * 1000)
    data = resp.json()
    content = data["choices"][0]["message"]["content"] or ""

    metrics: dict = {"duration_ms": duration_ms}
    usage = data.get("usage") or {}
    if usage:
        tokens_out = usage.get("completion_tokens")
        metrics["tokens_in"] = usage.get("prompt_tokens")
        metrics["tokens_out"] = tokens_out
        if tokens_out and duration_ms:
            metrics["tok_per_s"] = round(tokens_out / (duration_ms / 1000), 1)
    return content, metrics


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
        # Operator decision 2026-07-12: bench results are judged by the human
        # (the artifact IS the review), never by an agent reviewer / the board
        # lead — a lead review burns frontier tokens for zero benefit.
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
        if task.completed_at and task.dispatched_at:
            metrics["duration_ms"] = int(
                (task.completed_at - task.dispatched_at).total_seconds() * 1000
            )
        entry.metrics = metrics
        entry.status = "generated"
        entry.error = None
    session.add(entry)
    await session.commit()
    await maybe_advance(session, entry.challenge_id)


# ── Render + Compose ─────────────────────────────────────────────────────


async def record_entry(entry: BenchEntry) -> dict:
    """POST /record on mc-playwright (PR 1) for one entry. Raises on failure."""
    out_dir = str(challenge_dir(entry.challenge_id) / _safe_label(entry.model_label))
    async with httpx.AsyncClient(timeout=RECORD_TIMEOUT_S) as cli:
        resp = await cli.post(
            f"{PLAYWRIGHT_BASE}/record",
            json={
                "html_path": entry.artifact_path,
                "duration_s": RECORD_DURATION_S,
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


def _format_outro_time(metrics: dict) -> str:
    duration_ms = (metrics or {}).get("duration_ms")
    if not duration_ms:
        return "—"
    return f"{duration_ms / 60000:.1f} min"


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
    """Video-branding payload (spec: bench-video-branding, 2026-07-12) for the
    mc-playwright /compose branded path — fills the argyelan frame + outro
    templates. Only ever called for exactly 2 side_by_side entries."""
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
        outro_rows.append({
            "name": entry.model_label,
            "time": _format_outro_time(entry.metrics or {}),
            "size": _format_outro_size(entry.artifact_path),
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
    output_name: str = "grid.mp4",
) -> str:
    """POST /compose on mc-playwright (PR 1) — grid video with model labels,
    or (side_by_side with exactly 2 rendered entries) the branded frame +
    outro video (spec: bench-video-branding, 2026-07-12). Returns the
    composed video_path. Raises on failure."""
    ordered = sorted(rendered, key=lambda e: e.model_label)
    payload: dict = {
        "inputs": [e.video_path for e in ordered],
        "labels": [e.model_label for e in ordered],
        "layout": "grid",
        "output_path": str(challenge_dir(challenge.id) / output_name),
    }
    if speed_labels:
        payload["speed_labels"] = [format_speed_label(e.metrics or {}) for e in ordered]
    # speed_labels re-compose (drafts.py "grid-speeds.mp4" for X posts with
    # per-model metric overlays) stays on the plain grid path — branding is
    # only for the primary review composition, not the metrics variant.
    if not speed_labels and challenge.mode == "side_by_side" and len(ordered) == 2:
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
            result = await record_entry(entry)
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

    if challenge.mode == "side_by_side" and len(rendered) >= 2:
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
    (spec §7, lesson from the Grok review: uncaught -> eternal in_progress)."""
    from app.models.task import Task

    changed = False
    for entry in entries:
        if (
            entry.status == "generating"
            and entry.source_kind == "agent"
            and entry.task_id is not None
        ):
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
            for e in candidates:
                e.status = "generated"
                e.error = None
                session.add(e)
            challenge.composed_video_path = None
            session.add(challenge)
            await session.commit()
            await _render_and_compose(session, challenge, candidates)
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
