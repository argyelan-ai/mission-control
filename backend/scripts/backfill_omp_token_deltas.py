"""One-time backfill: correct omp token events harvested with FULL-CONTEXT input.

omp (OpenAI-completions convention) reports the entire conversation context as
``usage.input`` on every call; the harvester used to store that number 1:1,
which made an omp worker look ~100x more expensive than a Claude worker on the
Insights page. This script re-derives fresh_input/cache_read per omp session
from the JSONL source files (see token_harvester.split_omp_context) and UPDATEs
the affected model_usage_events rows, including cost_usd.

Usage (inside the backend container):
    docker compose exec backend python -m scripts.backfill_omp_token_deltas --dry-run
    docker compose exec backend python -m scripts.backfill_omp_token_deltas

Idempotent: run it twice — the second run corrects 0 events (every row already
matches the pure-function derivation from the source files).
"""
import argparse
import asyncio
import logging

from app.database import engine
from app.services.token_harvester import backfill_omp_token_deltas
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("backfill_omp")


async def _run(dry_run: bool) -> None:
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        stats = await backfill_omp_token_deltas(session, commit=not dry_run)
        if dry_run:
            await session.rollback()
    logger.info(
        "omp token backfill%s: files_scanned=%d events_matched=%d events_corrected=%d",
        " (dry-run)" if dry_run else "",
        stats["files_scanned"],
        stats["events_matched"],
        stats["events_corrected"],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute corrections but do not commit (rollback instead).",
    )
    args = parser.parse_args()
    asyncio.run(_run(args.dry_run))


if __name__ == "__main__":
    main()
