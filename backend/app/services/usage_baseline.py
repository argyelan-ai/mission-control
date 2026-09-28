"""E0 token baseline — tokens and list-price cost per ISO week × source.

Reproduces the hand-made table in docs/metrics/2026-09-baseline.md §1 as a
query, so the baseline stops being a one-off. Every bucket is derived from
columns the harvester already writes; nothing is guessed:

  operator        host-CLI rows from ``~/.claude/projects/<dir>`` whose <dir>
                  is not the lead checkout. The harvester books these on the
                  lead agent's id (token_harvester._should_attribute_boss_path),
                  so ``agent_id`` alone mixes both — ``source_file`` splits them.
  lead            every other row of the lead agent (slug ``boss*``, same rule
                  as the harvester): its checkout dir and its own config dir.
  agents:<h>      other persistent agents, by the harness recorded on the row
                  (cli-bridge, sparky, grok, hermes, host, ...).
  heads:<l>       head runs (harness ``head-*``), by the run's recorded
                  locality — ``unknown`` when the spec had none.
  unattributed    no agent and not a head.

Local vs cloud: a row's own ``locality`` wins (heads); otherwise a model
counts as local only when a host-bound runtime serves it and no cloud runtime
does. Unknown or ambiguous models count as cloud, so the local share is never
overstated.

Cost is the stored ``cost_usd`` — the list-price equivalent from
``model_prices`` at harvest time (subscriptions are not invoices). Rows
without a price are counted in ``unpriced_events``.
"""

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import case, func, literal
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.model_usage import ModelUsageEvent
from app.models.runtime import Runtime
from app.services.token_harvester import _host_home, _slugify_agent_name
from app.services.transcript_chat import encode_cwd
from app.utils import ensure_aware, utcnow

MAX_WEEKS = 26
_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


def iso_week_label(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def week_starts(weeks: int, now: datetime) -> list[date]:
    """Mondays of the last ``weeks`` ISO weeks, oldest first, current week last."""
    weeks = max(1, min(int(weeks), MAX_WEEKS))
    today = ensure_aware(now).astimezone(timezone.utc).date()
    monday = today - timedelta(days=today.weekday())
    return [monday - timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]


def _lead_checkout_dir() -> str:
    # Same checkout the Sessions chat resolves for the lead (transcript_chat).
    return encode_cwd(str(_host_home() / ".mc" / "checkouts" / "mission-control"))


async def _model_locality(session: AsyncSession) -> set[str]:
    """Lower-cased model ids served only by host-bound (local) runtimes."""
    local: set[str] = set()
    cloud: set[str] = set()
    for rt in (await session.exec(select(Runtime))).all():
        for ident in (rt.model_identifier, rt.lms_identifier):
            if ident:
                (local if rt.host_id is not None else cloud).add(ident.lower())
    return local - cloud


async def _lead_ids(session: AsyncSession) -> set:
    agents = (await session.exec(select(Agent.id, Agent.name))).all()
    return {aid for aid, name in agents if _slugify_agent_name(name).startswith("boss")}


def _empty() -> dict:
    return {"events": 0, **{f: 0 for f in _TOKEN_FIELDS}, "total_tokens": 0, "cost_usd": 0.0,
            "unpriced_events": 0, "local_tokens": 0, "local_output_tokens": 0}


def _finish(bucket: dict) -> dict:
    total, out = bucket["total_tokens"], bucket["output_tokens"]
    bucket["cost_usd"] = round(bucket["cost_usd"], 4)
    bucket["local_share"] = bucket["local_tokens"] / total if total else None
    bucket["local_output_share"] = bucket["local_output_tokens"] / out if out else None
    return bucket


async def compute_weekly_baseline(session: AsyncSession, *, weeks: int = 6, now: datetime | None = None) -> dict:
    now = ensure_aware(now) if now is not None else utcnow()
    mondays = week_starts(weeks, now)
    start = datetime.combine(mondays[0], datetime.min.time(), tzinfo=timezone.utc)

    lead_dir = _lead_checkout_dir()
    is_operator = case(
        (
            (ModelUsageEvent.harness == "host")
            & ModelUsageEvent.source_file.like("%/.claude/projects/%")
            & ~ModelUsageEvent.source_file.like(f"%/.claude/projects/{lead_dir}/%"),
            literal(True),
        ),
        else_=literal(False),
    ).label("is_operator")
    day = func.date(ModelUsageEvent.ts).label("day")
    unpriced = func.sum(case((ModelUsageEvent.cost_usd.is_(None), 1), else_=0)).label("unpriced")

    rows = (
        await session.exec(
            select(
                day,
                ModelUsageEvent.harness,
                ModelUsageEvent.agent_id,
                ModelUsageEvent.locality,
                ModelUsageEvent.model,
                is_operator,
                func.count(ModelUsageEvent.id).label("events"),
                *(func.sum(getattr(ModelUsageEvent, f)).label(f) for f in _TOKEN_FIELDS),
                func.sum(ModelUsageEvent.cost_usd).label("cost"),
                unpriced,
            )
            .where(ModelUsageEvent.ts >= start)
            .group_by(
                day,
                ModelUsageEvent.harness,
                ModelUsageEvent.agent_id,
                ModelUsageEvent.locality,
                ModelUsageEvent.model,
                is_operator,
            )
        )
    ).all()

    local_models = await _model_locality(session)
    lead_ids = await _lead_ids(session)

    weeks_out: dict[str, dict] = {
        iso_week_label(m): {"week": iso_week_label(m), "week_start": m.isoformat(),
                            "partial": m == mondays[-1], "totals": _empty(), "sources": {}}
        for m in mondays
    }
    for r in rows:
        # func.date(): "YYYY-MM-DD" string on SQLite, date on Postgres.
        d = r.day if isinstance(r.day, date) else date.fromisoformat(str(r.day)[:10])
        week = weeks_out.get(iso_week_label(d))
        if week is None:
            continue

        if r.harness.startswith("head-"):
            source = f"heads:{r.locality or 'unknown'}"
        elif r.is_operator:
            source = "operator"
        elif r.agent_id is not None:
            source = "lead" if r.agent_id in lead_ids else f"agents:{r.harness}"
        else:
            source = "unattributed"

        if r.locality in ("local", "cloud"):
            local = r.locality == "local"
        else:
            local = r.model.lower() in local_models

        tokens = {f: int(getattr(r, f) or 0) for f in _TOKEN_FIELDS}
        total = sum(tokens.values())
        for bucket in (week["totals"], week["sources"].setdefault(source, _empty())):
            bucket["events"] += r.events
            for f, v in tokens.items():
                bucket[f] += v
            bucket["total_tokens"] += total
            bucket["cost_usd"] += float(r.cost or 0.0)
            bucket["unpriced_events"] += int(r.unpriced or 0)
            if local:
                bucket["local_tokens"] += total
                bucket["local_output_tokens"] += tokens["output_tokens"]

    result_weeks = []
    for week in weeks_out.values():
        sources = [{"source": name, **_finish(b)} for name, b in week["sources"].items()]
        sources.sort(key=lambda s: (s["cost_usd"], s["total_tokens"]), reverse=True)
        result_weeks.append({**week, "totals": _finish(week["totals"]), "sources": sources})

    return {"generated_at": now.isoformat(), "start": start.isoformat(), "weeks": result_weeks}
