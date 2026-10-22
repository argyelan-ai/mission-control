"""One-off: move the operator's own host Claude Code usage off the lead agent.

Until 22.10.2026 the token harvester booked every MC-looking line under the
host's ``~/.claude/projects`` on the lead agent — including the operator's
own interactive sessions, which share that folder. The harvester now books
only the lead's own project dir (the Claude Code encoding of its working dir,
MC_CHECKOUT_PATH) on the lead; this script corrects the rows written before.

Dry run by default: prints rows + list-price USD per bucket, writes nothing.
``--apply`` moves the ``operator_session`` bucket to agent_id NULL + harness
``operator-session`` (tokens, cost and task_id unchanged). Idempotent.

Usage (inside the backend container):
    docker compose -p mission-control exec -u mcuser backend python -m scripts.reattribute_operator_sessions
    docker compose -p mission-control exec -u mcuser backend python -m scripts.reattribute_operator_sessions --since 2026-10-01
    docker compose -p mission-control exec -u mcuser backend python -m scripts.reattribute_operator_sessions --apply
"""
import argparse
import asyncio
import json
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import engine
from app.services.token_harvester import reattribute_operator_sessions


def _parse_since(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _run(apply: bool, since: datetime | None) -> dict:
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        return await reattribute_operator_sessions(session, apply=apply, since=since)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Write the change (default: dry run, counts only).")
    parser.add_argument("--since", type=_parse_since, default=None,
                        help="Only rows with ts >= this ISO date/time (UTC if no zone). Default: all rows.")
    args = parser.parse_args()

    report = asyncio.run(_run(args.apply, args.since))
    print(json.dumps(report, indent=2))
    moved = report["buckets"]["operator_session"]
    if args.apply:
        print(f"applied: {report['updated']} rows moved to the operator bucket")
    else:
        print(f"dry run: would move {moved['rows']} rows / {moved['cost_usd']} USD — rerun with --apply")


if __name__ == "__main__":
    main()
