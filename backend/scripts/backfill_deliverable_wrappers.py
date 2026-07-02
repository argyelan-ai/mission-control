"""One-shot backfill: sync existing TaskDeliverables into the vault as
Markdown wrappers + hardlinked attachments. Part of Phase A vault-as-brain.

Usage (inside the backend container):

    docker compose exec backend python -m scripts.backfill_deliverable_wrappers --dry-run
    docker compose exec backend python -m scripts.backfill_deliverable_wrappers
    docker compose exec backend python -m scripts.backfill_deliverable_wrappers --limit 10
    docker compose exec backend python -m scripts.backfill_deliverable_wrappers --force

Default behaviour is idempotent — existing wrappers are kept untouched. Use
``--force`` to overwrite (helpful when the wrapper template changes).

The script enumerates from the DB, NOT from the filesystem — the
``mc_shared_deliverables`` named volume isn't visible from the host walk
but its rows still live in ``task_deliverables`` and resolve to
``/shared-deliverables/<task_id>/<file>`` paths the backend can read.

Fail-soft: a single unresolvable / missing source file logs a warning and
moves on. Final summary lists per-status counts plus any errors.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import engine
from app.models.deliverable import TaskDeliverable
from app.services.deliverable_wrapper import sync_deliverable_to_vault

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("backfill_wrappers")


async def _run(args: argparse.Namespace) -> int:
    summary = {
        "total": 0,
        "synced": 0,
        "skipped": 0,
        "errors": 0,
    }
    skip_reasons: dict[str, int] = {}
    error_lines: list[str] = []

    async with AsyncSession(engine, expire_on_commit=False) as session:
        stmt = select(TaskDeliverable).order_by(TaskDeliverable.created_at.asc())
        if args.limit:
            stmt = stmt.limit(args.limit)
        result = await session.exec(stmt)
        deliverables = list(result.all())

        summary["total"] = len(deliverables)
        logger.info(
            "Backfill: %d deliverables to consider (dry-run=%s force=%s)",
            summary["total"],
            args.dry_run,
            args.force,
        )

        for i, deliverable in enumerate(deliverables, start=1):
            if args.dry_run:
                # Pretend run — still call the resolver-only code path so
                # we surface "source-unresolvable" warnings, but never
                # actually write.
                logger.info(
                    "[%d/%d] DRY %s (%s) — %s",
                    i,
                    summary["total"],
                    deliverable.id,
                    deliverable.deliverable_type,
                    deliverable.title[:60],
                )
                continue

            try:
                res = await sync_deliverable_to_vault(
                    deliverable, session, force=args.force
                )
            except Exception as exc:
                summary["errors"] += 1
                error_lines.append(f"{deliverable.id}: {exc}")
                logger.exception("sync failed for %s", deliverable.id)
                continue

            if res.error:
                summary["errors"] += 1
                error_lines.append(f"{deliverable.id}: {res.error}")
            elif res.skipped:
                summary["skipped"] += 1
                skip_reasons[res.reason or "unknown"] = (
                    skip_reasons.get(res.reason or "unknown", 0) + 1
                )
            else:
                summary["synced"] += 1

            if i % 50 == 0:
                logger.info(
                    "  progress: %d/%d (synced=%d skipped=%d errors=%d)",
                    i,
                    summary["total"],
                    summary["synced"],
                    summary["skipped"],
                    summary["errors"],
                )

    print()
    print("─" * 60)
    print(f"Backfill summary  (dry-run={args.dry_run}, force={args.force})")
    print(f"  Total:    {summary['total']}")
    print(f"  Synced:   {summary['synced']}")
    print(f"  Skipped:  {summary['skipped']}")
    for reason, n in sorted(skip_reasons.items(), key=lambda kv: -kv[1]):
        print(f"    · {reason}: {n}")
    print(f"  Errors:   {summary['errors']}")
    if error_lines:
        print("  Error detail (first 20):")
        for line in error_lines[:20]:
            print(f"    · {line}")
    print("─" * 60)

    return 0 if summary["errors"] == 0 else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill vault wrappers for existing TaskDeliverables."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be done without writing files.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Cap the number of deliverables processed (0 = all).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-write existing wrappers (default: idempotent skip).",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
