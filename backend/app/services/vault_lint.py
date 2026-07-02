"""Vault structural lint — orphans, frontmatter, duplicate ids.

Runs as 24h cron (wired in main.py by Task 4). Writes report to
~/.mc/vault/_lint/{YYYY-MM-DD}.md as a regular vault note (so it gets
indexed + searchable + linkable).

No network, no Redis — pure filesystem operations.
"""

import logging
import re
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import frontmatter

from app.helpers.vault_constants import EXCLUDED_PREFIXES
from app.helpers.vault_frontmatter import (
    FrontmatterError,
    parse_frontmatter,
    validate_frontmatter,
)

logger = logging.getLogger("mc.vault_lint")

INTENTIONAL_ROOTS: tuple[str, ...] = (
    "agents/",
    "projects/",
    "global/",
    "memory/",
    "attachments/",
)

SKIP_DIRS = {"_inbox", "_rejected", "_conflicts", "_trash", "_lint", ".git", ".obsidian"}

WIKILINK_RE = re.compile(r"\[\[([^\[\]|#]+?)(?:\|[^\[\]]+?)?(?:#[^\[\]]+?)?\]\]")


def _is_excluded(rel: str) -> bool:
    """Return True if the relative path should be skipped by the linter."""
    # Auto-generated graphify output (GRAPH_REPORT.md, *-INSIGHTS.md) lives in
    # nested `_graph/` dirs and is regenerated on every graph run — it has no
    # vault frontmatter (no id/status) by design. Skip it so the linter does
    # not report perpetual orphan + missing-id false positives. Matches any
    # path segment named `_graph`, at any depth (e.g. channel-knowledge/.../_graph/).
    if "_graph" in Path(rel).parts:
        return True
    return any(rel.startswith(p) for p in EXCLUDED_PREFIXES)


def _is_orphan(rel: str) -> bool:
    """Return True if the file is not under any intentional vault root."""
    return not any(rel.startswith(root) for root in INTENTIONAL_ROOTS)


def _should_skip(rel: str) -> bool:
    """Return True if relative path is in a directory to skip."""
    first = rel.split("/", 1)[0]
    return first in SKIP_DIRS or _is_excluded(rel)


def _find_broken_wikilinks(vault_path: Path) -> list[dict[str, str]]:
    """Find wikilinks that point to non-existent notes."""
    if not vault_path.exists():
        return []

    # Build index of all note stems
    all_stems: set[str] = set()
    for p in vault_path.rglob("*.md"):
        rel = str(p.relative_to(vault_path))
        if _should_skip(rel):
            continue
        all_stems.add(p.stem)

    broken: list[dict[str, str]] = []
    for p in vault_path.rglob("*.md"):
        rel = str(p.relative_to(vault_path))
        if _should_skip(rel):
            continue
        try:
            content = p.read_text()
        except Exception:
            continue
        for m in WIKILINK_RE.finditer(content):
            target = m.group(1).strip()
            if target and target not in all_stems:
                broken.append({"source": rel, "target": target})

    return broken


def _find_missing_confidence(vault_path: Path) -> list[str]:
    """Find notes without a confidence field in frontmatter."""
    if not vault_path.exists():
        return []

    missing: list[str] = []
    for p in vault_path.rglob("*.md"):
        rel = str(p.relative_to(vault_path))
        if _should_skip(rel):
            continue
        try:
            post = frontmatter.load(p)
            if "confidence" not in (post.metadata or {}):
                missing.append(rel)
        except Exception:
            continue
    return missing


def _auto_fix_missing_confidence(vault_path: Path) -> int:
    """Auto-fix: set confidence=medium on notes missing the field. Returns count fixed."""
    if not vault_path.exists():
        return 0

    fixed = 0
    for p in vault_path.rglob("*.md"):
        rel = str(p.relative_to(vault_path))
        if _should_skip(rel):
            continue
        try:
            post = frontmatter.load(p)
            if "confidence" not in (post.metadata or {}):
                post.metadata["confidence"] = "medium"
                p.write_text(frontmatter.dumps(post))
                fixed += 1
        except Exception:
            continue
    return fixed


def _auto_fix_broken_wikilinks(vault_path: Path) -> int:
    """Auto-fix: remove broken wikilink brackets (keep text). Returns count fixed."""
    if not vault_path.exists():
        return 0

    all_stems: set[str] = set()
    for p in vault_path.rglob("*.md"):
        rel = str(p.relative_to(vault_path))
        if _should_skip(rel):
            continue
        all_stems.add(p.stem)

    fixed = 0
    for p in vault_path.rglob("*.md"):
        rel = str(p.relative_to(vault_path))
        if _should_skip(rel):
            continue
        try:
            content = p.read_text()
            original = content
            for m in WIKILINK_RE.finditer(original):
                target = m.group(1).strip()
                if target and target not in all_stems:
                    # Replace [[broken-link|Display Text]] with Display Text
                    # (preserve alias when present, otherwise use target)
                    full_match = m.group(0)
                    if "|" in full_match:
                        display = full_match.split("|", 1)[1].rstrip("]]").strip()
                    else:
                        display = target
                    content = content.replace(full_match, display, 1)
            if content != original:
                p.write_text(content)
                fixed += 1
        except Exception:
            continue
    return fixed


def lint_vault(vault_path: Path) -> dict[str, Any]:
    """Walk vault, return stats dict with orphans, invalid frontmatter, duplicate IDs.

    Returns:
        {
            "total_files_scanned": int,
            "orphan_count": int,
            "orphans": list[str],          # relative paths
            "frontmatter_invalid_count": int,
            "frontmatter_invalid": list[dict],  # {"path": str, "reason": str}
            "duplicate_id_count": int,
            "duplicate_ids": list[dict],   # {"id": str, "paths": list[str]}
        }
    """
    orphans: list[str] = []
    frontmatter_invalid: list[dict[str, str]] = []
    id_index: dict[str, list[str]] = defaultdict(list)
    total = 0

    if not vault_path.exists():
        logger.warning("lint_vault: vault path does not exist: %s", vault_path)
        return _empty_stats()

    for file_path in sorted(vault_path.rglob("*.md")):
        rel = str(file_path.relative_to(vault_path))

        # Skip excluded system directories
        if _is_excluded(rel):
            continue

        total += 1

        # Orphan check
        if _is_orphan(rel):
            orphans.append(rel)
            logger.debug("Orphan: %s", rel)
            # Still attempt to read for duplicate-ID detection — but continue to next
            # if parsing fails (orphan + invalid is reported under frontmatter_invalid only
            # when parsing fails; here the file is in orphans already).

        # Frontmatter parse + validate
        try:
            post = parse_frontmatter(file_path)
            validate_frontmatter(post.metadata)
        except FrontmatterError as e:
            frontmatter_invalid.append({"path": rel, "reason": str(e)})
            logger.debug("Invalid frontmatter: %s — %s", rel, e)
            continue
        except Exception as e:
            # Unreadable file, permission error, etc.
            frontmatter_invalid.append({"path": rel, "reason": f"read error: {e}"})
            logger.warning("Cannot lint %s: %s", rel, e)
            continue

        # Duplicate ID tracking
        note_id = post.metadata.get("id")
        if note_id:
            id_index[str(note_id)].append(rel)

    # Build duplicate list
    duplicate_ids: list[dict[str, Any]] = [
        {"id": nid, "paths": paths}
        for nid, paths in id_index.items()
        if len(paths) > 1
    ]

    # Extended checks (Phase 3 Intelligence)
    broken_wikilinks = _find_broken_wikilinks(vault_path)
    missing_confidence = _find_missing_confidence(vault_path)

    stats = {
        "total_files_scanned": total,
        "orphan_count": len(orphans),
        "orphans": orphans,
        "frontmatter_invalid_count": len(frontmatter_invalid),
        "frontmatter_invalid": frontmatter_invalid,
        "duplicate_id_count": len(duplicate_ids),
        "duplicate_ids": duplicate_ids,
        "broken_wikilink_count": len(broken_wikilinks),
        "broken_wikilinks": broken_wikilinks,
        "missing_confidence_count": len(missing_confidence),
        "missing_confidence": missing_confidence,
        "linted_at": datetime.utcnow().isoformat() + "Z",
    }
    logger.info(
        "Vault lint complete: %d files, %d orphans, %d invalid fm, %d dup IDs, "
        "%d broken wikilinks, %d missing confidence",
        total,
        len(orphans),
        len(frontmatter_invalid),
        len(duplicate_ids),
        len(broken_wikilinks),
        len(missing_confidence),
    )
    return stats


def _empty_stats() -> dict[str, Any]:
    return {
        "total_files_scanned": 0,
        "orphan_count": 0,
        "orphans": [],
        "frontmatter_invalid_count": 0,
        "frontmatter_invalid": [],
        "duplicate_id_count": 0,
        "duplicate_ids": [],
        "broken_wikilink_count": 0,
        "broken_wikilinks": [],
        "missing_confidence_count": 0,
        "missing_confidence": [],
        "linted_at": datetime.utcnow().isoformat() + "Z",
    }


async def write_lint_report(vault_path: Path, stats: dict[str, Any]) -> Path:
    """Write the daily lint report as a vault note. Returns the report path.

    The report itself satisfies vault frontmatter requirements so it gets
    indexed and is searchable. Uses type="reference" (in VALID_TYPES).
    """
    lint_dir = vault_path / "_lint"
    lint_dir.mkdir(parents=True, exist_ok=True)

    # Derive today's datestamp from the stats timestamp so the filename and
    # report body always agree — avoids a midnight drift if lint ran at 23:59
    # but write_lint_report is called after 00:00 UTC.
    linted_at = stats.get("linted_at") or (datetime.utcnow().isoformat() + "Z")
    today = linted_at[:10]  # "YYYY-MM-DD"
    report_path = lint_dir / f"{today}.md"

    # Build report body
    total = stats.get("total_files_scanned", 0)

    lines: list[str] = [
        f"# Vault Lint Report — {today}",
        "",
        f"Scanned **{total}** files at `{linted_at}`.",
        "",
    ]

    # Orphans section
    orphan_count = stats.get("orphan_count", 0)
    lines.append(f"## Orphan Files ({orphan_count})")
    lines.append("")
    orphans = stats.get("orphans", [])
    if orphans:
        for rel in orphans:
            stem = Path(rel).stem
            # Pipe-alias: [[stem|rel]] keeps link resolution while showing the
            # full path — prevents collision when two files share the same stem.
            lines.append(f"- [[{stem}|{rel}]]")
    else:
        lines.append("_No orphan files found._")
    lines.append("")

    # Frontmatter invalid section
    fm_count = stats.get("frontmatter_invalid_count", 0)
    lines.append(f"## Frontmatter Issues ({fm_count})")
    lines.append("")
    fm_invalid = stats.get("frontmatter_invalid", [])
    if fm_invalid:
        for entry in fm_invalid:
            rel = entry.get("path", "?")
            reason = entry.get("reason", "unknown")
            stem = Path(rel).stem
            lines.append(f"- [[{stem}|{rel}]] — {reason}")
    else:
        lines.append("_No frontmatter issues found._")
    lines.append("")

    # Duplicate IDs section
    dup_count = stats.get("duplicate_id_count", 0)
    lines.append(f"## Duplicate IDs ({dup_count})")
    lines.append("")
    duplicates = stats.get("duplicate_ids", [])
    if duplicates:
        for entry in duplicates:
            nid = entry.get("id", "?")
            paths = entry.get("paths", [])
            path_links = ", ".join(f"[[{Path(p).stem}|{p}]]" for p in paths)
            lines.append(f"- `{nid}`: {path_links}")
    else:
        lines.append("_No duplicate IDs found._")
    lines.append("")

    # Broken Wikilinks section
    bw_count = stats.get("broken_wikilink_count", 0)
    lines.append(f"## Broken Wikilinks ({bw_count})")
    lines.append("")
    broken_wikilinks = stats.get("broken_wikilinks", [])
    if broken_wikilinks:
        for entry in broken_wikilinks:
            src = entry.get("source", "?")
            target = entry.get("target", "?")
            lines.append(f"- `{src}` -> [[{target}]] (not found)")
    else:
        lines.append("_No broken wikilinks found._")
    lines.append("")

    # Missing Confidence section
    mc_count = stats.get("missing_confidence_count", 0)
    lines.append(f"## Missing Confidence ({mc_count})")
    lines.append("")
    missing_conf = stats.get("missing_confidence", [])
    if missing_conf:
        for rel in missing_conf:
            stem = Path(rel).stem
            lines.append(f"- [[{stem}|{rel}]]")
    else:
        lines.append("_All notes have a confidence field._")
    lines.append("")

    body = "\n".join(lines)

    # Frontmatter for the report itself — must pass validate_frontmatter
    fm: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "type": "reference",
        "agent": "system",
        "date": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lint_orphans": orphan_count,
        "lint_fm_invalid": fm_count,
        "lint_dup_ids": dup_count,
        "lint_broken_wikilinks": bw_count,
        "lint_missing_confidence": mc_count,
        "lint_total_files": total,
    }

    post = frontmatter.Post(body, **fm)
    # Overwrite is intentional: last daily run wins (no incremental append).
    report_path.write_text(frontmatter.dumps(post))

    logger.info("Lint report written: %s", report_path)
    return report_path
