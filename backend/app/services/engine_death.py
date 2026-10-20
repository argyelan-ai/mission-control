"""Engine death — the record, the sentence and the task comments (ADR-096 §11).

The live case (19.10.2026): a recipe's memory guard killed the DeepSeek engine
(container Exited 137). MC said ``runtime.unreachable`` 3 min 53 s later,
without a cause, three times — once per row sharing the Spark's ``:8000``.

The DECISION lives in the watcher, next to the mechanisms ADR-096 §11 extends
(``runtime_watcher._probe_one`` → ``_maybe_declare_death``: the connection-
refused fast path plus the row's own anchor; the explanation reuses
``_read_container_logs`` / ``_last_error_line`` / ``CRASH_LOOP_LOG_PATTERNS``).
Planned silence is ``runtime_grace``'s durable stop intent (§11.1). This module
only holds what both need and nobody had yet:

- the death record in Redis (``RedisKeys.runtime_death``), mirrored by the
  watcher into the live snapshot's EXISTING fields ``status="failed"`` +
  ``reason`` (as the crash-loop stop writes them) plus ``reason_code`` / ``box``
  / ``at`` — no new status, no second reason field;
- the reason sentence in German and English (§11.4);
- one system comment on every task in progress of an agent on the dead port
  (§11.5). Work is NOT moved: it keeps its status, delivery waits.

No restart here: that stays ``_maybe_auto_recover`` (§11.6).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.runtime import Runtime
from app.redis_client import RedisKeys

logger = logging.getLogger(__name__)

# ADR-096 §11.3 — the only reason codes. ``kernel_oom`` and ``member_down``
# need node-agent facts that do not exist yet (kernel OOM report; member
# anchors in desired state, PR 6); they are listed so the UI knows them, and
# never guessed.
GUARD = "guard"
OOM = "oom"
KERNEL_OOM = "kernel_oom"
EXIT = "exit"
MEMBER_DOWN = "member_down"
CRASH_LOOP = "crash_loop"
UNKNOWN = "unknown"
REASON_CODES = (GUARD, OOM, KERNEL_OOM, EXIT, MEMBER_DOWN, CRASH_LOOP, UNKNOWN)

#: Built-in death markers (§11.3), appended to ``CRASH_LOOP_LOG_PATTERNS``.
#: Patterns explain, they never decide. A recipe's own markers
#: (``requirements.death_markers``) join here once recipes carry
#: ``requirements`` (ADR-096 PR 6) — until then a guard kill reads as
#: ``exit`` 137, never as an invented ``guard``.
DEATH_LOG_PATTERNS = ("CUDA out of memory", "NCCL error", "Killed")
_OOM_PATTERNS = ("CUDA out of memory",)

#: A death record outlives a weekend: the card still says on Monday why the
#: box is dark. It goes earlier on the first answering probe, a start or a stop.
DEATH_TTL = 7 * 24 * 3600
#: Endpoint-wide "one death / one alarm per outage" keys (§11.2/§11.4).
ENDPOINT_OUTAGE_TTL = 24 * 3600


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clock(at: str | None) -> str | None:
    """``"2026-10-19T18:44:25Z"`` → ``"20:44"`` in the operator's zone."""
    if not at:
        return None
    try:
        dt = datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        tz = ZoneInfo(settings.usage_timezone or "UTC")
    except Exception:  # noqa: BLE001 — a bad TZ setting must not cost the alarm
        tz = ZoneInfo("UTC")
    return dt.astimezone(tz).strftime("%H:%M")


def classify(*, oom_killed: bool | None, exit_code: int | None, marker: str | None,
             process_gone: bool = False) -> str:
    """Reason code from what the box said. Order: OOMKilled, CUDA OOM in the
    log, an exit code or a host process that is gone (``exit``), else
    ``unknown`` (e.g. the container was removed and left no trace)."""
    if oom_killed:
        return OOM
    if marker in _OOM_PATTERNS:
        return OOM
    if exit_code is not None or process_gone:
        return EXIT
    return UNKNOWN


def sentences(code: str, *, display: str, box: str | None, at: str | None,
              exit_code: int | None, marker: str | None) -> tuple[str, str]:
    """(German, English) — "<runtime> stopped on <box>: <cause> at <time>"."""
    where_de = f" auf {box}" if box else ""
    where_en = f" on {box}" if box else ""
    clock = _clock(at)
    when_de = f" um {clock}" if clock else ""
    when_en = f" at {clock}" if clock else ""
    if code == OOM:
        cause_de, cause_en = "kein Speicher mehr (OOM)", "out of memory (OOM)"
    elif code == GUARD:
        cause_de = "der Speicherwächter des Rezepts hat die Engine beendet"
        cause_en = "the recipe's memory guard ended the engine"
    elif code == EXIT and exit_code == 137:
        cause_de = "Engine hart beendet (Exit 137, SIGKILL — z. B. ein Speicherwächter)"
        cause_en = "engine killed (exit 137, SIGKILL — e.g. a memory guard)"
    elif code == EXIT and exit_code is not None:
        cause_de = f"Engine beendet (Exit {exit_code})"
        cause_en = f"engine exited (exit {exit_code})"
    elif code == EXIT:
        cause_de, cause_en = "Engine-Prozess läuft nicht mehr", "engine process is gone"
    elif code == CRASH_LOOP:
        cause_de, cause_en = "Neustart-Schleife, von MC angehalten", "restart loop, stopped by MC"
    else:
        cause_de, cause_en = "Grund unbekannt", "reason unknown"
    if marker and code != OOM:
        cause_de += f" — Log: „{marker}“"
        cause_en += f" — log: “{marker}”"
    return (
        f"{display} gestoppt{where_de}: {cause_de}{when_de}",
        f"{display} stopped{where_en}: {cause_en}{when_en}",
    )


# ── Record ───────────────────────────────────────────────────────────────

async def get_death(redis, slug: str) -> dict | None:
    try:
        raw = await redis.get(RedisKeys.runtime_death(slug))
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        doc = json.loads(raw)
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


async def claim_death(redis, runtime: Runtime, doc: dict) -> bool:
    """Store the record — once per runtime and outage, once per ENDPOINT
    outage (one engine death = one death, whatever shares its port).
    Returns False when another row/worker already holds this outage."""
    endpoint = (runtime.endpoint or runtime.slug).rstrip("/")
    try:
        first = bool(await redis.set(
            RedisKeys.runtime_endpoint_death(endpoint), runtime.slug,
            nx=True, ex=ENDPOINT_OUTAGE_TTL,
        ))
        if not first:
            owner = await redis.get(RedisKeys.runtime_endpoint_death(endpoint))
            if isinstance(owner, bytes):
                owner = owner.decode()
            if owner != runtime.slug:
                return False
        return bool(await redis.set(
            RedisKeys.runtime_death(runtime.slug), json.dumps(doc), nx=True, ex=DEATH_TTL,
        ))
    except Exception as exc:  # noqa: BLE001 — Redis down: report anyway, once per tick
        logger.debug("engine-death: record not stored for %s: %s", runtime.slug, exc)
        return True


async def endpoint_had_death(redis, endpoint: str | None) -> bool:
    if not endpoint:
        return False
    try:
        return bool(await redis.get(RedisKeys.runtime_endpoint_death(endpoint)))
    except Exception:  # noqa: BLE001
        return False


async def endpoint_answered(redis, runtime: Runtime) -> None:
    """The first answer on a port ends its outage: the death record of THIS
    row and the endpoint's one-death / one-alarm keys go."""
    try:
        await redis.delete(RedisKeys.runtime_death(runtime.slug))
        if runtime.endpoint:
            await redis.delete(
                RedisKeys.runtime_endpoint_death(runtime.endpoint),
                RedisKeys.runtime_endpoint_alarm(runtime.endpoint),
            )
    except Exception:  # noqa: BLE001
        pass


async def claim_endpoint_alarm(redis, runtime: Runtime) -> bool:
    """One plain ``runtime.unreachable`` per endpoint outage (slot row + recipe
    rows on :8000 add none). Redis down → True (better twice than never)."""
    endpoint = (runtime.endpoint or runtime.slug).rstrip("/")
    try:
        return bool(await redis.set(
            RedisKeys.runtime_endpoint_alarm(endpoint), runtime.slug,
            nx=True, ex=ENDPOINT_OUTAGE_TTL,
        ))
    except Exception:  # noqa: BLE001
        return True


def live_fields(doc: dict) -> dict:
    """The record in the live snapshot's existing shape (crash-loop stop
    writes ``status="failed"`` + ``reason`` the same way)."""
    return {
        "status": "failed",
        "reason": doc.get("reason"),
        "reason_en": doc.get("reason_en"),
        "reason_code": doc.get("reason_code"),
        "box": doc.get("box"),
        "at": doc.get("at"),
        "exit_code": doc.get("exit_code"),
        "log_line": doc.get("log_line"),
        "autostart": doc.get("autostart"),
        "tasks_commented": doc.get("tasks_commented", 0),
    }


# ── Work does not fail silently (§11.5) ──────────────────────────────────

async def comment_running_tasks(
    session: AsyncSession, runtime: Runtime, *, reason_de: str
) -> list[str]:
    """One system comment on every task in progress of every agent that talks
    to the dead PORT — bound to the dead row or to any row on the same
    endpoint (live, the local agents hang on the box's slot row). Status,
    delivery and attempt stay exactly as they are: the work waits for the
    model, it is not handed elsewhere. Called once per death (the record is
    claimed first), so each card gets one comment per death."""
    from app.models.agent import Agent
    from app.models.task import Task, TaskComment

    row_ids = [runtime.id]
    if runtime.endpoint:
        row_ids = list((await session.exec(
            select(Runtime.id).where(Runtime.endpoint == runtime.endpoint)
        )).all()) or [runtime.id]
    agent_ids = list((await session.exec(
        select(Agent.id).where(Agent.runtime_id.in_(row_ids))
    )).all())
    if not agent_ids:
        return []
    task_ids = list((await session.exec(
        select(Task.id).where(
            Task.assigned_agent_id.in_(agent_ids),
            Task.status == "in_progress",
        )
    )).all())
    for task_id in task_ids:
        session.add(TaskComment(
            task_id=task_id,
            author_type="system",
            comment_type="system",
            content=(
                f"Modell ausgefallen: {reason_de}. Der Task bleibt in Arbeit und "
                "wartet, bis das Modell wieder läuft — bisherige Arbeit bleibt "
                "erhalten. Neu starten: Runtimes-Seite."
            ),
        ))
    if task_ids:
        await session.commit()
    return [str(t) for t in task_ids]
