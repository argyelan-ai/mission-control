"""Bench W1 runner — one test at a time, class A before class B.

The vertical's background loop (``hooks.background_loops``) starts waiting
tests one after another: never two tests at once, and inside a test every
class A (model pure) run before any class B (agent) run, so an agent never
shares the box with a speed measurement ("2 Motoren parallel = Ringkampf").

Class A — the direct runner for ANY OpenAI-compatible runtime: streaming
with ``include_usage`` gives TTFT, decode tok/s (sanity: decode >= the wall
average tokens/gen_ms, else not a decode number), ``finish_reason`` and
reasoning tokens. n runs per contestant, run 1 (fixed before the start) is
the only one recorded as video (W1.9 retention). Before the first local run
the quiet-box guard waits for serving_since + 15 min, an idle engine and no
fleet usage in the last 2 min — at most 10 min for "busy", then it runs and
the gate goes amber.

Class B — the existing fleet dispatch (omp / Grok Build), one run with a
time limit; effort is recorded, not set (D5).

At the end: write-once receipts, the gate, the 16:9 video with chips and the
receipt card, the 1080×1350 card PNG (one template), an Eingang item
"Test fertig" (``Approval(action_type="bench_result")``).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import httpx
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.bench import BenchChallenge, BenchEntry

from . import orchestrator as orch
from . import w1_common as c
from . import w1_kit, w1_plan, w1_receipt, w1_tests

logger = logging.getLogger("mc.bench_studio")

#: Tests put an ``httpx.MockTransport`` here (engines + renderer).
_transport: httpx.AsyncBaseTransport | None = None
#: The stopwatch of the streaming runner (tests swap in a fake clock).
_clock = time.monotonic

LOOP_INITIAL_DELAY_S = 20
LOOP_INTERVAL_S = 5
QUIET_POLL_S = 15
AGENT_POLL_S = 10
LIVE_WRITE_EVERY_S = 2.0
MAX_ATTEMPTS = 2
STREAM_READ_TIMEOUT_S = 300.0
PROBE_RETRY_AFTER = timedelta(minutes=30)
FEATURE_CARD = "bench_card_v1"

_wake_event: asyncio.Event | None = None
_tasks: dict[uuid.UUID, asyncio.Task] = {}
_probe_failures: dict[str, datetime] = {}

SessionFactory = Callable[[], AsyncSession]


def _default_factory() -> AsyncSession:
    from app.database import engine

    return AsyncSession(engine, expire_on_commit=False)


@asynccontextmanager
async def _session(factory: SessionFactory | None) -> AsyncIterator[AsyncSession]:
    session = (factory or _default_factory)()
    try:
        yield session
    finally:
        await session.close()


def wake() -> None:
    if _wake_event is not None:
        _wake_event.set()


def cancel(test_id: uuid.UUID) -> None:
    task = _tasks.pop(test_id, None)
    if task is not None and not task.done():
        task.cancel()


def running() -> set[uuid.UUID]:
    return {tid for tid, t in _tasks.items() if not t.done()}


class RunError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# ── Streaming chat completion ────────────────────────────────────────────


async def stream_chat(
    url: str,
    body: dict[str, Any],
    *,
    api_key: str | None,
    on_progress: Callable[[dict[str, Any]], Any] | None = None,
    read_timeout: float = STREAM_READ_TIMEOUT_S,
) -> tuple[str, dict[str, Any]]:
    """POST a streaming chat completion; returns (content, metrics).

    gen_ms = request -> last token; ttft_ms = request -> first token
    (thinking counts as a token: it is generated output); decode =
    (completion_tokens − 1) / (t_last − t_first). An engine that ignores
    ``stream`` answers with plain JSON: tokens then come from its usage,
    TTFT/decode are not measured (never guessed)."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    timeout = httpx.Timeout(connect=15.0, read=read_timeout, write=30.0, pool=15.0)
    t_start = _clock()
    t_first = t_last = None
    content: list[str] = []
    reasoning: list[str] = []
    usage: dict[str, Any] = {}
    finish_reason = None
    model = None
    chunks = 0
    last_progress = 0.0
    async with httpx.AsyncClient(timeout=timeout, transport=_transport, headers=headers) as cli:
        try:
            async with cli.stream("POST", url, json=body) as resp:
                if resp.status_code != 200:
                    text = (await resp.aread()).decode(errors="replace")[:300]
                    raise RunError("http_error", f"HTTP {resp.status_code}: {text}")
                ctype = resp.headers.get("content-type", "")
                if "event-stream" not in ctype:
                    data = json.loads(await resp.aread())
                    t_last = _clock()
                    choice = (data.get("choices") or [{}])[0]
                    msg = choice.get("message") or {}
                    content.append(msg.get("content") or "")
                    reasoning.append((msg.get("reasoning_content") or "") + (msg.get("reasoning") or ""))
                    usage = data.get("usage") or {}
                    finish_reason = choice.get("finish_reason")
                    model = data.get("model")
                    streamed = False
                else:
                    streamed = True
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            data = json.loads(payload)
                        except ValueError:
                            continue
                        model = data.get("model") or model
                        if data.get("usage"):
                            usage = data["usage"]
                        for choice in data.get("choices") or []:
                            delta = choice.get("delta") or {}
                            piece = delta.get("content") or ""
                            think = (delta.get("reasoning_content") or "") + (delta.get("reasoning") or "")
                            if piece or think:
                                now = _clock()
                                if t_first is None:
                                    t_first = now
                                t_last = now
                                chunks += 1
                                content.append(piece)
                                reasoning.append(think)
                            if choice.get("finish_reason"):
                                finish_reason = choice["finish_reason"]
                        if on_progress is not None and _clock() - last_progress >= LIVE_WRITE_EVERY_S:
                            last_progress = _clock()
                            elapsed = (last_progress - t_start) * 1000
                            live = {"elapsed_ms": int(elapsed), "tokens_so_far": chunks}
                            if t_first is not None and last_progress > t_first and chunks > 1:
                                live["live_decode_tok_s"] = round((chunks - 1) / (last_progress - t_first), 1)
                            await on_progress(live)
        except httpx.TimeoutException as exc:
            raise RunError("timeout", str(exc)) from exc
        except httpx.HTTPError as exc:
            raise RunError("unreachable", str(exc)) from exc
    text = "".join(content)
    from app.services import reasoning_params

    m: dict[str, Any] = {"model": model, "finish_reason": finish_reason, "truncated": finish_reason == "length",
                         "streamed": streamed}
    end = t_last if t_last is not None else _clock()
    m["gen_ms"] = int((end - t_start) * 1000)
    out = usage.get("completion_tokens")
    if usage:
        m["tokens_in"] = usage.get("prompt_tokens")
        m["tokens_out"] = out
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        if cached:
            m["tokens_cache_read"] = cached
    else:
        m["usage_missing"] = True
    obs = reasoning_params.reasoning_of(
        {"content": text, "reasoning_content": "".join(reasoning)}, usage or None
    )
    m["effort_observed"] = obs
    if obs.get("reasoning_tokens") is not None:
        m["tokens_reasoning"] = obs["reasoning_tokens"]
    if streamed and t_first is not None:
        m["ttft_ms"] = int((t_first - t_start) * 1000)
        if out and out > 1 and t_last is not None and t_last > t_first:
            decode = (out - 1) / (t_last - t_first)
            wall = out / max(1e-6, (end - t_start))
            span_share = (t_last - t_first) / max(1e-6, (end - t_start))
            m["decode_tok_s"] = round(decode, 1)
            m["wall_tok_s"] = round(wall, 1)
            # Sanity (concept §5.3): decode can never be below the wall
            # average; a buffered stream (all chunks at the end) is no
            # decode measurement either.
            m["decode_sane"] = decode >= wall and (chunks >= 2 and span_share >= 0.1)
    return text, m


# ── Direct class A run ───────────────────────────────────────────────────


async def _runtime_access(session: AsyncSession, pc: dict[str, Any]):
    """(runtime row, chat URL, API key) of a contestant, as it runs NOW."""
    from app.models.runtime import Runtime
    from app.services.harness_compat import resolve_provider_credentials

    rt = await session.get(Runtime, uuid.UUID(pc["runtime_id"]))
    if rt is None:
        raise RunError("runtime_gone")
    creds = await resolve_provider_credentials(session, None, rt)
    return rt, f"{rt.endpoint.rstrip('/')}/chat/completions", creds.get("OPENAI_API_KEY")


async def _effort_fields(session: AsyncSession, pc: dict, rt, api_key: str | None) -> tuple[dict, dict]:
    """Request fields for the contestant's effort from the probe of the
    engine as it runs NOW (a level it no longer offers fails the run with
    the reason instead of silently running on the default)."""
    from app.services import reasoning_params

    from . import effort_probe

    level = pc.get("effort") or "default"
    local = pc.get("locality") == "local"
    paid = (pc.get("money") or {}).get("cost_kind") == "api_price"
    if level == "default" and paid:
        return {}, {}
    ttl = effort_probe.UNPINNED_PROBE_TTL if local else effort_probe.CLOUD_PROBE_TTL
    try:
        probe = await effort_probe.get_or_probe_for(
            session, runtime_id=rt.id, model=pc["served_model_id"], serving_since=rt.serving_since,
            llm_url=rt.endpoint, api_key=api_key, ttl=ttl,
        )
    except Exception as exc:  # noqa: BLE001
        if level == "default":
            return {}, {}
        raise RunError("effort_probe_failed", str(exc)) from exc
    info = {"thinking_key": probe.effort_param or probe.thinking_key, "default_observed": probe.default_observed}
    if level == "default":
        return {}, info
    try:
        return effort_probe.level_fields(probe, level), info
    except reasoning_params.UnsupportedEffortError as exc:
        raise RunError("effort_not_offered", str(exc)) from exc


async def _set_metrics(factory, entry_id: uuid.UUID, **fields) -> None:
    async with _session(factory) as s:
        e = await s.get(BenchEntry, entry_id)
        if e is None:
            return
        for k in ("status", "error", "artifact_path", "attempt_no"):
            if k in fields:
                setattr(e, k, fields.pop(k))
        if fields:
            e.metrics = {**(e.metrics or {}), **fields}
        s.add(e)
        await s.commit()


async def run_api_entry(factory, challenge_id: uuid.UUID, pc: dict, entry_id: uuid.UUID, quiet: dict) -> bool:
    """One repetition of one class A contestant, up to MAX_ATTEMPTS attempts
    (every attempt counted). Returns True when the run produced a page."""
    from app.services import reasoning_params

    async with _session(factory) as s:
        entry = await s.get(BenchEntry, entry_id)
        challenge = await s.get(BenchChallenge, challenge_id)
        if entry is None or challenge is None or entry.status not in ("pending", "generating"):
            return entry is not None and entry.status in w1_receipt.DONE_ENTRY
        prompt = challenge.prompt_text
        attempt = entry.attempt_no or 1
        errors = list((entry.metrics or {}).get("attempt_errors") or [])
    while attempt <= MAX_ATTEMPTS:
        await _set_metrics(factory, entry_id, status="generating", attempt_no=attempt, phase="generating",
                           tokens_so_far=0, elapsed_ms=0, quiet_box=quiet.get("status"),
                           serving_age_min=quiet.get("age_min"))
        try:
            async with _session(factory) as s:
                rt, url, api_key = await _runtime_access(s, pc)
                fields, info = await _effort_fields(s, pc, rt, api_key)
            body = {
                "model": pc["served_model_id"],
                "messages": [{"role": "system", "content": c.SYSTEM_PROMPT},
                             {"role": "user", "content": prompt}],
                "max_tokens": pc.get("max_tokens") or c.BASE_MAX_TOKENS,
                "temperature": c.TEMPERATURE,
                "stream": True,
                "stream_options": {"include_usage": True},
                **fields,
            }
            sent = reasoning_params.wire_fields(fields)
            await _set_metrics(factory, entry_id, effort_sent=sent, thinking_key=info.get("thinking_key"),
                               temperature=c.TEMPERATURE, max_tokens=body["max_tokens"])
            logger.info("bench W1 run %s model=%s effort_sent=%s max_tokens=%s",
                        entry_id, body["model"], sent, body["max_tokens"])

            async def progress(live: dict) -> None:
                await _set_metrics(factory, entry_id, **live)

            content, metrics = await stream_chat(url, body, api_key=api_key, on_progress=progress)
            html = orch.extract_html(content)
            if not html:
                raise RunError("no_html_truncated" if metrics.get("truncated") else "no_html",
                               f"finish_reason={metrics.get('finish_reason')}")
            async with _session(factory) as s:
                entry = await s.get(BenchEntry, entry_id)
                out_dir = orch.entry_dir(entry)
                out_dir.mkdir(parents=True, exist_ok=True)
                path = out_dir / "index.html"
                path.write_text(html, encoding="utf-8")
                entry.artifact_path = str(path)
                entry.status = "generated"
                entry.error = None
                entry.metrics = {**(entry.metrics or {}), **metrics, "html_bytes": len(html.encode("utf-8")),
                                 "phase": "done", "attempt_errors": errors}
                s.add(entry)
                await s.commit()
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — every failure lands on the entry
            code = exc.code if isinstance(exc, RunError) else "run_failed"
            errors.append(code)
            logger.warning("bench W1 run %s attempt %d failed: %s", entry_id, attempt, exc)
            attempt += 1
            if attempt > MAX_ATTEMPTS or code in ("effort_not_offered", "runtime_gone"):
                await _set_metrics(factory, entry_id, status="failed", error=f"{code}: {exc}"[:2000],
                                   error_code=code, attempt_errors=errors, phase="done")
                return False
            await _set_metrics(factory, entry_id, attempt_errors=errors)
    return False


# ── Quiet box (W1.4) ─────────────────────────────────────────────────────


async def _fleet_usage_recent(session: AsyncSession, model: str, since: datetime) -> bool:
    from app.models.model_usage import ModelUsageEvent

    row = (
        await session.exec(
            select(ModelUsageEvent.id).where(
                ModelUsageEvent.model == model,
                ModelUsageEvent.ts >= since,
                ModelUsageEvent.harness != "vanilla",
            )
        )
    ).first()
    return row is not None


async def _box_busy(session: AsyncSession, pc: dict, rt) -> bool:
    from app.models.agent import Agent

    in_use = await w1_plan._in_use(rt.endpoint)
    if in_use:
        return True
    row_ids = [uuid.UUID(x) for x in pc.get("row_ids") or [pc["runtime_id"]]]
    agents = (await session.exec(select(Agent).where(Agent.runtime_id.in_(row_ids)))).all()  # type: ignore[union-attr]
    if any(a.current_task_id for a in agents):
        return True
    since = c.now_utc() - timedelta(seconds=c.BUSY_WINDOW_S)
    return await _fleet_usage_recent(session, pc["served_model_id"], since)


async def _set_wait(factory, challenge_id: uuid.UUID, reason: str | None, until: datetime | None = None) -> None:
    async with _session(factory) as s:
        ch = await s.get(BenchChallenge, challenge_id)
        if ch is None:
            return
        ch.wait_reason = reason
        if until is not None:
            ch.plan = {**(ch.plan or {}), "not_before": until.isoformat()}
        s.add(ch)
        await s.commit()


async def quiet_box(factory, challenge_id: uuid.UUID, pc: dict, *, sleep=asyncio.sleep) -> dict[str, Any]:
    """Wait for a quiet local box: serving_since + 15 min (no cap — the
    start time is shown), then an idle engine and no fleet usage in the
    last 2 min (at most 10 min, then run anyway: quiet_box "timeout",
    gate amber "box was busy")."""
    if pc.get("locality") != "local":
        return {"status": None, "age_min": None}
    busy_waited = 0.0
    was_busy = False
    while True:
        async with _session(factory) as s:
            rt, _url, _key = await _runtime_access(s, pc)
            since = c.as_aware(rt.serving_since)
            now = c.now_utc()
            if since is not None and now - since < timedelta(minutes=c.FRESH_MIN):
                until = since + timedelta(minutes=c.FRESH_MIN)
                await _set_wait(factory, challenge_id, "model_fresh", until)
                await sleep(min(QUIET_POLL_S, max(1.0, (until - now).total_seconds())))
                continue
            busy = await _box_busy(s, pc, rt)
        age = int((now - since).total_seconds() // 60) if since else None
        if not busy:
            await _set_wait(factory, challenge_id, None)
            return {"status": "busy" if was_busy else "ok", "age_min": age}
        if busy_waited >= c.QUIET_WAIT_MAX_S:
            await _set_wait(factory, challenge_id, None)
            return {"status": "timeout", "age_min": age}
        was_busy = True
        await _set_wait(factory, challenge_id, "box_busy")
        await sleep(QUIET_POLL_S)
        busy_waited += QUIET_POLL_S


# ── Class B (agent) run ──────────────────────────────────────────────────


async def agent_times(session: AsyncSession, task_id: uuid.UUID) -> tuple[int | None, int | None]:
    """(active_ms, wait_ms): work time from task events (blocked/inbox
    pauses subtracted, orchestrator.task_work_duration_ms) and the paused
    time separately — never one wall number."""
    from app.models.task import TaskEvent

    active = await orch.task_work_duration_ms(session, task_id)
    events = (await session.exec(select(TaskEvent).where(TaskEvent.task_id == task_id)
                                 .order_by(TaskEvent.created_at))).all()  # type: ignore[arg-type]
    start = next((e for e in events if e.to_status == "in_progress"), None)
    end = next((e for e in events if e.from_status == "in_progress" and e.to_status == "review"
                and e.changed_by == "agent"), None)
    if active is None or start is None or end is None:
        return active, None
    span = int((c.as_aware(end.created_at) - c.as_aware(start.created_at)).total_seconds() * 1000)
    return active, max(0, span - active)


async def agent_usage(session: AsyncSession, entry: BenchEntry) -> dict[str, Any]:
    """Attributed usage of the agent task: out / in (uncached) / cache-read
    kept apart (never a summed "in"), the model the task really used."""
    from sqlalchemy import func

    from app.models.model_usage import ModelUsageEvent

    if entry.task_id is None:
        return {}
    row = (
        await session.exec(
            select(func.sum(ModelUsageEvent.input_tokens), func.sum(ModelUsageEvent.output_tokens),
                   func.sum(ModelUsageEvent.cache_read_tokens), func.count(ModelUsageEvent.id))
            .where(ModelUsageEvent.task_id == entry.task_id)
        )
    ).one()
    tin, tout, cache, n = row
    if not n:
        return {}
    models = (await session.exec(select(ModelUsageEvent.model).where(ModelUsageEvent.task_id == entry.task_id))).all()
    out = {"tokens_in": int(tin or 0), "tokens_out": int(tout or 0), "tokens_cache_read": int(cache or 0),
           "usage_rows": int(n)}
    if models:
        out["served_model_id"] = max(set(models), key=list(models).count)
    return out


async def run_agent_contestant(factory, challenge_id: uuid.UUID, pc: dict, entry_id: uuid.UUID,
                               *, sleep=asyncio.sleep) -> bool:
    from app.models.task import Task

    async with _session(factory) as s:
        entry = await s.get(BenchEntry, entry_id)
        challenge = await s.get(BenchChallenge, challenge_id)
        if entry is None or challenge is None:
            return False
        if entry.status == "pending":
            entry.metrics = {**(entry.metrics or {}), "phase": "generating"}
            await orch.dispatch_agent_entry(s, entry, challenge)
            await s.refresh(entry)
            if entry.status == "failed":
                entry.metrics = {**(entry.metrics or {}), "error_code": "agent_missing", "phase": "done"}
                s.add(entry)
                await s.commit()
                return False
        started = c.now_utc()
    deadline = started + timedelta(minutes=pc.get("time_limit_min") or c.AGENT_TIME_LIMIT_MIN)
    while True:
        async with _session(factory) as s:
            entry = await s.get(BenchEntry, entry_id)
            if entry is None:
                return False
            if entry.status != "generating":
                break
            task = await s.get(Task, entry.task_id) if entry.task_id else None
            code = None
            if task is None:
                code = "task_deleted"
            elif task.status in ("failed", "aborted"):
                code = "agent_task_failed"
            elif c.now_utc() > deadline:
                code = "time_limit"
            if code:
                entry.status = "failed"
                entry.error = code
                entry.metrics = {**(entry.metrics or {}), "error_code": code, "phase": "done"}
                s.add(entry)
                await s.commit()
                if code == "time_limit" and task is not None:
                    try:
                        from app.services.operations import stop_task_run

                        user = ((await s.get(BenchChallenge, challenge_id)).plan or {}).get("created_by")
                        await stop_task_run(s, task.id, user or "system", reason="bench time limit")
                        await s.commit()
                    except Exception:  # noqa: BLE001 — the run is over either way
                        logger.info("bench W1: task %s not stopped at the time limit", task.id)
                return False
        await sleep(AGENT_POLL_S)
    async with _session(factory) as s:
        entry = await s.get(BenchEntry, entry_id)
        if entry.status not in w1_receipt.DONE_ENTRY:
            return False
        await finish_agent_metrics(s, entry)
        await s.commit()
    return True


async def finish_agent_metrics(session: AsyncSession, entry: BenchEntry) -> None:
    active, wait = (await agent_times(session, entry.task_id)) if entry.task_id else (None, None)
    usage = await agent_usage(session, entry)
    m = dict(entry.metrics or {})
    before = {k: m.get(k) for k in ("active_ms", "wait_ms", *usage.keys())}
    if active is not None:
        m["active_ms"] = active
    if wait is not None:
        m["wait_ms"] = wait
    m.update(usage)
    after = {k: m.get(k) for k in before}
    # "harvested_at" moves only when a number really changed — an unchanged
    # harvest must not create a new receipt version (write-once record).
    if after != before or not m.get("harvested_at"):
        m["harvested_at"] = c.now_utc().isoformat()
    m["phase"] = "done"
    entry.metrics = m
    session.add(entry)


# ── Record + compose + card ──────────────────────────────────────────────


async def _record_video_runs(factory, challenge_id: uuid.UUID) -> None:
    async with _session(factory) as s:
        challenge = await s.get(BenchChallenge, challenge_id)
        pairs = w1_tests.contestant_plans(challenge, await w1_tests.entries_of(s, challenge_id))
        targets = [
            e.id for pc, es in pairs if pc.get("video")
            for e in es if (e.repeat_idx or 1) == c.VIDEO_RUN_INDEX and e.status == "generated" and not e.video_path
        ]
        challenge.status = "rendering"
        s.add(challenge)
        await s.commit()
    for entry_id in targets:
        async with _session(factory) as s:
            entry = await s.get(BenchEntry, entry_id)
            challenge = await s.get(BenchChallenge, challenge_id)
            entry.metrics = {**(entry.metrics or {}), "phase": "recording"}
            s.add(entry)
            await s.commit()
            try:
                result = await orch.record_entry(entry, challenge)
                orch._apply_record_result(entry, result)
                page = result.get("page_blank")
                entry.metrics = {**(entry.metrics or {}),
                                 "page_check": ("blank" if page else "loads") if page is not None else "unknown",
                                 "phase": "done"}
                entry.status = "rendered"
            except Exception as exc:  # noqa: BLE001 — no video is a state, not a crash
                logger.warning("bench W1 record %s failed: %s", entry_id, exc)
                entry.metrics = {**(entry.metrics or {}), "record_error": str(exc)[:500], "phase": "done",
                                 "page_check": "unknown"}
            s.add(entry)
            await s.commit()


def artefact_name(kind: str, checksum: str | None, ext: str) -> str:
    return f"{kind}-{(checksum or 'nochecksum')[:12]}.{ext}"


async def renderer_has_card() -> bool:
    return FEATURE_CARD in await orch.renderer_features()


async def compose_artefacts(factory, challenge_id: uuid.UUID) -> None:
    """Video (16:9, chips above the panes, 4.5 s receipt card) + card PNG
    (1080×1350) from the SAME payload. Both file names carry the record
    checksum, so a stale artefact is never served after "Zahlen nachladen"."""
    async with _session(factory) as s:
        challenge = await s.get(BenchChallenge, challenge_id)
        entries = await w1_tests.entries_of(s, challenge_id)
        detail = await w1_tests.test_detail(s, challenge, entries=entries, with_actions=False, finished=True)
        pairs = w1_tests.contestant_plans(challenge, entries)
        videos = [e for pc, es in pairs if pc.get("video") for e in es
                  if (e.repeat_idx or 1) == c.VIDEO_RUN_INDEX and e.video_path]
        checksum = detail["gate"].get("checksum")
        challenge.status = "composing"
        s.add(challenge)
        await s.commit()
        old_video = challenge.composed_video_path
    root = orch.challenge_dir(challenge_id)
    root.mkdir(parents=True, exist_ok=True)
    card = w1_kit.card_payload(detail)
    frame = w1_kit.frame_payload(detail)
    new_video = None
    if await renderer_has_card():
        card_path = root / artefact_name("card", checksum, "png")
        try:
            await _post_renderer("/card", {"card": card, "output_path": str(card_path), "width": 1080,
                                           "height": 1350, "checksum": checksum})
        except Exception as exc:  # noqa: BLE001
            logger.warning("bench W1 card %s failed: %s", challenge_id, exc)
        if videos:
            out = root / artefact_name("video", checksum, "mp4")
            try:
                res = await _post_renderer("/compose", {
                    "inputs": [e.video_path for e in videos],
                    "labels": [p["model"] for p in frame["panes"]][: len(videos)],
                    "output_path": str(out),
                    "duration_s": c.VIDEO_DURATION_S,
                    "w1": {"card": card, "frame": {**frame, "panes": frame["panes"][: len(videos)]},
                           "checksum": checksum},
                })
                new_video = res.get("output_path")
            except Exception as exc:  # noqa: BLE001
                logger.warning("bench W1 compose %s failed: %s", challenge_id, exc)
    elif videos:
        # Renderer image older than W1: the old branded compose, no card.
        async with _session(factory) as s:
            challenge = await s.get(BenchChallenge, challenge_id)
            rows = [await s.get(BenchEntry, e.id) for e in videos]
            try:
                new_video = await orch.compose_challenge(s, challenge, rows)
            except Exception as exc:  # noqa: BLE001
                logger.warning("bench W1 legacy compose %s failed: %s", challenge_id, exc)
    async with _session(factory) as s:
        challenge = await s.get(BenchChallenge, challenge_id)
        if new_video:
            challenge.composed_video_path = new_video
        s.add(challenge)
        await s.commit()
    if new_video:
        orch._cleanup_old_compose(old_video, new_video)
    cleanup_stale_artefacts(challenge_id, checksum)


def cleanup_stale_artefacts(challenge_id: uuid.UUID, checksum: str | None) -> None:
    """W1.9 retention: only the current card/zip survive a recompose."""
    root = orch.challenge_dir(challenge_id)
    keep = (checksum or "")[:12]
    for pattern in ("card-*.png", "kit-*.zip"):
        for p in root.glob(pattern):
            if keep and keep in p.name:
                continue
            try:
                p.unlink()
            except OSError:
                pass


async def _post_renderer(path: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=orch.COMPOSE_TIMEOUT_S, transport=_transport) as cli:
        resp = await cli.post(f"{orch.PLAYWRIGHT_BASE}{path}", json=payload)
        resp.raise_for_status()
        return resp.json()


# ── Receipts + gate + inbox ──────────────────────────────────────────────


async def write_receipts(session: AsyncSession, challenge: BenchChallenge, *, reason: str) -> int:
    """One receipt version per contestant (write-once). Returns how many
    new versions were written."""
    entries = await w1_tests.entries_of(session, challenge.id)
    written = 0
    for pc, es in w1_tests.contestant_plans(challenge, entries):
        if not es:
            continue
        head = next((e for e in es if (e.repeat_idx or 1) == c.VIDEO_RUN_INDEX), es[0])
        harvested = (head.metrics or {}).get("harvested_at") if pc.get("run_class") == "B" else None
        receipt = w1_receipt.build_receipt(challenge, pc, es, harvested_at=harvested)
        _v, _sum, new = await w1_receipt.write_receipt(session, head, receipt, reason=reason)
        written += int(new)
    return written


async def store_gate(session: AsyncSession, challenge: BenchChallenge) -> dict[str, Any]:
    entries = await w1_tests.entries_of(session, challenge.id)
    detail = await w1_tests.test_detail(session, challenge, entries=entries, with_actions=False, finished=True)
    for pc, es in w1_tests.contestant_plans(challenge, entries):
        head = next((e for e in es if (e.repeat_idx or 1) == c.VIDEO_RUN_INDEX), es[0] if es else None)
        if head is not None:
            head.gate = json.loads(json.dumps(detail["gate"], default=str))
            session.add(head)
    return detail


async def post_inbox_item(session: AsyncSession, challenge: BenchChallenge, detail: dict) -> None:
    """Eingang item "Test fertig" — MC has no web push; the Eingang is the
    channel (critic 32 S7). One per test; resolving it has no side effect."""
    from app.models.approval import Approval

    existing = (
        await session.exec(select(Approval).where(Approval.action_type == "bench_result"))
    ).all()
    if any((a.payload or {}).get("test_id") == str(challenge.id) for a in existing):
        return
    names = [r["display_name"] for b in detail["blocks"] for r in b["contestants"]]
    session.add(Approval(
        action_type="bench_result",
        description=f"Test finished: {', '.join(names) or challenge.title} · {challenge.title}",
        payload={
            "test_id": str(challenge.id),
            "href": f"/bench/r/{challenge.id}",
            "title": challenge.title,
            "models": names,
            "state": detail["state"],
            "section": detail["section"],
            "gate_status": detail["gate"]["status"],
            "trigger": challenge.trigger,
            "truncation": bool(detail.get("truncation")),
        },
    ))


async def finalize(factory, challenge_id: uuid.UUID, *, reason: str = "run_end", compose: bool = True) -> None:
    async with _session(factory) as s:
        challenge = await s.get(BenchChallenge, challenge_id)
        entries = await w1_tests.entries_of(s, challenge_id)
        if not any(e.status in w1_receipt.DONE_ENTRY for e in entries):
            challenge.status = "failed"
            challenge.error = "all_runs_failed"
            challenge.wait_reason = None
            s.add(challenge)
            await s.commit()
            detail = await w1_tests.test_detail(s, challenge, with_actions=False)
            await post_inbox_item(s, challenge, detail)
            await s.commit()
            return
        await write_receipts(s, challenge, reason=reason)
        await s.commit()
    if compose:
        await compose_artefacts(factory, challenge_id)
    async with _session(factory) as s:
        challenge = await s.get(BenchChallenge, challenge_id)
        challenge.status = "review"
        challenge.error = None
        challenge.wait_reason = None
        s.add(challenge)
        await s.commit()
        detail = await store_gate(s, challenge)
        await post_inbox_item(s, challenge, detail)
        await s.commit()


# ── One test ─────────────────────────────────────────────────────────────


async def run_test(test_id: uuid.UUID, factory: SessionFactory | None = None, *, sleep=asyncio.sleep) -> None:
    """Runs a waiting test to its end. Resumable: done runs are skipped,
    a recorded video is not recorded again (backend restart heal)."""
    try:
        async with _session(factory) as s:
            ch = await s.get(BenchChallenge, test_id)
            if ch is None or ch.status not in ("waiting", "generating", "rendering", "composing"):
                return
            ch.status = "generating"
            s.add(ch)
            await s.commit()
            pairs = w1_tests.contestant_plans(ch, await w1_tests.entries_of(s, test_id))
            plan_a = [(pc, [e.id for e in es]) for pc, es in pairs if pc.get("run_class") == "A"]
            plan_b = [(pc, [e.id for e in es]) for pc, es in pairs if pc.get("run_class") == "B"]
        for pc, ids in plan_a:
            quiet = await quiet_box(factory, test_id, pc, sleep=sleep)
            for entry_id in ids:
                await run_api_entry(factory, test_id, pc, entry_id, quiet)
        for pc, ids in plan_b:
            for entry_id in ids:
                await run_agent_contestant(factory, test_id, pc, entry_id, sleep=sleep)
        await _record_video_runs(factory, test_id)
        await finalize(factory, test_id)
    except asyncio.CancelledError:
        logger.info("bench W1 test %s cancelled", test_id)
        raise
    except Exception:  # noqa: BLE001 — nothing hangs silently
        logger.exception("bench W1 test %s crashed", test_id)
        try:
            async with _session(factory) as s:
                ch = await s.get(BenchChallenge, test_id)
                if ch is not None and ch.status in ("generating", "rendering", "composing"):
                    entries = await w1_tests.entries_of(s, test_id)
                    if any(e.status in w1_receipt.DONE_ENTRY for e in entries):
                        ch.status = "review"
                    else:
                        ch.status = "failed"
                        ch.error = "all_runs_failed"
                    s.add(ch)
                    await s.commit()
        except Exception:  # noqa: BLE001
            logger.exception("bench W1 test %s failure write failed", test_id)
    finally:
        _tasks.pop(test_id, None)


# ── Harvest ("Zahlen nachladen") ─────────────────────────────────────────


async def harvest(session: AsyncSession, challenge: BenchChallenge) -> int:
    """Read the agent usage again, write a NEW receipt version where a
    number changed (v0 rows get their first receipt, reason backfill_v0).
    Returns how many versions were written; the caller recomposes."""
    from app.models.task import Task

    from .agent_effort import observed_agent_effort

    entries = await w1_tests.entries_of(session, challenge.id)
    for e in entries:
        is_agent = (e.run_class or ("B" if e.source_kind == "agent" else "A")) == "B"
        if not is_agent or e.status not in w1_receipt.DONE_ENTRY:
            continue
        await finish_agent_metrics(session, e)
        task = await session.get(Task, e.task_id) if e.task_id else None
        if task is not None:
            e.metrics = {**(e.metrics or {}), "effort_observed": await observed_agent_effort(session, e, task)}
            session.add(e)
    await session.commit()
    reason = "harvest" if w1_tests.is_v1(challenge) else "backfill_v0"
    written = await write_receipts(session, challenge, reason=reason)
    await session.commit()
    if w1_tests.is_v1(challenge):
        await store_gate(session, challenge)
        await session.commit()
    return written


async def recompose(test_id: uuid.UUID, factory: SessionFactory | None = None) -> None:
    """Background: rebuild the video's receipt card + the card PNG from the
    newest receipts (no re-record)."""
    try:
        async with _session(factory) as s:
            ch = await s.get(BenchChallenge, test_id)
            if ch is None:
                return
            status_before = ch.status
        await compose_artefacts(factory, test_id)
        async with _session(factory) as s:
            ch = await s.get(BenchChallenge, test_id)
            ch.status = status_before if status_before not in ("rendering", "composing") else "review"
            s.add(ch)
            await store_gate(s, ch)
            await s.commit()
    except Exception:  # noqa: BLE001
        logger.exception("bench W1 recompose %s failed", test_id)


async def rerender(test_id: uuid.UUID, factory: SessionFactory | None = None) -> None:
    """Background: record run 1 again (seeded Math.random → same video) and
    recompose."""
    try:
        async with _session(factory) as s:
            ch = await s.get(BenchChallenge, test_id)
            for e in await w1_tests.entries_of(s, test_id):
                if e.video_path and e.status == "rendered":
                    e.video_path = None
                    e.status = "generated"
                    s.add(e)
            await s.commit()
        await _record_video_runs(factory, test_id)
        await compose_artefacts(factory, test_id)
        async with _session(factory) as s:
            ch = await s.get(BenchChallenge, test_id)
            ch.status = "review"
            s.add(ch)
            await store_gate(s, ch)
            await s.commit()
    except Exception:  # noqa: BLE001
        logger.exception("bench W1 rerender %s failed", test_id)


# ── Auto tests (W1.11) ───────────────────────────────────────────────────


async def resolve_auto_tests(factory: SessionFactory | None = None) -> None:
    """Waiting auto tests without contestants: once the switched model
    answers on its host, freeze the Standard plan for it (start ≥ 15 min
    after serving_since); no answer within the timeout → switch_failed."""
    async with _session(factory) as s:
        waiting = (
            await s.exec(select(BenchChallenge).where(BenchChallenge.status == "waiting",
                                                      BenchChallenge.trigger == "auto_switch"))
        ).all()
        pending = [ch for ch in waiting if not (ch.plan or {}).get("contestants")]
        if not pending:
            return
        views = await w1_plan.load_views(s)
    for ch in pending:
        auto = (ch.plan or {}).get("auto") or {}
        host_id = auto.get("host_id")
        requested = c.as_aware(datetime.fromisoformat(auto["requested_at"])) if auto.get("requested_at") else c.now_utc()
        match = None
        for v in views:
            if v.locality != "local" or not v.served_model or v.runtime.host_id is None:
                continue
            if str(v.runtime.host_id) != host_id:
                continue
            recipe_ok = v.recipe is None or v.recipe.slug == auto.get("recipe_slug")
            since = v.serving_since
            if recipe_ok and since is not None and since >= requested - timedelta(minutes=2):
                match = v
                break
        async with _session(factory) as s:
            ch = await s.get(BenchChallenge, ch.id)
            if ch is None or ch.status != "waiting":
                continue
            if match is None:
                if c.now_utc() - requested > timedelta(minutes=c.AUTO_TEST_SWITCH_TIMEOUT_MIN):
                    ch.status = "failed"
                    ch.error = "switch_failed"
                    ch.wait_reason = None
                    s.add(ch)
                    await s.commit()
                continue
            try:
                pcs, money, _blockers = await w1_tests._resolve_contestants(
                    s, [{"runtime_id": match.runtime.id, "harness": "api", "efforts": ["default"]}],
                    preset="standard", n_runs=c.PRESET_RUNS["standard"], prompt_text=ch.prompt_text,
                    max_tokens_override=None,
                )
            except Exception as exc:  # noqa: BLE001
                logger.info("bench W1 auto test %s not ready yet: %s", ch.id, exc)
                continue
            if money.get("cost_kind") == "api_price":
                ch.status = "failed"
                ch.error = "switch_failed"
                s.add(ch)
                await s.commit()
                continue
            not_before = match.serving_since + timedelta(minutes=c.FRESH_MIN)
            ch.plan = {**(ch.plan or {}), "contestants": pcs, "money": money,
                       "picks": [{"runtime_id": str(match.runtime.id), "harness": "api", "efforts": ["default"]}],
                       "not_before": not_before.isoformat(), "context_limit": match.runtime.max_context_len,
                       "video_seconds": c.VIDEO_DURATION_S + 4.5,
                       "temperature": c.TEMPERATURE, "system_prompt_sha": c.SYSTEM_PROMPT_SHA}
            ch.wait_reason = "model_fresh"
            s.add(ch)
            for pc in pcs:
                for r in range(1, pc["n_runs"] + 1):
                    s.add(BenchEntry(
                        challenge_id=ch.id, model_label=pc["served_model_id"], position=pc["position"],
                        source_kind="api", spark_model=pc["served_model_id"], runtime_id=uuid.UUID(pc["runtime_id"]),
                        harness="api", run_class="A", effort_requested="default", repeat_idx=r,
                        metrics={"effort_requested": "default", "phase": "queued"},
                    ))
            await s.commit()


# ── Probe warming (once per serving_since, only on a quiet box) ──────────


async def warm_probes(factory: SessionFactory | None = None) -> None:
    from . import effort_probe

    async with _session(factory) as s:
        views = await w1_plan.load_views(s)
        for v in views:
            if v.locality != "local" or not v.served_model or not v.serving_since:
                continue
            if c.now_utc() - v.serving_since < timedelta(minutes=c.FRESH_MIN) or v.in_use or v.bench_running:
                continue
            if any(a.current_task_id for a in v.agents):
                continue
            key = f"{v.runtime.id}:{v.served_model}:{v.serving_since.isoformat()}"
            failed_at = _probe_failures.get(key)
            if failed_at and c.now_utc() - failed_at < PROBE_RETRY_AFTER:
                continue
            row = await w1_plan.stored_probe(s, v)
            if row is not None and row.status != "failed":
                continue
            probe = await effort_probe.get_or_probe_for(
                s, runtime_id=v.runtime.id, model=v.served_model, serving_since=v.runtime.serving_since,
                llm_url=v.runtime.endpoint,
            )
            if probe.status == "failed":
                _probe_failures[key] = c.now_utc()
            return  # one probe per tick: a probe is a few real requests


# ── Background loop ──────────────────────────────────────────────────────


async def heal_after_restart(factory: SessionFactory | None = None) -> None:
    """A test this process does not run but that says it runs was cut off by
    a restart: its direct runs start again as a new attempt (counted), the
    test goes back to the queue. Agent runs keep their task."""
    async with _session(factory) as s:
        rows = (
            await s.exec(select(BenchChallenge).where(
                BenchChallenge.protocol_version == c.PROTOCOL_V1,
                BenchChallenge.status.in_(("generating", "rendering", "composing")),  # type: ignore[union-attr]
            ))
        ).all()
        for ch in rows:
            if ch.id in running():
                continue
            for e in await w1_tests.entries_of(s, ch.id):
                if e.status == "generating" and e.source_kind == "api":
                    errors = list((e.metrics or {}).get("attempt_errors") or []) + ["backend_restart"]
                    e.status = "pending"
                    e.attempt_no = (e.attempt_no or 1) + 1
                    e.metrics = {**(e.metrics or {}), "attempt_errors": errors, "phase": "queued"}
                    s.add(e)
            ch.status = "waiting"
            s.add(ch)
        await s.commit()


async def next_test_id(factory: SessionFactory | None = None) -> uuid.UUID | None:
    async with _session(factory) as s:
        rows = (
            await s.exec(
                select(BenchChallenge)
                .where(BenchChallenge.status == "waiting", BenchChallenge.protocol_version == c.PROTOCOL_V1)
                .order_by(BenchChallenge.created_at)  # type: ignore[arg-type]
            )
        ).all()
    now = c.now_utc()
    for ch in rows:
        plan = ch.plan or {}
        if not plan.get("contestants"):
            continue
        if ch.trigger == "auto_switch" and plan.get("not_before"):
            nb = c.as_aware(datetime.fromisoformat(plan["not_before"]))
            if nb and nb > now:
                continue
        return ch.id
    return None


async def tick(factory: SessionFactory | None = None) -> uuid.UUID | None:
    await resolve_auto_tests(factory)
    if running():
        return None
    test_id = await next_test_id(factory)
    if test_id is not None:
        from app.utils import create_tracked_task

        _tasks[test_id] = create_tracked_task(run_test(test_id, factory), name=f"bench_w1_test({test_id})")
        return test_id
    try:
        await warm_probes(factory)
    except Exception:  # noqa: BLE001
        logger.info("bench W1 probe warming skipped", exc_info=True)
    return None


async def w1_loop() -> None:
    global _wake_event
    _wake_event = asyncio.Event()
    await asyncio.sleep(LOOP_INITIAL_DELAY_S)
    try:
        await heal_after_restart()
    except Exception:  # noqa: BLE001
        logger.exception("bench W1 heal failed")
    while True:
        try:
            await tick()
        except Exception:  # noqa: BLE001 — a DB hiccup must not end the loop
            logger.exception("bench W1 tick failed")
        try:
            await asyncio.wait_for(_wake_event.wait(), timeout=LOOP_INTERVAL_S)
        except asyncio.TimeoutError:
            pass
        _wake_event.clear()


# Used by the kit download and the legacy rerender/recompose routes.
def card_path(challenge_id: uuid.UUID, checksum: str | None) -> Path:
    return orch.challenge_dir(challenge_id) / artefact_name("card", checksum, "png")
