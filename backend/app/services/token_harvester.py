"""Token Harvester — reads JSONL transcripts and writes model_usage_events.

Data sources:
  - ~/.mc/agents/{slug}/claude-config/projects/**/*.jsonl (cli-bridge + hermes,
    Claude Code JSONL schema — type=="assistant")
  - ~/.mc/agents/{slug}/omp-sessions/**/*.jsonl (omp headless harness, ADR-045 —
    e.g. Sparky on Qwen/Spark. Distinct schema — type=="message", see
    parse_transcript_line)
  - ~/.claude/projects/**/*.jsonl (boss-host + operator's private sessions → boss attribution heuristic!)
  - ~/.grok/logs/unified.jsonl (Grok Build CLI, ADR-066 host harness — append-only
    structured log, only "shell.turn.inference_done" lines carry usage; joined
    against ~/.grok/sessions/<urlenc-cwd>/<sid>/summary.json for model/cwd and
    .../prompt_history.jsonl for task_id, see _harvest_grok)
  - ~/.hermes/state.db (Hermes host harness — sqlite session ledger, never
    opened live; copied to a temp dir first, see _harvest_hermes)

Dedup key: top-level `uuid` (UNIQUE) for Claude Code lines. message.id has
1042+ collisions — NEVER dedupe on that! omp lines carry no top-level uuid at
all (only a collision-prone top-level 8-hex `id`) — their dedup key is
synthesized as ``{session_id}:{message.responseId or id}`` where session_id
comes from the JSONL filename (omp writes one file per session, no sessionId
field in the line itself) and responseId (a chatcmpl-* id from the backing
API) is preferred over the collision-prone short id when present.
Idempotent: can run over the same files any number of times.
Offset resume: harvest_state stores processed_lines → only reads new lines.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.model_usage import ModelPrice, ModelUsageEvent, ModelUsageHarvestState

# Extracts the MC dispatch task_id out of a "[MC DISPATCH] task_id=<uuid> ..."
# prompt (Grok prompt_history.jsonl, Hermes first user message).
_TASK_ID_RE = re.compile(r"task_id=([0-9a-f-]{36})")

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
    - ``"message"`` → omp schema (ADR-045 headless harness — model/provider/
      usage nested under message, camelCase usage keys, no top-level uuid).
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

    Real sample (Sparky/Qwen on Spark, 2026-07-15, verified in-container —
    everything except type/id/parentId/timestamp lives INSIDE ``message``,
    not top-level — an earlier revision of this function got that wrong from
    a misread sample and silently harvested 0 events)::

        {"type":"message","id":"74f7a91e","parentId":"54c8d3f0",
         "timestamp":"2026-07-15T16:29:37.102Z",
         "message":{"role":"assistant","content":[...],
           "api":"openai-completions","provider":"mc-openai",
           "model":"Qwen/Qwen3.6-35B-A3B-FP8",
           "usage":{"input":28848,"output":135,"cacheRead":0,"cacheWrite":0,
             "totalTokens":28983,
             "cost":{"input":0.004,...,"total":0.0042}},
           "stopReason":"toolUse","timestamp":1784132972177,
           "responseId":"chatcmpl-915e3d69480ffb2c", ...}}

    Filters:
    - Only when message.role == "assistant"
    - Only when message.usage is present
    - Only when a model is present (message.model, falls back to a top-level
      model if a future omp version ever puts it there)

    Dedup key: omp has no top-level uuid. ``message.responseId`` (a full
    chatcmpl-* id from the backing API) is far less collision-prone than the
    top-level ``id`` (8 hex, generated per omp turn, collides across
    sessions) — preferred when present, with ``id`` as fallback. Since one
    JSONL file == one omp session, the caller-supplied session_id namespaces
    it: ``{session_id}:{responseId or id}``.

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

    model = message.get("model") or d.get("model")
    if not model:
        return None

    short_id = d.get("id")
    if not short_id:
        return None
    dedup_id = message.get("responseId") or short_id

    sess = session_id or ""
    msg_uuid = f"{sess}:{dedup_id}"

    return {
        "uuid": msg_uuid,
        "msg_id": short_id,  # For debug only — NOT for dedup!
        "session_id": sess,
        "timestamp": d.get("timestamp", ""),
        "cwd": d.get("cwd", ""),
        "git_branch": d.get("gitBranch"),
        "model": model,
        "provider": message.get("provider") or d.get("provider"),
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


# ── Grok Build CLI source (ADR-066 host harness, Bench #18 PR1) ────────────


def parse_grok_line(line: str) -> dict[str, Any] | None:
    """Parses one line of ~/.grok/logs/unified.jsonl.

    Only ``msg == "shell.turn.inference_done"`` lines carry usage — the vast
    majority of unified.jsonl lines are unrelated structured-log noise and
    are filtered out here (same idea as parse_transcript_line's type dispatch).

    Real sample (verified against the live file, 2026-07-10)::

        {"ts":"2026-07-10T21:02:09.251Z","src":"shell","pid":41213,"lvl":"info",
         "sid":"019f4dd6-6505-7510-b05c-b6dfc47a2c2d","msg":"shell.turn.inference_done",
         "ctx":{"loop_index":1,"model_elapsed_ms":1493,"prompt_tokens":18609,
                "cached_prompt_tokens":6016,"completion_tokens":35,
                "reasoning_tokens":27,"tokens_per_sec":48.2}}

    Token math (OpenAI convention — cached_prompt_tokens is a SUBSET of
    prompt_tokens, reasoning_tokens is a SUBSET of completion_tokens):
        input_tokens = prompt_tokens - cached_prompt_tokens (floor 0)
        cache_read_tokens = cached_prompt_tokens
        cache_write_tokens = 0 (Grok/xAI has no cache-write billing concept)
        output_tokens = completion_tokens

    No model/cwd/task_id here — those need the sid→summary.json /
    sid→prompt_history.jsonl join, done once per harvest run by the caller.
    """
    try:
        d = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None

    if d.get("msg") != "shell.turn.inference_done":
        return None

    sid = d.get("sid")
    ts = d.get("ts")
    ctx = d.get("ctx")
    if not sid or not ts or not ctx:
        return None

    loop_index = ctx.get("loop_index")
    if loop_index is None:
        return None

    prompt_tokens = ctx.get("prompt_tokens", 0) or 0
    cached_prompt_tokens = ctx.get("cached_prompt_tokens", 0) or 0
    completion_tokens = ctx.get("completion_tokens", 0) or 0

    return {
        "uuid": f"grok:{sid}:{ts}:{loop_index}",
        "sid": sid,
        "timestamp": ts,
        "input_tokens": max(prompt_tokens - cached_prompt_tokens, 0),
        "output_tokens": completion_tokens,
        "cache_read_tokens": cached_prompt_tokens,
        "cache_write_tokens": 0,
    }


def harvest_grok_file(path: str, processed_lines: int = 0) -> list[dict[str, Any]]:
    """Reads unified.jsonl from `processed_lines` onward (offset resume,
    same convention as harvest_file — reuses model_usage_harvest_state)."""
    records: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i < processed_lines:
                    continue
                line = line.strip()
                if not line:
                    continue
                rec = parse_grok_line(line)
                if rec is not None:
                    records.append(rec)
    except OSError as e:
        logger.warning("harvest_grok_file(%s): OS error: %s", path, e)
    return records


def _build_grok_session_index(sessions_base: str) -> dict[str, dict[str, Any]]:
    """{sid: {"model": ..., "cwd": ...}} from every summary.json under
    sessions_base (``~/.grok/sessions/<urlenc-cwd>/<sid>/summary.json``).
    Built once per harvest run — cheap (globs, small JSON files) compared to
    re-globbing per event."""
    index: dict[str, dict[str, Any]] = {}
    base = Path(sessions_base)
    if not base.exists():
        return index
    for summary_path in base.glob("*/*/summary.json"):
        try:
            data = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        info = data.get("info") or {}
        sid = info.get("id") or summary_path.parent.name
        model = data.get("current_model_id")
        cwd = info.get("cwd")
        if not sid or not model:
            continue
        index[sid] = {"model": model, "cwd": cwd or ""}
    return index


def _extract_task_id(text_: str) -> uuid.UUID | None:
    """Extracts a ``task_id=<uuid>`` reference from dispatch-message text
    (Grok prompt_history.jsonl prompts, Hermes first user message). Returns
    None (never guesses) if the regex doesn't match or the match isn't a
    parseable UUID."""
    m = _TASK_ID_RE.search(text_ or "")
    if not m:
        return None
    try:
        return uuid.UUID(m.group(1))
    except ValueError:
        return None


def _build_grok_task_index(sessions_base: str) -> dict[str, uuid.UUID]:
    """{sid: task_id} from every prompt_history.jsonl under sessions_base
    (one file per cwd-dir, shared by all sessions in that cwd — lines are
    ``{"session_id": ..., "prompt": "[MC DISPATCH] task_id=<uuid> ..."}``)."""
    index: dict[str, uuid.UUID] = {}
    base = Path(sessions_base)
    if not base.exists():
        return index
    for history_path in base.glob("*/prompt_history.jsonl"):
        try:
            with open(history_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    sid = d.get("session_id")
                    prompt = d.get("prompt") or ""
                    if not sid or sid in index:
                        continue
                    task_id = _extract_task_id(prompt)
                    if task_id is not None:
                        index[sid] = task_id
        except OSError:
            continue
    return index


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


def _translate_agent_cwd(cwd: str, slug: str) -> str:
    """Rewrites a container-side cwd (``/workspace/...``) to the host path
    the DB actually stores on ``tasks.workspace_path``.

    cli-bridge agents write JSONL transcripts with the cwd they see inside
    their own container (``/workspace`` — the mount target of their
    ``~/.mc/workspaces/<slug>`` bind mount, see docker-compose.agents.yml).
    ``_resolve_task_for_rec`` compares against host paths, so without this
    rewrite every cli-bridge/sparky event's cwd match fails and task_id stays
    NULL forever. Inverse of ``dispatch._container_workspace_path``.

    Non-``/workspace`` cwds (boss-host lines, or anything already host-side)
    pass through unchanged. Anchored on the path boundary — ``/workspacefoo``
    is NOT ``/workspace`` (a naive ``startswith`` would wrongly match it).
    """
    m = re.match(r"^/workspace(/.*)?$", cwd)
    if not m:
        return cwd
    suffix = m.group(1) or ""
    return str(_host_home() / ".mc" / "workspaces" / slug) + suffix


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
    grok_log_path: str | None = None,
    grok_sessions_path: str | None = None,
    hermes_state_db_path: str | None = None,
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
    - grok_log_path / grok_sessions_path: Grok Build CLI sources (default:
      settings.grok_harvest_path / settings.grok_sessions_path, expanduser)
    - hermes_state_db_path: Hermes sqlite ledger (default:
      settings.hermes_state_db_path, expanduser)

    Returns:
        {"files_scanned": N, "new_events": M, "skipped_private": K,
         "backfilled_task_id": L, "grok_skipped_no_summary": G,
         "hermes_sessions_scanned": H}
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

    if grok_log_path is None:
        grok_log_path = _expand_harvest_path(
            getattr(app_settings, "grok_harvest_path", "~/.grok/logs/unified.jsonl")
        )
    if grok_sessions_path is None:
        grok_sessions_path = _expand_harvest_path(
            getattr(app_settings, "grok_sessions_path", "~/.grok/sessions")
        )
    if hermes_state_db_path is None:
        hermes_state_db_path = _expand_harvest_path(
            getattr(app_settings, "hermes_state_db_path", "~/.hermes/state.db")
        )

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
    grok_agent_id = agent_slug_map.get("grok")
    hermes_agent_id = agent_slug_map.get("hermes")

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
        "grok_skipped_no_summary": 0,
        "hermes_sessions_scanned": 0,
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
                    cwd_translate_slug=slug,
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

    # ── Grok source: ~/.grok/logs/unified.jsonl ─────────────────────────────
    if Path(grok_log_path).exists():
        await _process_grok_file(
            session=session,
            path=grok_log_path,
            sessions_base=grok_sessions_path,
            agent_id=grok_agent_id,
            all_prices=all_prices,
            state_map=state_map,
            stats=stats,
            task_workspace_map=task_workspace_map,
        )

    # ── Hermes source: ~/.hermes/state.db (sqlite) ──────────────────────────
    if Path(hermes_state_db_path).exists():
        await _harvest_hermes(
            session=session,
            state_db_path=hermes_state_db_path,
            agent_id=hermes_agent_id,
            all_prices=all_prices,
            stats=stats,
            task_workspace_map=task_workspace_map,
        )

    # Commit at the end
    try:
        await session.commit()
    except Exception as e:
        logger.error("run_harvest: commit error: %s", e)
        await session.rollback()

    logger.info(
        "run_harvest: files=%d new=%d skipped_private=%d backfilled_task_id=%d "
        "grok_skipped_no_summary=%d hermes_sessions_scanned=%d",
        stats["files_scanned"],
        stats["new_events"],
        stats["skipped_private"],
        stats["backfilled_task_id"],
        stats["grok_skipped_no_summary"],
        stats["hermes_sessions_scanned"],
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
    cwd_translate_slug: str | None = None,
) -> None:
    """Processes a single JSONL file (offset resume, batch insert).

    cwd_translate_slug: agent slug for container→host cwd rewrite (agent
    paths only — is_boss_path lines already carry a host-native cwd and
    never get translated).
    """
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
    if cwd_translate_slug and not is_boss_path:
        for rec in records:
            rec["cwd"] = _translate_agent_cwd(rec.get("cwd", ""), cwd_translate_slug)
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


async def _process_grok_file(
    session: AsyncSession,
    path: str,
    sessions_base: str,
    agent_id: Any | None,
    all_prices: list[ModelPrice],
    state_map: dict[str, ModelUsageHarvestState],
    stats: dict[str, int],
    task_workspace_map: dict[str, list[dict[str, Any]]] | None = None,
) -> None:
    """Processes ~/.grok/logs/unified.jsonl (offset resume, same mechanics
    as _process_jsonl_file — reuses model_usage_harvest_state keyed by this
    file's absolute path)."""
    stats["files_scanned"] += 1

    try:
        current_mtime = os.path.getmtime(path)
    except OSError:
        return

    state = state_map.get(path)
    if state and state.mtime == current_mtime:
        return

    processed_lines = state.processed_lines if state else 0
    records = harvest_grok_file(path, processed_lines)
    if not records:
        total_lines = _count_lines(path)
        await _update_harvest_state(session, state_map, path, current_mtime, total_lines)
        return

    # Batch dedup (same pattern as _process_jsonl_file)
    candidate_uuids = [r["uuid"] for r in records]
    existing_result = await session.exec(
        select(ModelUsageEvent.message_uuid).where(
            ModelUsageEvent.message_uuid.in_(candidate_uuids)
        )
    )
    existing_uuids: set[str] = {row for row in existing_result.all()}
    new_records = [r for r in records if r["uuid"] not in existing_uuids]

    session_index = _build_grok_session_index(sessions_base)
    task_index = _build_grok_task_index(sessions_base)
    task_workspace_map = task_workspace_map or {}

    new_events_count = 0
    for rec in new_records:
        sess_info = session_index.get(rec["sid"])
        if sess_info is None:
            # No summary.json for this sid → no model, cannot attribute cost.
            # Never guess a model — skip, but count it (visible in stats).
            stats["grok_skipped_no_summary"] += 1
            continue

        model = sess_info["model"]
        cwd = sess_info.get("cwd") or ""
        ts = _parse_ts(rec["timestamp"])

        task_id = task_index.get(rec["sid"])
        if task_id is None:
            norm_cwd = _normalize_workspace_path(cwd)
            candidates = task_workspace_map.get(norm_cwd)
            if candidates:
                task_id = _resolve_task_id(candidates, None, ts)

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

        event = ModelUsageEvent(
            id=uuid.uuid4(),
            agent_id=agent_id,
            task_id=task_id,
            harness="grok",
            model=model,
            provider="xai",
            session_id=rec["sid"],
            message_uuid=rec["uuid"],
            input_tokens=rec["input_tokens"],
            output_tokens=rec["output_tokens"],
            cache_read_tokens=rec["cache_read_tokens"],
            cache_write_tokens=rec["cache_write_tokens"],
            cost_usd=cost_usd,
            ts=ts,
            source_file=path,
        )
        try:
            async with session.begin_nested():
                session.add(event)
            new_events_count += 1
        except IntegrityError:
            logger.debug("harvest(grok): duplicate uuid %s — skipped", rec["uuid"])

    stats["new_events"] += new_events_count

    total_lines = _count_lines(path)
    await _update_harvest_state(session, state_map, path, current_mtime, total_lines)


def _copy_hermes_db(state_db_path: str) -> tuple[str, str]:
    """Copies state.db (+ -wal/-shm if present + readable) to a fresh temp
    dir and returns (tmp_dir, copied_db_path). NEVER open the live file —
    Hermes writes to it continuously and a live open risks a lock/corrupt
    read. Missing/stale WAL is tolerated (db-only fallback, e.g. after a
    docker-compose individual-file mount recreates a stale -wal/-shm)."""
    tmp_dir = tempfile.mkdtemp(prefix="mc_hermes_harvest_")
    dst_db = os.path.join(tmp_dir, "state.db")
    shutil.copy2(state_db_path, dst_db)
    for suffix in ("-wal", "-shm"):
        src = state_db_path + suffix
        if os.path.exists(src):
            try:
                shutil.copy2(src, os.path.join(tmp_dir, "state.db" + suffix))
            except OSError as e:
                logger.warning(
                    "_copy_hermes_db: could not copy %s (falling back to db-only): %s",
                    src, e,
                )
    return tmp_dir, dst_db


async def _harvest_hermes(
    session: AsyncSession,
    state_db_path: str,
    agent_id: Any | None,
    all_prices: list[ModelPrice],
    stats: dict[str, int],
    task_workspace_map: dict[str, list[dict[str, Any]]] | None = None,
    *,
    cutoff_days: int = 30,
) -> None:
    """Reads Hermes' sqlite session ledger and inserts one ModelUsageEvent
    per finished session. No file-offset state (the DB mutates continuously,
    unlike an append-only JSONL) — dedup is purely message_uuid-based, so a
    re-run over the same window inserts 0 new rows."""
    task_workspace_map = task_workspace_map or {}
    cutoff_ts = (datetime.now(timezone.utc) - timedelta(days=cutoff_days)).timestamp()

    tmp_dir, copied_db = _copy_hermes_db(state_db_path)
    try:
        conn = sqlite3.connect(f"file:{copied_db}?mode=ro", uri=True)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, model, input_tokens, output_tokens, cache_read_tokens, "
                "cache_write_tokens, cwd, git_branch, ended_at FROM sessions "
                "WHERE ended_at IS NOT NULL AND started_at > ?",
                (cutoff_ts,),
            ).fetchall()

            stats["hermes_sessions_scanned"] += len(rows)
            if not rows:
                return

            candidate_uuids = [f"hermes:{row['id']}" for row in rows]
            existing_result = await session.exec(
                select(ModelUsageEvent.message_uuid).where(
                    ModelUsageEvent.message_uuid.in_(candidate_uuids)
                )
            )
            existing = {row for row in existing_result.all()}

            new_events_count = 0
            for row in rows:
                message_uuid = f"hermes:{row['id']}"
                if message_uuid in existing:
                    continue

                model = row["model"] or ""
                ts = datetime.fromtimestamp(row["ended_at"], tz=timezone.utc)

                first_user_msg = conn.execute(
                    "SELECT content FROM messages WHERE session_id = ? AND role = 'user' "
                    "ORDER BY timestamp ASC LIMIT 1",
                    (row["id"],),
                ).fetchone()
                task_id = (
                    _extract_task_id(first_user_msg["content"])
                    if first_user_msg and first_user_msg["content"]
                    else None
                )

                if task_id is None:
                    norm_cwd = _normalize_workspace_path(row["cwd"] or "")
                    candidates = task_workspace_map.get(norm_cwd)
                    if candidates:
                        task_id = _resolve_task_id(candidates, row["git_branch"], ts)

                price_info = match_price(model, ts, all_prices) if model else None
                cost_usd: float | None = None
                if price_info is not None:
                    cost_usd = _compute_cost_usd(
                        price_info,
                        row["input_tokens"] or 0,
                        row["output_tokens"] or 0,
                        row["cache_read_tokens"] or 0,
                        row["cache_write_tokens"] or 0,
                    )

                event = ModelUsageEvent(
                    id=uuid.uuid4(),
                    agent_id=agent_id,
                    task_id=task_id,
                    harness="hermes",
                    model=model,
                    provider=_provider_from_model(model) if model else None,
                    session_id=row["id"],
                    message_uuid=message_uuid,
                    input_tokens=row["input_tokens"] or 0,
                    output_tokens=row["output_tokens"] or 0,
                    cache_read_tokens=row["cache_read_tokens"] or 0,
                    cache_write_tokens=row["cache_write_tokens"] or 0,
                    cost_usd=cost_usd,
                    ts=ts,
                    source_file=state_db_path,
                )
                try:
                    async with session.begin_nested():
                        session.add(event)
                    new_events_count += 1
                except IntegrityError:
                    logger.debug(
                        "harvest(hermes): duplicate uuid %s — skipped", message_uuid
                    )

            stats["new_events"] += new_events_count
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
