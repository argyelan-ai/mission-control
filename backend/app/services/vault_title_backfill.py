"""W2 — generate `title:` frontmatter for vault notes via DGX-Spark Qwen3.6.

Runs after W1 cleanup so the corpus is reduced to ~305 keep-notes.
Idempotent: only touches notes without existing `title:` frontmatter.
Resumable via state.set_checkpoint('title-backfill', last_rel_path).
"""

from __future__ import annotations

import frontmatter
from dataclasses import dataclass
from pathlib import Path

from app.services.spark_client import SparkClient
from app.services.vault_cleanup_state import VaultCleanupState


SYSTEM_PROMPT = (
    "You are a concise title generator. Given the body of a knowledge note, "
    "return ONLY a 4-8 word title that captures the essence. No quotes, "
    "no trailing punctuation, no preamble like 'Title:'."
)


async def generate_title_for_note(spark: SparkClient, content: str) -> str:
    """Generate a 4-8 word title from a note's content via Qwen3.6."""
    excerpt = content.strip()[:1500]
    raw = await spark.complete(
        prompt=excerpt,
        system=SYSTEM_PROMPT,
        max_tokens=40,
        temperature=0.2,
    )
    cleaned = raw.strip().strip('"').strip("'").rstrip(".")
    cleaned = " ".join(cleaned.split())
    return cleaned[:80]


@dataclass
class TitleBackfillResult:
    processed: int
    skipped: int
    failed: int


async def backfill_titles(
    spark: SparkClient,
    vault_root: Path,
    state: VaultCleanupState,
) -> TitleBackfillResult:
    """Walk the live vault, generate `title:` for notes without it.

    Skips _inbox/_rejected/_conflicts and any note that already has `title:`.
    Resumable via state.get_checkpoint('title-backfill')."""
    SKIP_DIRS = {"_inbox", "_rejected", "_conflicts"}
    paths = sorted(vault_root.rglob("*.md"))
    paths = [p for p in paths if p.relative_to(vault_root).parts[0] not in SKIP_DIRS]

    checkpoint = state.get_checkpoint("title-backfill")
    if checkpoint:
        rels = [str(p.relative_to(vault_root)) for p in paths]
        idx = rels.index(checkpoint) + 1 if checkpoint in rels else 0
        paths = paths[idx:]
        state.log("INFO", f"title-backfill resume from index {idx}")

    processed = 0
    skipped = 0
    failed = 0
    for path in paths:
        rel = str(path.relative_to(vault_root))
        try:
            post = frontmatter.load(path)
            if (post.metadata or {}).get("title"):
                skipped += 1
                state.set_checkpoint("title-backfill", rel)
                continue
            title = await generate_title_for_note(spark, post.content)
            post.metadata["title"] = title
            path.write_text(frontmatter.dumps(post))
            processed += 1
            state.set_checkpoint("title-backfill", rel)
            if processed % 25 == 0:
                state.log("INFO", f"title-backfill progress: {processed}/{len(paths)}")
        except Exception as e:
            failed += 1
            state.log("WARN", f"title-backfill failed for {rel}: {e}")
            state.set_checkpoint("title-backfill", rel)

    state.log("INFO", f"title-backfill done: processed={processed} skipped={skipped} failed={failed}")
    return TitleBackfillResult(processed=processed, skipped=skipped, failed=failed)
