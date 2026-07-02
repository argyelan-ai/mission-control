"""Contradiction detection for vault notes via Qdrant + Spark LLM.

When a new note is written, check if it contradicts existing notes:
1. Embed the note content via Spark
2. Query Qdrant for top-5 similar notes (cosine >= 0.80)
3. For each candidate, ask Spark LLM to classify the relation
4. Return contradictions + refinements for the caller to act on

Pipeline pattern reused from vault_wikilink_backfill.py.
Fail-soft: if Spark or Qdrant is down, returns empty list.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import frontmatter

logger = logging.getLogger("mc.vault_contradiction")

SIMILARITY_THRESHOLD = 0.80
MAX_LLM_CALLS = 5  # Rate-limit protection on Spark

VALID_RELATIONS = {"contradicts", "refines", "confirms", "unrelated"}

CONTRADICTION_SYSTEM_PROMPT = (
    "Du bist ein Fact-Checker. Vergleiche diese zwei Wissensbeitraege.\n\n"
    "Klassifiziere die Beziehung:\n"
    '- "contradicts": A sagt das Gegenteil von B (oder umgekehrt)\n'
    '- "refines": A ist eine aktualisierte/praezisere Version von B\n'
    '- "confirms": A bestaetigt was B sagt\n'
    '- "unrelated": A und B behandeln verschiedene Themen\n\n'
    'Antworte NUR mit einem JSON: {"relation": "...", "reason": "..."}'
)


@dataclass
class ContradictionResult:
    relation: str  # contradicts | refines | confirms | unrelated
    reason: str
    other_note_id: str
    other_note_path: str


async def classify_relation(
    spark: Any,
    title_a: str,
    content_a: str,
    title_b: str,
    content_b: str,
) -> ContradictionResult:
    """Ask Spark LLM to classify the relation between two notes.

    Returns a ContradictionResult. On any failure, returns 'unrelated'
    (fail-soft — missing a contradiction is better than blocking writes).
    """
    prompt = (
        f"NOTE A (neu): {title_a}\n{content_a[:800]}\n\n"
        f"NOTE B (bestehend): {title_b}\n{content_b[:800]}"
    )

    try:
        raw = await spark.complete(
            prompt=prompt,
            system=CONTRADICTION_SYSTEM_PROMPT,
            max_tokens=200,
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

        relation = parsed.get("relation", "unrelated")
        if relation not in VALID_RELATIONS:
            relation = "unrelated"
        reason = parsed.get("reason", "")

        return ContradictionResult(
            relation=relation,
            reason=reason,
            other_note_id="",
            other_note_path="",
        )

    except Exception as exc:
        logger.warning("classify_relation failed: %s", exc)
        return ContradictionResult(
            relation="unrelated",
            reason=f"classification failed: {exc}",
            other_note_id="",
            other_note_path="",
        )


async def check_contradictions(
    note_path: Path,
    vault_path: Path,
    qdrant_client: Any,
    spark: Any,
) -> list[ContradictionResult]:
    """Check a newly written note for contradictions with existing notes.

    Pipeline:
    1. Parse the note's frontmatter to get its id + content
    2. Embed the content via Spark
    3. Query Qdrant for top-5 similar notes (cosine >= SIMILARITY_THRESHOLD)
    4. For each candidate above threshold (max MAX_LLM_CALLS):
       - Skip self-references (same note id)
       - Ask Spark LLM to classify the relation
    5. Return only contradictions and refinements

    Fail-soft: returns [] on any infrastructure failure.
    """
    try:
        post = frontmatter.load(str(note_path))
    except Exception as exc:
        logger.warning("check_contradictions: cannot parse %s: %s", note_path, exc)
        return []

    meta = post.metadata or {}
    note_id = str(meta.get("id", ""))
    title = str(meta.get("title", note_path.stem))
    content = post.content or ""

    if not content.strip():
        return []

    # Step 1: Embed
    try:
        embedding = await spark.embed(f"{title}\n{content[:1500]}")
    except Exception as exc:
        logger.warning("check_contradictions: embedding failed for %s: %s", note_id, exc)
        return []

    # Step 2: Query Qdrant
    try:
        # Support both sync mock and async client
        search_result = qdrant_client.search(
            collection_name="memory_vault",
            query_vector=embedding,
            limit=MAX_LLM_CALLS + 1,  # +1 for possible self-hit
        )
        if hasattr(search_result, "__await__"):
            hits = await search_result
        else:
            hits = search_result
    except Exception as exc:
        logger.warning("check_contradictions: Qdrant search failed: %s", exc)
        return []

    # Step 3: Filter and classify
    results: list[ContradictionResult] = []
    llm_calls = 0

    for hit in hits:
        if llm_calls >= MAX_LLM_CALLS:
            break

        payload = hit.payload or {}
        other_id = str(payload.get("id", ""))
        other_path = str(payload.get("path", ""))

        # Skip self
        if other_id == note_id:
            continue

        # Skip below threshold
        if hit.score < SIMILARITY_THRESHOLD:
            continue

        # Read candidate note content
        other_full = vault_path / other_path
        if not other_full.exists():
            continue

        try:
            other_post = frontmatter.load(str(other_full))
            other_title = str(other_post.metadata.get("title", other_full.stem))
            other_content = other_post.content or ""
        except Exception:
            continue

        # Classify
        result = await classify_relation(spark, title, content, other_title, other_content)
        result.other_note_id = other_id
        result.other_note_path = other_path
        llm_calls += 1

        if result.relation in ("contradicts", "refines"):
            results.append(result)
            logger.info(
                "Contradiction check: %s %s %s (%s)",
                note_id, result.relation, other_id, result.reason[:80],
            )

    return results
