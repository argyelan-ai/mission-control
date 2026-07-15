"""Token Harvester — reads JSONL transcripts and writes model_usage_events.

Data sources:
  - ~/.mc/agents/{slug}/claude-config/projects/**/*.jsonl (cli-bridge + hermes,
    Claude Code JSONL schema — type=="assistant")
  - ~/.mc/agents/{slug}/omp-sessions/**/*.jsonl (omp headless harness, ADR-045 —
    e.g. Sparky on Qwen/Spark. Distinct schema — type=="message", see
    parse_transcript_line)
  - ~/.claude/projects/**/*.jsonl (boss-host + operator's private sessions → boss attribution heuristic!)

Dedup key: top-level `uuid` (UNIQUE) for Claude Code lines. message.id has
1042+ collisions — NEVER dedupe on that! omp lines carry no top-level uuid at
all (only a collision-prone 8-hex `id`) — their dedup key is synthesized as
``{session_id}:{id}`` where session_id comes from the JSONL filename (omp
writes one file per session, no sessionId field in the line itself).
Idempotent: can run over the same files any number of times.
Offset resume: harvest_state stores processed_lines → only reads new lines.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.model_usage import ModelPrice, ModelUsageEvent, ModelUsageHarvestState

# Grace window added to a task's completed_at when checking whether an
# event's ts falls "inside" the task's active lifetime (agents keep writing
# transcript lines briefly after PATCH status: done/review).
_TASK_WINDOW_GRACE = timedelta(hours=1)

logger = logging.getLogger("mc.token_harvester")

# ── MC workspace indicators for boss attribution ──────────────────────────
# Paths/branches that point to an MC context → attribute to boss
_MC_CWD_MARKERS = [
    "mission-control",  # main repo
    "/.mc/",            # agent workspaces under ~/.mc/
]
_MC_BRANCH_PREFIX = "task/"


# ── Pure helper functions (testable without a DB) ─────────────────────────


def parse_transcript_line(line: str, session_id: str | None = None) -> dict[str, Any] | None:
    """Parses one JSONL line from either a Claude Code or an omp transcript.

    Dispatches on the top-level ``type`` field:
    - ``"assistant"`` → Claude Code schema (nested message.model/message.usage,
      top-level uuid).
    - ``"message"`` → omp schema (ADR-045 headless harness — top-level
      model/provider, camelCase usage keys, no top-level uuid).
    Anything else (user lines, unknown types, invalid JSON) → None.

    Args:
        line: One raw JSONL line.
        session_id: Only consulted for omp lines (which have no sessionId
            field of their own) — the caller derives it from the JSONL
            filename via ``_derive_omp_session_id`` and passes it through so
            the dedup key can be namespaced per session. Ignored for Claude
            Code lines, which carry their own ``sessionId``.

    Returns a normalized dict, or None if the line is filtered out.
    """
    try:
        d = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None

    msg_type = d.get("type")
    if msg_type == "assistant":
        return _parse_claude_line(d)
    if msg_type == "message":
        return _parse_omp_line(d, session_id)
    return None


def _parse_claude_line(d: dict[str, Any]) -> dict[str, Any] | None:
    """Parses a Claude Code transcript line (``type == "assistant"``).

    Filters:
    - Only when message.usage is present
    - Only when message.model is present and NOT '<synthetic>'
    - Only when a top-level uuid is present
    """
    msg_uuid = d.get("uuid")
    if not msg_uuid:
        return None

    message = d.get("message")
    if not message:
        return None

    usage = message.get("usage")
    if not usage:
        return None

    model = message.get("model")
    if not model:
        return None
    if "<synthetic>" in model:
        return None

    # Cache tokens (optional, default 0)
    cache_read = usage.get("cache_read_input_tokens", 0) or 0
    # cache_creation sums ephemeral_5m + ephemeral_1h if present
    cache_write = usage.get("cache_creation_input_tokens", 0) or 0

    return {
        "uuid": msg_uuid,
        "msg_id": message.get("id"),  # For debug only — NOT for dedup!
        "session_id": d.get("sessionId", ""),
        "timestamp": d.get("timestamp", ""),
        "cwd": d.get("cwd", ""),
        "git_branch": d.get("gitBranch"),
        "model": model,
        "input_tokens": usage.get("input_tokens", 0) or 0,
        "output_tokens": usage.get("output_tokens", 0) or 0,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
    }


def _parse_omp_line(d: dict[str, Any], session_id: str | None) -> dict[str, Any] | None:
    """Parses an omp transcript line (``type == "message"``, ADR-045).

    Real sample (Sparky/Qwen on Spark, 2026-07-15)::

        {"type":"message","id":"74f7a91e","message":{"role":"assistant","usage":
         {"input":28848,"output":135,"cacheRead":0,"cacheWrite":0,
          "cost":{"input":0.004,...,"total":0.0042}}},
         "model":"Qwen/Qwen3.6-35B-A3B-FP8","provider":"mc-openai", ...}

    Filters:
    - Only when message.role == "assistant"
    - Only when message.usage is present
    - Only when a top-level model is present

    Dedup key: omp has no top-level uuid — only a short (8 hex) `id` that
    collides across sessions. Since one JSONL file == one omp session, the
    caller-supplied session_id namespaces it: ``{session_id}:{id}``.

    Cost: omp's own `usage.cost` reflects a generic per-token price table
    baked into the omp binary — not Mark's actual cost, which for a local
    Spark/vLLM model is $0. We deliberately ignore it and let the existing
    ModelPrice-based pipeline (match_price/_compute_cost_usd, applied
    uniformly to every harness in _process_jsonl_file) decide: no matching
    price row → cost_usd stays NULL, same as any other unlisted model.
    """
    message = d.get("message")
    if not message:
        return None
    if message.get("role") != "assistant":
        return None

    usage = message.get("usage")
    if not usage:
        return None

    model = d.get("model")
    if not model:
        return None

    short_id = d.get("id")
    if not short_id:
        return None

    sess = session_id or ""
    msg_uuid = f"{sess}:{short_id}"

    return {
        "uuid": msg_uuid,
        "msg_id": short_id,  # For debug only — NOT for dedup!
        "session_id": sess,
        "timestamp": d.get("timestamp", ""),
        "cwd": d.get("cwd", ""),
        "git_branch": d.get("gitBranch"),
        "model": model,
        "provider": d.get("provider"),
        "input_tokens": usage.get("input", 0) or 0,
        "output_tokens": usage.get("output", 0) or 0,
        "cache_read_tokens": usage.get("cacheRead", 0) or 0,
        "cache_write_tokens": usage.get("cacheWrite", 0) or 0,
    }


def _derive_omp_session_id(path: str) -> str:
    """omp writes one JSONL file per session — the filename (minus the
    extension) is the natural session id, e.g.
    ``2026-07-15T16-29-31-091Z_019f669c-....jsonl``. Only consulted by
    ``_parse_omp_line``; Claude Code lines carry their own sessionId field
    and ignore this."""
    return Path(path).stem


def harvest_file(path: str, processed_lines: int = 0) -> list[dict[str, Any]]:
    """Reads a JSONL file from `processed_lines` onward and returns parsed records.

    Lines filtered out by parse_transcript_line (user, synthetic, etc.) are
    skipped. The dedup logic (same uuid) lives at the DB insert — harvest_file
    returns all parsed records unfiltered.

    Args:
        path: Absolute path to the JSONL file.
        processed_lines: Number of lines already read (offset resume).

    Returns:
        List of parsed records (may contain duplicate uuids → DB does the dedup).
    """
    records: list[dict[str, Any]] = []
    session_id = _derive_omp_session_id(path)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i < processed_lines:
                    continue
                line = line.strip()
                if not line:
                    continue
                rec = parse_transcript_line(line, session_id=session_id)
                if rec is not None:
                    records.append(rec)
    except OSError as e:
        logger.warning("harvest_file(%s): OS error: %s", path, e)
    return records


def match_price(
    model: str,
    ts: datetime,
    prices: list[ModelPrice],
) -> dict[str, float] | None:
    """Finds the best price for a model at time ts.

    Matching logic:
    1. Only prices with valid_from <= ts
    2. fnmatch glob on model_pattern
    3. Higher priority wins; on equal priority: newer valid_from wins

    Returns a dict with the price fields, or None if there's no match.
    """
    candidates: list[ModelPrice] = []
    for price in prices:
        # Time filter: only prices that were valid at time ts
        valid_from = price.valid_from
        if valid_from.tzinfo is None:
            valid_from = valid_from.replace(tzinfo=timezone.utc)
        ts_aware = ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)
        if valid_from > ts_aware:
            continue
        # Glob match
        if fnmatch.fnmatch(model, price.model_pattern):
            candidates.append(price)

    if not candidates:
        return None

    # Sort: priority DESC, valid_from DESC (newest first)
    candidates.sort(key=lambda p: (p.priority, p.valid_from), reverse=True)
    best = candidates[0]

    return {
        "input_per_mtok": best.input_per_mtok,
        "output_per_mtok": best.output_per_mtok,
        "cache_read_per_mtok": best.cache_read_per_mtok,
        "cache_write_per_mtok": best.cache_write_per_mtok,
    }


def _compute_cost_usd(
    price_info: dict[str, float],
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
) -> float:
    """Computes cost_usd from price info and token counts."""
    return (
        input_tokens * price_info["input_per_mtok"]
        + output_tokens * price_info["output_per_mtok"]
        + cache_read_tokens * price_info["cache_read_per_mtok"]
        + cache_write_tokens * price_info["cache_write_per_mtok"]
    ) / 1_000_000.0


def _should_attribute_boss_path(cwd: str, git_branch: str | None) -> bool:
    """Decides whether a ~/.claude line should be attributed to the boss.

    Boss criteria (OR):
    1. cwd contains an MC workspace marker (mission-control, /.mc/)
    2. gitBranch starts with 'task/'

    ANYTHING ELSE → private session → SKIP.
    """
    if git_branch and git_branch.startswith(_MC_BRANCH_PREFIX):
        return True
    for marker in _MC_CWD_MARKERS:
        if marker in cwd:
            return True
    return False


def _harness_from_slug(slug: str) -> str:
    """Derives the harness type from the agent slug."""
    if slug == "sparky":
        return "sparky"
    # Host agents
    if slug in ("hermes", "boss-host", "boss", "jarvis"):
        return "host"
    return "cli-bridge"


def _provider_from_model(model: str) -> str:
    """Derives the provider name from the model string (heuristic)."""
    m = model.lower()
    if "claude" in m:
        return "anthropic"
    if ":" in m or "qwen2.5-coder" in m or "llama" in m or "mistral" in m:
        # Format "model:tag" = ollama
        return "ollama"
    if "/" in m:
        # Format "Organization/model-name" = lmstudio / vllm
        return "lmstudio"
    return "unknown"


def _parse_ts(ts_str: str) -> datetime:
    """Parses an ISO-8601 timestamp into an aware datetime."""
    try:
        # Python 3.11+ fromisoformat understands 'Z' as UTC
        if ts_str.endswith("Z"):
            ts_str = ts_str[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, AttributeError):
        return datetime.now(timezone.utc)


# ── Main harvest logic ─────────────────────────────────────────────────────


def _host_home() -> Path:
    """Host HOME — in the container via HOME_HOST (PR #137 pattern), else expanduser.

    The transcript mounts live under the absolute HOST path
    (/Users/.../.mc, /Users/.../.claude); ~ in the container points to /home/mcuser.
    """
    return Path(os.environ.get("HOME_HOST") or Path.home())


def _expand_harvest_path(p: str) -> str:
    if p.startswith("~"):
        return str(_host_home() / p.lstrip("~/").lstrip("/"))
    return str(Path(p).expanduser())


def _slugify_agent_name(name: str) -> str:
    """Same slug convention as docker_agent_sync._agent_slug."""
    return name.lower().replace(" ", "-")


async def _build_agent_slug_map(session: AsyncSession) -> dict[str, Any]:
    """{slug: agent_id} from the agents table — default attribution."""
    from app.models import Agent

    result = await session.exec(select(Agent))
    return {_slugify_agent_name(a.name): a.id for a in result.all()}


def _normalize_workspace_path(p: str) -> str:
    """Realpath-normalizes a cwd/workspace_path for exact-match comparison.

    Tolerates trailing slashes and relative segments. Safe on paths that
    don't exist on this filesystem (os.path.realpath never raises for a
    missing path — it just normalizes the components it can).
    """
    if not p:
        return ""
    return os.path.realpath(p.rstrip("/"))


async def _build_task_workspace_map(session: AsyncSession) -> dict[str, list[dict[str, Any]]]:
    """{normalized workspace_path: [candidate task dicts]} for attribution.

    Only tasks with a non-null workspace_path are loaded (one query per
    harvest cycle, same pattern as _build_agent_slug_map). Multiple tasks
    can share a workspace_path (re-runs reuse the same worktree dir) — all
    candidates are kept here, disambiguated later by _resolve_task_id.
    """
    from app.models.task import Task
    from app.services.git_service import slugify_workspace_slug

    result = await session.exec(
        select(Task.id, Task.title, Task.workspace_path, Task.created_at, Task.completed_at)
        .where(Task.workspace_path.is_not(None))
    )

    workspace_map: dict[str, list[dict[str, Any]]] = {}
    for task_id, title, workspace_path, created_at, completed_at in result.all():
        norm = _normalize_workspace_path(workspace_path)
        if not norm:
            continue
        workspace_map.setdefault(norm, []).append({
            "task_id": task_id,
            "branch": f"task/{slugify_workspace_slug(title)}",
            "created_at": created_at,
            "completed_at": completed_at,
        })
    return workspace_map


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _resolve_task_id(
    candidates: list[dict[str, Any]],
    git_branch: str | None,
    ts: datetime,
) -> Any | None:
    """Picks the task_id for an event given same-workspace_path candidates.

    Cascade:
    1. Exactly one candidate → that one (the common case, no collision).
    2. gitBranch == 'task/{slug}' narrows to matching candidates; if that's
       down to exactly one, use it.
    3. Among the remaining candidates, the one whose lifetime window
       (created_at .. completed_at + grace, open-ended if not completed)
       contains the event ts, newest created_at first.
    4. Otherwise: newest created_at wins.
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]["task_id"]

    pool = candidates
    if git_branch:
        branch_matches = [c for c in pool if c["branch"] == git_branch]
        if len(branch_matches) == 1:
            return branch_matches[0]["task_id"]
        if branch_matches:
            pool = branch_matches

    ts_aware = _aware(ts)
    window_matches = []
    for c in pool:
        start = _aware(c["created_at"])
        end = _aware(c["completed_at"]) + _TASK_WINDOW_GRACE if c["completed_at"] else None
        if start <= ts_aware and (end is None or ts_aware <= end):
            window_matches.append(c)
    if window_matches:
        window_matches.sort(key=lambda c: c["created_at"], reverse=True)
        return window_matches[0]["task_id"]

    newest = max(pool, key=lambda c: c["created_at"])
    return newest["task_id"]


async def run_harvest(
    session: AsyncSession,
    *,
    agent_base_paths: list[str] | None = None,
    boss_base_paths: list[str] | None = None,
    agent_slug_map: dict[str, Any] | None = None,
    task_workspace_map: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, int]:
    """Scans all JSONL files, parses assistant lines, and inserts events.

    Configurable via:
    - agent_base_paths: paths to ~/.mc/agents-like directories
                        (default: settings.token_harvest_paths, expanduser)
    - boss_base_paths: paths to ~/.claude/projects-like directories
    - agent_slug_map: {slug: agent_id} for agent lookup (optional)
    - task_workspace_map: {normalized workspace_path: [candidate task dicts]}
                          for task attribution (optional, built from DB if
                          omitted — see _build_task_workspace_map)

    Returns:
        {"files_scanned": N, "new_events": M, "skipped_private": K,
         "backfilled_task_id": L}
    """
    from app.config import settings as app_settings

    # Default paths from settings (expanduser)
    if agent_base_paths is None:
        harvest_paths = getattr(app_settings, "token_harvest_paths", [
            "~/.mc/agents",
        ])
        agent_base_paths = [_expand_harvest_path(p) for p in harvest_paths]

    if boss_base_paths is None:
        boss_base_paths = [str(_host_home() / ".claude/projects")]

    if agent_slug_map is None:
        # Default: attribution from the agents table (slug = name-based)
        agent_slug_map = await _build_agent_slug_map(session)

    if task_workspace_map is None:
        task_workspace_map = await _build_task_workspace_map(session)

    # Boss agent for ~/.claude attribution (host agent, slug starts with "boss")
    boss_agent_id = next(
        (aid for slug, aid in agent_slug_map.items() if slug.startswith("boss")),
        None,
    )

    # Load prices once (for cost calculation)
    prices_result = await session.exec(select(ModelPrice))
    all_prices: list[ModelPrice] = list(prices_result.all())

    # Load harvest state (all known files)
    state_result = await session.exec(select(ModelUsageHarvestState))
    state_map: dict[str, ModelUsageHarvestState] = {
        s.file_path: s for s in state_result.all()
    }

    stats = {
        "files_scanned": 0,
        "new_events": 0,
        "skipped_private": 0,
        "backfilled_task_id": 0,
    }

    # ── Agent paths: ~/.mc/agents/{slug}/claude-config/projects/**/*.jsonl
    #    AND ~/.mc/agents/{slug}/omp-sessions/**/*.jsonl (ADR-045 omp harness,
    #    e.g. Sparky — no host mount existed for this before this fix, so the
    #    glob was silently finding zero files for omp agents) ──────────────
    for base_str in agent_base_paths:
        base = Path(base_str)
        if not base.exists():
            continue
        for pattern in (
            "*/claude-config/projects/**/*.jsonl",
            "*/omp-sessions/**/*.jsonl",
        ):
            for jsonl_path in sorted(base.glob(pattern)):
                # Slug from the path segment directly under base
                try:
                    rel = jsonl_path.relative_to(base)
                    slug = rel.parts[0]
                except (ValueError, IndexError):
                    slug = "unknown"

                agent_id = agent_slug_map.get(slug)
                harness = _harness_from_slug(slug)

                await _process_jsonl_file(
                    session=session,
                    path=str(jsonl_path),
                    agent_id=agent_id,
                    harness=harness,
                    is_boss_path=False,
                    all_prices=all_prices,
                    state_map=state_map,
                    stats=stats,
                    task_workspace_map=task_workspace_map,
                )

    # ── Boss paths: ~/.claude/projects/**/*.jsonl ───────────────────────────
    for base_str in boss_base_paths:
        base = Path(base_str)
        if not base.exists():
            continue
        for jsonl_path in sorted(base.glob("**/*.jsonl")):
            await _process_jsonl_file(
                session=session,
                path=str(jsonl_path),
                agent_id=boss_agent_id,  # only applies to MC-attributed lines
                harness="host",
                is_boss_path=True,
                all_prices=all_prices,
                state_map=state_map,
                stats=stats,
                task_workspace_map=task_workspace_map,
                # boss_agent_id gets reloaded later if needed
            )

    # Commit at the end
    try:
        await session.commit()
    except Exception as e:
        logger.error("run_harvest: commit error: %s", e)
        await session.rollback()

    logger.info(
        "run_harvest: files=%d new=%d skipped_private=%d backfilled_task_id=%d",
        stats["files_scanned"],
        stats["new_events"],
        stats["skipped_private"],
        stats["backfilled_task_id"],
    )
    return stats


async def _process_jsonl_file(
    session: AsyncSession,
    path: str,
    agent_id: Any | None,
    harness: str,
    is_boss_path: bool,
    all_prices: list[ModelPrice],
    state_map: dict[str, ModelUsageHarvestState],
    stats: dict[str, int],
    task_workspace_map: dict[str, list[dict[str, Any]]] | None = None,
) -> None:
    """Processes a single JSONL file (offset resume, batch insert)."""
    stats["files_scanned"] += 1

    try:
        current_mtime = os.path.getmtime(path)
    except OSError:
        return

    # mtime skip: file unchanged → skip
    state = state_map.get(path)
    if state and state.mtime == current_mtime:
        return  # nothing new in this file

    processed_lines = state.processed_lines if state else 0

    # Read lines (from offset)
    records = harvest_file(path, processed_lines)
    if not records:
        # File changed but no new valid lines → update state
        total_lines = _count_lines(path)
        await _update_harvest_state(session, state_map, path, current_mtime, total_lines)
        return

    # Batch dedup: which uuids are already in the DB, and which of those
    # still have task_id IS NULL (candidates for the backfill pass below)?
    candidate_uuids = [r["uuid"] for r in records]
    existing_result = await session.exec(
        select(
            ModelUsageEvent.message_uuid,
            ModelUsageEvent.id,
            ModelUsageEvent.task_id,
        ).where(ModelUsageEvent.message_uuid.in_(candidate_uuids))
    )
    existing_rows = existing_result.all()
    existing_uuids: set[str] = {row[0] for row in existing_rows}
    # uuid → event.id, only for rows still missing task_id
    existing_untasked: dict[str, Any] = {
        row[0]: row[1] for row in existing_rows if row[2] is None
    }

    new_records = [r for r in records if r["uuid"] not in existing_uuids]
    task_workspace_map = task_workspace_map or {}

    def _resolve_task_for_rec(rec: dict[str, Any], ts: datetime) -> Any | None:
        norm_cwd = _normalize_workspace_path(rec.get("cwd", ""))
        candidates = task_workspace_map.get(norm_cwd)
        if not candidates:
            return None
        return _resolve_task_id(candidates, rec.get("git_branch"), ts)

    # For boss paths: decide attribution per line
    new_events_count = 0
    for rec in new_records:
        if is_boss_path:
            cwd = rec.get("cwd", "")
            git_branch = rec.get("git_branch")
            if not _should_attribute_boss_path(cwd, git_branch):
                stats["skipped_private"] += 1
                continue
            # Boss: passed-through boss_agent_id (None if no boss agent exists)
            eff_agent_id = agent_id
        else:
            eff_agent_id = agent_id

        # Price matching
        ts = _parse_ts(rec["timestamp"])
        model = rec["model"]
        price_info = match_price(model, ts, all_prices)
        cost_usd: float | None = None
        if price_info is not None:
            cost_usd = _compute_cost_usd(
                price_info,
                rec["input_tokens"],
                rec["output_tokens"],
                rec["cache_read_tokens"],
                rec["cache_write_tokens"],
            )

        # Top-level `provider` (omp lines only) wins over the heuristic — it
        # comes straight from the API response and avoids the heuristic
        # misclassifying "Organization/model" style Qwen models as lmstudio.
        provider = rec.get("provider") or _provider_from_model(model)
        task_id = _resolve_task_for_rec(rec, ts)

        event = ModelUsageEvent(
            id=uuid.uuid4(),
            agent_id=eff_agent_id,
            task_id=task_id,
            harness=harness,
            model=model,
            provider=provider,
            session_id=rec["session_id"],
            message_uuid=rec["uuid"],
            input_tokens=rec["input_tokens"],
            output_tokens=rec["output_tokens"],
            cache_read_tokens=rec["cache_read_tokens"],
            cache_write_tokens=rec["cache_write_tokens"],
            cost_usd=cost_usd,
            ts=ts,
            source_file=path,
        )

        # Idempotent insert: UNIQUE constraint as backstop (race condition
        # when two harvester runs execute in parallel).
        # Use a nested transaction (SAVEPOINT) so an IntegrityError only rolls
        # back this one row instead of losing the whole batch.
        try:
            async with session.begin_nested():
                session.add(event)
            new_events_count += 1
        except IntegrityError:
            logger.debug("harvest: duplicate uuid %s (UNIQUE conflict) — skipped", rec["uuid"])

    stats["new_events"] += new_events_count

    # Backfill pass: lines already harvested (dedup-skipped above) whose
    # event still has task_id IS NULL get re-attributed now that the task
    # workspace map may know about them (e.g. re-harvest after a task's
    # workspace_path was set). Orphaned events whose JSONL no longer exists
    # are simply never visited here and stay NULL — never guessed.
    if existing_untasked:
        for rec in records:
            event_id = existing_untasked.get(rec["uuid"])
            if event_id is None:
                continue
            ts = _parse_ts(rec["timestamp"])
            task_id = _resolve_task_for_rec(rec, ts)
            if task_id is None:
                continue
            await session.exec(
                update(ModelUsageEvent)
                .where(ModelUsageEvent.id == event_id)
                .values(task_id=task_id)
            )
            stats["backfilled_task_id"] += 1

    # Update harvest state
    total_lines = _count_lines(path)
    await _update_harvest_state(session, state_map, path, current_mtime, total_lines)


async def _update_harvest_state(
    session: AsyncSession,
    state_map: dict[str, ModelUsageHarvestState],
    path: str,
    mtime: float,
    total_lines: int,
) -> None:
    """Updates the harvest state for a file (upsert)."""
    from app.utils import utcnow

    state = state_map.get(path)
    if state is None:
        state = ModelUsageHarvestState(
            file_path=path,
            mtime=mtime,
            processed_lines=total_lines,
            updated_at=utcnow(),
        )
        session.add(state)
        state_map[path] = state
    else:
        state.mtime = mtime
        state.processed_lines = total_lines
        state.updated_at = utcnow()
        session.add(state)


def _count_lines(path: str) -> int:
    """Counts lines in a file (for offset state)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0
