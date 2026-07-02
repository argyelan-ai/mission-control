"""W3-B — generate wikilinks for vault notes via DGX-Spark Qwen3.6.

For each note: get top-k similarity candidates from Qdrant, ask Qwen which
2-4 are TRULY related (and with which relation), write [[wikilinks]] to
the note's frontmatter `related:` field AND inline at note end.

Resumable via state.set_checkpoint('wikilink-backfill', last_rel_path).

Qdrant payload schema (written by vault_embeddings.py):
  path  — vault-relative path (e.g. "memory/note.md")
  id    — frontmatter `id` value (string)
  agent — frontmatter `agent` value
  type  — frontmatter `type` value
  tags  — frontmatter `tags` list

slug is derived from path stem; title and excerpt are read from frontmatter
and content at search time (not stored in Qdrant payload).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import frontmatter

from app.services.spark_client import SparkClient
from app.services.vault_cleanup_state import VaultCleanupState

logger = logging.getLogger("mc.vault_wikilink_backfill")

ALLOWED_RELATIONS = {
    "supersedes",
    "contradicts",
    "refines",
    "example-of",
    "depends-on",
    "related-to",
}


SYSTEM_PROMPT = (
    "You are a knowledge-graph editor. Given a note and 5-8 candidate notes, "
    "select 2-4 that are TRULY related (not just topically adjacent). "
    "For each pick, classify the relation as one of: "
    "supersedes, contradicts, refines, example-of, depends-on, related-to. "
    "Reply with ONLY a JSON array, no preamble. Schema: "
    '[{"slug": "...", "relation": "..."}, ...]'
)


async def generate_wikilinks(
    spark: SparkClient,
    note_title: str,
    note_excerpt: str,
    candidates: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    """Returns up to 4 (slug, relation) tuples. Falls back to top-2
    'related-to' on parse failure to keep the corpus moving."""
    prompt_lines = [
        f"NEW NOTE TITLE: {note_title}",
        f"NEW NOTE EXCERPT: {note_excerpt[:800]}",
        "",
        "CANDIDATES:",
    ]
    for c in candidates[:8]:
        prompt_lines.append(
            f"- slug={c['slug']} title={c['title']} excerpt={c['excerpt'][:200]}"
        )
    prompt = "\n".join(prompt_lines)

    try:
        raw = await spark.complete(
            prompt=prompt,
            system=SYSTEM_PROMPT,
            max_tokens=300,
            temperature=0.1,
        )
        cleaned = raw.strip()
        # Strip markdown code fences if present
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned.rsplit("```", 1)[0]
        cleaned = cleaned.strip()
        parsed = json.loads(cleaned)
        out: list[tuple[str, str]] = []
        valid_slugs = {c["slug"] for c in candidates}
        for entry in parsed[:4]:
            slug = entry.get("slug", "")
            relation = entry.get("relation", "related-to")
            if slug not in valid_slugs:
                continue
            if relation not in ALLOWED_RELATIONS:
                relation = "related-to"
            out.append((slug, relation))
        if len(out) >= 2:
            return out
    except Exception:
        pass

    # Fallback: top-2 candidates as related-to
    return [(c["slug"], "related-to") for c in candidates[:2]]


async def fetch_top_k_candidates(
    qdrant_client: Any,
    embedding: list[float],
    exclude_slug: str,
    k: int = 8,
    collection: str = "memory_vault",
) -> list[dict]:
    """Top-K Qdrant neighbours minus the source note itself.

    Works with both AsyncQdrantClient (production) and synchronous MagicMock
    (tests).  The Qdrant payload stores ``path`` (vault-relative, e.g.
    ``memory/note.md``); slug is derived from the path stem, and title /
    excerpt fall back to the slug / empty string when absent from the payload
    (they are not stored by vault_embeddings.py).
    """
    # Support both async (AsyncQdrantClient, modern API) and sync (MagicMock in tests).
    # AsyncQdrantClient deprecated `.search()` in favor of `.query_points()` which
    # returns a QueryResponse with `.points`. Tests use MagicMock(.search) for the
    # legacy shape and return ScoredPoint lists directly.
    if hasattr(qdrant_client, "query_points") and not hasattr(qdrant_client, "_mock_name"):
        response = await qdrant_client.query_points(
            collection_name=collection,
            query=embedding,
            limit=k + 1,
            with_payload=True,
        )
        hits = response.points
    else:
        search_result = qdrant_client.search(
            collection_name=collection,
            query_vector=embedding,
            limit=k + 1,
        )
        if hasattr(search_result, "__await__"):
            hits = await search_result
        else:
            hits = search_result

    out: list[dict] = []
    for hit in hits:
        payload = hit.payload or {}
        # Derive slug: prefer explicit `slug` key (future-proofing), else path stem
        slug = payload.get("slug") or Path(payload.get("path", "")).stem or ""
        if not slug or slug == exclude_slug:
            continue
        out.append(
            {
                "slug": slug,
                "title": payload.get("title", slug),
                "excerpt": payload.get("excerpt", ""),
                "score": hit.score,
            }
        )
        if len(out) >= k:
            break
    return out


@dataclass
class WikilinkBackfillResult:
    processed: int
    skipped: int
    failed: int


async def backfill_wikilinks(
    spark: SparkClient,
    qdrant_client: Any,
    vault_root: Path,
    state: VaultCleanupState,
) -> WikilinkBackfillResult:
    """For every note without ``related:`` in frontmatter, fetch top-K
    similarity candidates from Qdrant, ask Qwen to pick 2-4 with relations,
    write the wikilinks both as frontmatter list AND inline section.

    Skips notes that already have ``related:`` set (idempotent).
    Resumable: last processed relative path is checkpointed under
    ``wikilink-backfill`` so a crash or restart skips already-done notes.
    """
    SKIP_DIRS = {"_inbox", "_rejected", "_conflicts"}
    paths = sorted(vault_root.rglob("*.md"))
    paths = [
        p
        for p in paths
        if p.relative_to(vault_root).parts[0] not in SKIP_DIRS
    ]

    checkpoint = state.get_checkpoint("wikilink-backfill")
    if checkpoint:
        rels = [str(p.relative_to(vault_root)) for p in paths]
        idx = (rels.index(checkpoint) + 1) if checkpoint in rels else 0
        paths = paths[idx:]
        state.log("INFO", f"wikilink-backfill resume from index {idx}")

    processed = 0
    skipped = 0
    failed = 0

    for path in paths:
        rel = str(path.relative_to(vault_root))
        try:
            post = frontmatter.load(path)
            meta = post.metadata or {}

            if meta.get("related"):
                skipped += 1
                state.set_checkpoint("wikilink-backfill", rel)
                continue

            slug = meta.get("slug") or path.stem
            title = meta.get("title") or slug
            excerpt = (post.content or "")[:1500]

            embedding = await spark.embed(f"{title}\n{excerpt}")
            candidates = await fetch_top_k_candidates(
                qdrant_client, embedding, exclude_slug=slug, k=8
            )
            if len(candidates) < 2:
                skipped += 1
                state.log("INFO", f"wikilink-backfill skipped (too few candidates): {rel}")
                state.set_checkpoint("wikilink-backfill", rel)
                continue

            picks = await generate_wikilinks(spark, title, excerpt, candidates)

            related = [f"[[{p_slug}]]" for p_slug, _r in picks]
            relations = {p_slug: r for p_slug, r in picks}
            post.metadata["related"] = related
            post.metadata["relations"] = relations

            inline = "\n\n## Verwandt\n" + "\n".join(
                f"- [[{p_slug}]] ({r})" for p_slug, r in picks
            )
            if "## Verwandt" not in post.content:
                post.content = post.content + inline

            path.write_text(frontmatter.dumps(post))
            processed += 1
            state.set_checkpoint("wikilink-backfill", rel)

            if processed % 25 == 0:
                state.log(
                    "INFO",
                    f"wikilink-backfill progress: {processed} processed, "
                    f"{skipped} skipped, {failed} failed",
                )

        except Exception as e:
            failed += 1
            state.log("WARN", f"wikilink-backfill failed for {rel}: {e}")
            state.set_checkpoint("wikilink-backfill", rel)

    state.log(
        "INFO",
        f"wikilink-backfill done: processed={processed} skipped={skipped} failed={failed}",
    )
    return WikilinkBackfillResult(processed=processed, skipped=skipped, failed=failed)
