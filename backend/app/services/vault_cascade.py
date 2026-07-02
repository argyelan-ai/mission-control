"""Vault Cascading Page Updates — propagate new knowledge to related notes.

When a note transitions to ``status: published`` (promotion event), this
service finds the Top-5 semantically related published notes via Qdrant,
asks DGX Spark whether each needs an update, and writes patch envelopes
to ``_inbox/`` as drafts (never directly modifies published content).

Loop prevention:
- Redis key ``mc:vault:cascade:{note_id}`` with TTL 300s prevents
  re-processing the same note.
- ``depth`` parameter enforces max cascade depth of 1 — a cascade-created
  patch does NOT trigger further cascading.

Fail-soft: Spark/Qdrant down -> skip, log warning, return error in result.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import frontmatter

logger = logging.getLogger("mc.vault_cascade")

CASCADE_REDIS_PREFIX = "mc:vault:cascade:"
CASCADE_TTL_SECONDS = 600
CASCADE_DEPTH_LIMIT = 1
COSINE_THRESHOLD = 0.75
MAX_CANDIDATES = 5
MAX_PATCH_LENGTH = 2000

CASCADE_SYSTEM_PROMPT = (
    "Du bist ein Knowledge-Base-Editor. Eine neue Erkenntnis wurde dokumentiert. "
    "Pruefe ob eine bestehende Note aktualisiert werden muss um die neue "
    "Erkenntnis zu reflektieren.\n\n"
    "Regeln:\n"
    "- Wenn ja: Schreibe einen kurzen Update-Absatz (max 3 Saetze) der an die "
    "bestehende Note angehaengt werden soll.\n"
    "- Wenn nein: Sage 'no_update'.\n\n"
    'Antworte als JSON: {"update_needed": true/false, "patch": "..." oder null, "reason": "..."}'
)


def _parse_llm_response(raw: str) -> dict[str, Any]:
    """Parse the LLM JSON response. Returns safe defaults on failure."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned.rsplit("```", 1)[0]
    cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned)
        update_needed = bool(parsed.get("update_needed", False))
        patch = parsed.get("patch")
        reason = parsed.get("reason", "")

        # Reject patches that are too long (should be an addition, not a rewrite)
        if update_needed and patch and len(str(patch)) > MAX_PATCH_LENGTH:
            logger.info("cascade: rejecting oversized patch (%d chars)", len(str(patch)))
            return {"update_needed": False, "patch": None, "reason": "patch too long"}

        return {"update_needed": update_needed, "patch": patch, "reason": reason}
    except (json.JSONDecodeError, TypeError, KeyError):
        return {"update_needed": False, "patch": None, "reason": "parse_error"}


@dataclass
class CascadeResult:
    candidates_checked: int = 0
    patches_created: int = 0
    skipped_reason: str | None = None
    error: str | None = None
    details: list[dict[str, Any]] = field(default_factory=list)


async def cascade_updates(
    *,
    note_path: Path,
    vault_path: Path,
    spark: Any,
    qdrant_client: Any,
    redis: Any,
    depth: int = 0,
    collection: str = "memory_vault",
) -> CascadeResult:
    """Run cascading update check for a newly published note.

    Args:
        note_path: Absolute path to the published note.
        vault_path: Vault root directory.
        spark: SparkClient instance for LLM + embeddings.
        qdrant_client: AsyncQdrantClient for similarity search.
        redis: Redis client for cascade-lock TTL keys.
        depth: Current cascade depth (0 = initial, 1 = max).
        collection: Qdrant collection name.

    Returns:
        CascadeResult with counts and details.
    """
    result = CascadeResult()

    # Depth guard
    if depth >= CASCADE_DEPTH_LIMIT:
        result.skipped_reason = "max_depth"
        logger.debug("cascade: skipped (depth=%d >= limit=%d)", depth, CASCADE_DEPTH_LIMIT)
        return result

    # Read note content
    try:
        post = frontmatter.load(note_path)
    except Exception as e:
        result.error = f"failed to read note: {e}"
        return result

    note_id = post.metadata.get("id", note_path.stem)
    title = post.metadata.get("title", note_path.stem)
    content = post.content or ""

    # Redis cascade-lock check
    lock_key = f"{CASCADE_REDIS_PREFIX}{note_id}"
    try:
        existing = await redis.get(lock_key)
        if existing is not None:
            result.skipped_reason = "cascade_lock"
            logger.debug("cascade: skipped %s (lock exists)", note_id)
            return result
    except Exception as e:
        logger.warning("cascade: redis get failed (proceeding): %s", e)

    # Set cascade lock
    try:
        await redis.set(lock_key, "1", ex=CASCADE_TTL_SECONDS)
    except Exception as e:
        logger.warning("cascade: redis set lock failed (proceeding): %s", e)

    # Get embedding for the new note
    try:
        embedding = await spark.embed(f"{title}\n{content[:1500]}")
    except Exception as e:
        result.error = f"spark embed failed: {e}"
        logger.warning("cascade: embed failed for %s: %s", note_id, e)
        return result

    # Find top-K similar published notes via Qdrant
    try:
        response = await qdrant_client.query_points(
            collection_name=collection,
            query=embedding,
            limit=MAX_CANDIDATES + 1,
            with_payload=True,
        )
        hits = response.points
    except Exception as e:
        result.error = f"qdrant search failed: {e}"
        logger.warning("cascade: qdrant search failed for %s: %s", note_id, e)
        return result

    # Filter candidates: exclude self, only published, score >= threshold
    candidates: list[dict[str, Any]] = []
    for hit in hits:
        payload = hit.payload or {}
        candidate_path = payload.get("path", "")
        candidate_slug = payload.get("slug") or Path(candidate_path).stem
        try:
            note_rel = str(note_path.relative_to(vault_path))
        except ValueError:
            note_rel = note_path.stem
        if candidate_slug == note_path.stem or candidate_path == note_rel:
            continue
        if hit.score < COSINE_THRESHOLD:
            continue

        # Read the candidate note to check status
        candidate_full = vault_path / candidate_path
        if not candidate_full.resolve().is_relative_to(vault_path.resolve()):
            continue  # path traversal from Qdrant payload — skip
        if not candidate_full.exists():
            continue
        try:
            candidate_post = frontmatter.load(candidate_full)
            if candidate_post.metadata.get("status") not in ("published", None):
                continue  # Only cascade to published notes (None = pre-Phase-2 compat)
        except Exception:
            continue

        candidates.append({
            "path": candidate_path,
            "slug": candidate_slug,
            "title": candidate_post.metadata.get("title", candidate_slug),
            "content": candidate_post.content or "",
            "score": hit.score,
        })
        if len(candidates) >= MAX_CANDIDATES:
            break

    if not candidates:
        logger.debug("cascade: no candidates for %s", note_id)
        return result

    # Ask LLM for each candidate (parallel, max 5 concurrent)
    async def _check_candidate(candidate: dict) -> dict[str, Any]:
        prompt = (
            f"NEUE NOTE: {title}\n{content[:1500]}\n\n"
            f"BESTEHENDE NOTE: {candidate['title']}\n{candidate['content'][:1500]}"
        )
        try:
            raw = await spark.complete(
                prompt=prompt,
                system=CASCADE_SYSTEM_PROMPT,
                max_tokens=400,
                temperature=0.1,
            )
            parsed = _parse_llm_response(raw)
            return {**candidate, **parsed}
        except Exception as e:
            logger.warning("cascade: spark complete failed for %s: %s", candidate["slug"], e)
            return {**candidate, "update_needed": False, "error": str(e)}

    checked = await asyncio.gather(*[_check_candidate(c) for c in candidates])
    result.candidates_checked = len(checked)

    # Write patches as inbox envelopes
    inbox = vault_path / "_inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)

    for entry in checked:
        result.details.append({
            "slug": entry["slug"],
            "update_needed": entry.get("update_needed", False),
            "reason": entry.get("reason", ""),
        })

        if not entry.get("update_needed") or not entry.get("patch"):
            continue

        # Write envelope as a draft update for the target note
        ts = now.strftime("%Y%m%dT%H%M%S%f")
        envelope_name = f"{ts}_cascade_{entry['slug']}.md"
        envelope_path = inbox / envelope_name

        patch_content = (
            f"{entry['content']}\n\n"
            f"---\n\n"
            f"**Update basierend auf [[{note_path.stem}]]:**\n\n"
            f"{entry['patch']}"
        )

        metadata = {
            "op": "upsert",
            "target": entry["path"],
            "agent_id": "system",
            "agent": "system",
            "type": "knowledge",
            "tags": ["cascade-update"],
            "date": now.isoformat(),
            "id": f"system-cascade-{ts[:15]}",
            "status": "draft",
            "related": [f"[[{note_path.stem}]]"],
            "relations": {note_path.stem: "refined-by"},
            "cascade_source": note_id,
        }

        post_envelope = frontmatter.Post(patch_content, **metadata)
        tmp = envelope_path.with_suffix(".tmp")
        tmp.write_text(frontmatter.dumps(post_envelope))
        tmp.rename(envelope_path)

        result.patches_created += 1
        logger.info(
            "cascade: patch created for %s (source=%s, reason=%s)",
            entry["slug"], note_id, entry.get("reason", ""),
        )

    logger.info(
        "cascade: done for %s — checked=%d patches=%d",
        note_id, result.candidates_checked, result.patches_created,
    )
    return result
