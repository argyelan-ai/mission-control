"""Token Harvester — liest JSONL-Transkripte und schreibt model_usage_events.

Datenquellen:
  - ~/.mc/agents/{slug}/claude-config/projects/**/*.jsonl (cli-bridge + sparky + hermes)
  - ~/.claude/projects/**/*.jsonl (boss-host + private Sessions des Operators → Boss-Attribution-Heuristik!)

Dedup-Key: top-level `uuid` (UNIQUE). message.id hat 1042+ Kollisionen — NIEMALS dedupen!
Idempotent: beliebig oft ueber dieselben Dateien laufen.
Offset-Resume: harvest_state speichert processed_lines → nur neue Zeilen lesen.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.model_usage import ModelPrice, ModelUsageEvent, ModelUsageHarvestState

logger = logging.getLogger("mc.token_harvester")

# ── MC-Workspace-Indikatoren fuer Boss-Attribution ────────────────────────
# Pfade/Branches die auf MC-Kontext hinweisen → Boss zuschreiben
_MC_CWD_MARKERS = [
    "mission-control",  # Haupt-Repo
    "/.mc/",            # Agent-Workspaces unter ~/.mc/
]
_MC_BRANCH_PREFIX = "task/"


# ── Pure Helper-Funktionen (testbar ohne DB) ──────────────────────────────


def parse_transcript_line(line: str) -> dict[str, Any] | None:
    """Parst eine JSONL-Zeile aus einem Claude-Code-Transkript.

    Filtert:
    - Nur type=assistant
    - Nur wenn message.usage vorhanden
    - Nur wenn message.model vorhanden und NICHT '<synthetic>'
    - Nur wenn top-level uuid vorhanden

    Gibt ein normalisiertes Dict zurueck oder None wenn die Zeile gefiltert wird.
    """
    try:
        d = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None

    if d.get("type") != "assistant":
        return None

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

    # Cache-Tokens (optional, default 0)
    cache_read = usage.get("cache_read_input_tokens", 0) or 0
    # cache_creation summiert ephemeral_5m + ephemeral_1h wenn vorhanden
    cache_write = usage.get("cache_creation_input_tokens", 0) or 0

    return {
        "uuid": msg_uuid,
        "msg_id": message.get("id"),  # Nur fuer Debug — NICHT fuer Dedup!
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


def harvest_file(path: str, processed_lines: int = 0) -> list[dict[str, Any]]:
    """Liest eine JSONL-Datei ab `processed_lines` und gibt geparste Records zurueck.

    Zeilen die parse_transcript_line filtert (user, synthetic, etc.) werden
    uebersprungen. Die Dedup-Logik (gleiche uuid) liegt beim DB-Insert — harvest_file
    gibt alle geparsten Records ungefiltert zurueck.

    Args:
        path: Absoluter Pfad zur JSONL-Datei.
        processed_lines: Anzahl bereits gelesener Zeilen (Offset-Resume).

    Returns:
        Liste geparster Records (koennen gleiche uuid enthalten → DB macht Dedup).
    """
    records: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i < processed_lines:
                    continue
                line = line.strip()
                if not line:
                    continue
                rec = parse_transcript_line(line)
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
    """Findet den besten Preis fuer ein Modell zum Zeitpunkt ts.

    Matching-Logik:
    1. Nur Preise mit valid_from <= ts
    2. fnmatch-Glob auf model_pattern
    3. Hoehere priority gewinnt; bei gleicher priority: neuerer valid_from gewinnt

    Gibt ein Dict mit den Preisfeldern zurueck oder None wenn kein Match.
    """
    candidates: list[ModelPrice] = []
    for price in prices:
        # Zeitfilter: nur Preise die zum Zeitpunkt ts galten
        valid_from = price.valid_from
        if valid_from.tzinfo is None:
            valid_from = valid_from.replace(tzinfo=timezone.utc)
        ts_aware = ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)
        if valid_from > ts_aware:
            continue
        # Glob-Match
        if fnmatch.fnmatch(model, price.model_pattern):
            candidates.append(price)

    if not candidates:
        return None

    # Sortierung: priority DESC, valid_from DESC (neuester zuerst)
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
    """Berechnet cost_usd aus Preisinformationen und Token-Counts."""
    return (
        input_tokens * price_info["input_per_mtok"]
        + output_tokens * price_info["output_per_mtok"]
        + cache_read_tokens * price_info["cache_read_per_mtok"]
        + cache_write_tokens * price_info["cache_write_per_mtok"]
    ) / 1_000_000.0


def _should_attribute_boss_path(cwd: str, git_branch: str | None) -> bool:
    """Entscheidet ob eine ~/.claude-Zeile dem Boss zugeschrieben werden soll.

    Boss-Kriterien (OR):
    1. cwd enthaelt einen MC-Workspace-Marker (mission-control, /.mc/)
    2. gitBranch beginnt mit 'task/'

    ALLES ANDERE → private Session → SKIP.
    """
    if git_branch and git_branch.startswith(_MC_BRANCH_PREFIX):
        return True
    for marker in _MC_CWD_MARKERS:
        if marker in cwd:
            return True
    return False


def _harness_from_slug(slug: str) -> str:
    """Leitet den Harness-Typ vom Agent-Slug ab."""
    if slug == "sparky":
        return "sparky"
    # Host-Agents
    if slug in ("hermes", "boss-host", "boss", "jarvis"):
        return "host"
    return "cli-bridge"


def _provider_from_model(model: str) -> str:
    """Leitet den Provider-Namen vom Modell-String ab (heuristisch)."""
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
    """Parst ISO-8601 Timestamp zu aware datetime."""
    try:
        # Python 3.11+ fromisoformat versteht 'Z' als UTC
        if ts_str.endswith("Z"):
            ts_str = ts_str[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, AttributeError):
        return datetime.now(timezone.utc)


# ── Haupt-Harvest-Logik ────────────────────────────────────────────────────


def _host_home() -> Path:
    """Host-HOME — im Container via HOME_HOST (PR #137-Muster), sonst expanduser.

    Die Transkript-Mounts liegen unter dem absoluten HOST-Pfad
    (/Users/.../.mc, /Users/.../.claude); ~ im Container zeigt auf /home/mcuser.
    """
    return Path(os.environ.get("HOME_HOST") or Path.home())


def _expand_harvest_path(p: str) -> str:
    if p.startswith("~"):
        return str(_host_home() / p.lstrip("~/").lstrip("/"))
    return str(Path(p).expanduser())


def _slugify_agent_name(name: str) -> str:
    """Gleiche Slug-Konvention wie docker_agent_sync._agent_slug."""
    return name.lower().replace(" ", "-")


async def _build_agent_slug_map(session: AsyncSession) -> dict[str, Any]:
    """{slug: agent_id} aus der agents-Tabelle — Default-Attribution."""
    from app.models import Agent

    result = await session.exec(select(Agent))
    return {_slugify_agent_name(a.name): a.id for a in result.all()}


async def run_harvest(
    session: AsyncSession,
    *,
    agent_base_paths: list[str] | None = None,
    boss_base_paths: list[str] | None = None,
    agent_slug_map: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Scannt alle JSONL-Dateien, parst Assistant-Zeilen und insertiert Events.

    Konfigurierbar ueber:
    - agent_base_paths: Pfade zu ~/.mc/agents-aehnlichen Verzeichnissen
                        (Standard: settings.token_harvest_paths, expanduser)
    - boss_base_paths: Pfade zu ~/.claude/projects-aehnlichen Verzeichnissen
    - agent_slug_map: {slug: agent_id} zum Agent-Lookup (optional)

    Returns:
        {"files_scanned": N, "new_events": M, "skipped_private": K}
    """
    from app.config import settings as app_settings

    # Default-Pfade aus Settings (expanduser)
    if agent_base_paths is None:
        harvest_paths = getattr(app_settings, "token_harvest_paths", [
            "~/.mc/agents",
        ])
        agent_base_paths = [_expand_harvest_path(p) for p in harvest_paths]

    if boss_base_paths is None:
        boss_base_paths = [str(_host_home() / ".claude/projects")]

    if agent_slug_map is None:
        # Default: Attribution aus der agents-Tabelle (slug = name-basiert)
        agent_slug_map = await _build_agent_slug_map(session)

    # Boss-Agent fuer ~/.claude-Attribution (Host-Agent, Slug beginnt mit "boss")
    boss_agent_id = next(
        (aid for slug, aid in agent_slug_map.items() if slug.startswith("boss")),
        None,
    )

    # Preise einmal laden (fuer Kosten-Berechnung)
    prices_result = await session.exec(select(ModelPrice))
    all_prices: list[ModelPrice] = list(prices_result.all())

    # Harvest-State laden (alle bekannten Dateien)
    state_result = await session.exec(select(ModelUsageHarvestState))
    state_map: dict[str, ModelUsageHarvestState] = {
        s.file_path: s for s in state_result.all()
    }

    stats = {"files_scanned": 0, "new_events": 0, "skipped_private": 0}

    # ── Agenten-Pfade: ~/.mc/agents/{slug}/claude-config/projects/**/*.jsonl ──
    for base_str in agent_base_paths:
        base = Path(base_str)
        if not base.exists():
            continue
        # Glob: {slug}/claude-config/projects/**/*.jsonl UND subagents/*.jsonl
        for jsonl_path in sorted(base.glob("*/claude-config/projects/**/*.jsonl")):
            # Slug aus dem Pfad-Segment direkt unter base
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
            )

    # ── Boss-Pfade: ~/.claude/projects/**/*.jsonl ──────────────────────────
    for base_str in boss_base_paths:
        base = Path(base_str)
        if not base.exists():
            continue
        for jsonl_path in sorted(base.glob("**/*.jsonl")):
            await _process_jsonl_file(
                session=session,
                path=str(jsonl_path),
                agent_id=boss_agent_id,  # greift nur fuer MC-attribuierte Zeilen
                harness="host",
                is_boss_path=True,
                all_prices=all_prices,
                state_map=state_map,
                stats=stats,
                # Boss-agent_id wird spaeter nachgeladen wenn noetig
            )

    # Commit am Ende
    try:
        await session.commit()
    except Exception as e:
        logger.error("run_harvest: commit error: %s", e)
        await session.rollback()

    logger.info(
        "run_harvest: files=%d new=%d skipped_private=%d",
        stats["files_scanned"],
        stats["new_events"],
        stats["skipped_private"],
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
) -> None:
    """Verarbeitet eine einzelne JSONL-Datei (Offset-Resume, Batch-Insert)."""
    stats["files_scanned"] += 1

    try:
        current_mtime = os.path.getmtime(path)
    except OSError:
        return

    # mtime-Skip: Datei unveraendert → ueberspringen
    state = state_map.get(path)
    if state and state.mtime == current_mtime:
        return  # Nichts neues in dieser Datei

    processed_lines = state.processed_lines if state else 0

    # Zeilen lesen (ab Offset)
    records = harvest_file(path, processed_lines)
    if not records:
        # Datei wurde veraendert aber keine neuen validen Zeilen → State updaten
        total_lines = _count_lines(path)
        await _update_harvest_state(session, state_map, path, current_mtime, total_lines)
        return

    # Batch-Dedup: Welche uuids sind schon in der DB?
    candidate_uuids = [r["uuid"] for r in records]
    existing_result = await session.exec(
        select(ModelUsageEvent.message_uuid).where(
            ModelUsageEvent.message_uuid.in_(candidate_uuids)
        )
    )
    existing_uuids: set[str] = set(existing_result.all())

    new_records = [r for r in records if r["uuid"] not in existing_uuids]

    # Fuer Boss-Pfade: Attribution pro Zeile entscheiden
    new_events_count = 0
    for rec in new_records:
        if is_boss_path:
            cwd = rec.get("cwd", "")
            git_branch = rec.get("git_branch")
            if not _should_attribute_boss_path(cwd, git_branch):
                stats["skipped_private"] += 1
                continue
            # Boss: durchgereichte boss_agent_id (None wenn kein Boss-Agent existiert)
            eff_agent_id = agent_id
        else:
            eff_agent_id = agent_id

        # Preis-Matching
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

        provider = _provider_from_model(model)

        event = ModelUsageEvent(
            id=uuid.uuid4(),
            agent_id=eff_agent_id,
            task_id=None,
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

        # Idempotenter Insert: UNIQUE-Constraint als Backstop (Race condition
        # wenn zwei Harvester-Laeufe parallel laufen).
        # Nutze Nested Transaction (SAVEPOINT) um bei IntegrityError nur diesen
        # einen Row zurueckzurollen ohne den ganzen Batch zu verlieren.
        try:
            async with session.begin_nested():
                session.add(event)
            new_events_count += 1
        except IntegrityError:
            logger.debug("harvest: duplicate uuid %s (UNIQUE conflict) — skipped", rec["uuid"])

    stats["new_events"] += new_events_count

    # Harvest-State updaten
    total_lines = _count_lines(path)
    await _update_harvest_state(session, state_map, path, current_mtime, total_lines)


async def _update_harvest_state(
    session: AsyncSession,
    state_map: dict[str, ModelUsageHarvestState],
    path: str,
    mtime: float,
    total_lines: int,
) -> None:
    """Aktualisiert den Harvest-State fuer eine Datei (upsert)."""
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
    """Zaehlt Zeilen in einer Datei (fuer Offset-State)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0
