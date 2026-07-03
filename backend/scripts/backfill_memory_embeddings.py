"""One-time backfill: index existing BoardMemory entries into Qdrant.

Usage (inside the backend container):
    docker compose exec backend python -m scripts.backfill_memory_embeddings
    docker compose exec backend python -m scripts.backfill_memory_embeddings --dry-run
    docker compose exec backend python -m scripts.backfill_memory_embeddings --limit 100
    docker compose exec backend python -m scripts.backfill_memory_embeddings --force

Default: only memories that don't have a Qdrant point yet (idempotent). With
--force, all are re-indexed (for model changes).

Fail-soft: if Spark or Qdrant are down, processing continues and the number
of errors is reported at the end.
"""
import argparse
import asyncio
import logging
import sys

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import engine
from app.models.memory import BoardMemory
from app.services.memory_indexing import layer_for, index_memory
from app.services.qdrant_service import qdrant_service, LAYER_COLLECTIONS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("backfill")


async def _already_indexed(layer: str, memory_id: str) -> bool:
    """Check whether a memory already exists in the Qdrant collection."""
    try:
        client = await qdrant_service._get_client()
        coll = LAYER_COLLECTIONS[layer]
        result = await client.retrieve(
            collection_name=coll,
            ids=[memory_id],
            with_payload=False,
            with_vectors=False,
        )
        return len(result) > 0
    except Exception:
        return False


async def main(dry_run: bool, limit: int | None, force: bool) -> None:
    await qdrant_service.ensure_collections()

    async with AsyncSession(engine, expire_on_commit=False) as session:
        stmt = select(BoardMemory).order_by(BoardMemory.created_at.asc())  # type: ignore[union-attr]
        if limit:
            stmt = stmt.limit(limit)
        result = await session.exec(stmt)
        memories = result.all()

    total = len(memories)
    logger.info("Gefunden: %d BoardMemory-Eintraege", total)

    stats = {
        "total": total,
        "skipped_no_layer": 0,
        "skipped_already_indexed": 0,
        "indexed": 0,
        "failed": 0,
    }

    for i, mem in enumerate(memories, start=1):
        layer = layer_for(mem)
        if layer is None:
            stats["skipped_no_layer"] += 1
            continue

        if not force:
            already = await _already_indexed(layer, str(mem.id))
            if already:
                stats["skipped_already_indexed"] += 1
                continue

        if dry_run:
            logger.info(
                "[%d/%d] DRY: %s → layer=%s title=%r",
                i, total, mem.id, layer, (mem.title or "")[:60],
            )
            stats["indexed"] += 1
            continue

        try:
            result_layer = await index_memory(mem)
            if result_layer:
                stats["indexed"] += 1
                if i % 10 == 0:
                    logger.info("[%d/%d] Fortschritt: %d indexed", i, total, stats["indexed"])
            else:
                stats["failed"] += 1
        except Exception as e:
            logger.warning("Backfill failed for %s: %s", mem.id, e)
            stats["failed"] += 1

    logger.info("── BACKFILL FERTIG ──")
    for k, v in stats.items():
        logger.info("  %-25s %d", k, v)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Nichts schreiben, nur zaehlen")
    parser.add_argument("--limit", type=int, default=None, help="Maximal N Memories verarbeiten")
    parser.add_argument("--force", action="store_true", help="Auch bereits indexed Memories re-indexen")
    args = parser.parse_args()

    try:
        asyncio.run(main(dry_run=args.dry_run, limit=args.limit, force=args.force))
    except KeyboardInterrupt:
        sys.exit(130)
