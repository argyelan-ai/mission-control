"""Vault Cleanup — heuristic-based soft-archive of audit-trail noise.

W1 of the vault cleanup programme. Identifies ~576/881 notes that are
auto-emitted telemetry rather than knowledge, moves them to
~/.mc/vault.archive/<run-id>/, and marks board_memory.archived_at.

Three heuristics (each tested independently):
  H1 — system journal + auto tag (≈388 notes, 98% precision)
  H2 — reflection_fold OR length<150 (≈240 notes, 95%)
  H3 — failed-task echoes OR test agent (≈70 notes, 95%)

Whitelist (~/.mc/vault.cleanup.state/whitelist.txt) rescues false positives.
"""

from __future__ import annotations

import csv
import datetime as dt
import shutil
import subprocess
import tarfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import frontmatter

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession


@dataclass
class NoteSample:
    """Lightweight in-memory view of a vault note used by cleanup heuristics.

    Loaded from disk by load_notes_from_vault (see Task 1.5). Plain dataclass
    so it is trivially testable without DB/IO fixtures."""

    path: str
    agent: str
    note_type: str
    tags: list[str]
    content: str


def is_h1_audit_trail(note: NoteSample) -> bool:
    """H1: system-emitted journal entries with auto tag."""
    return (
        note.agent == "system"
        and note.note_type == "journal"
        and "auto" in note.tags
    )


def is_h2_reflection_or_stub(note: NoteSample) -> bool:
    """H2: reflection_fold tag OR very short content (pointer stubs)."""
    if "reflection_fold" in note.tags:
        return True
    if len(note.content.strip()) < 150:
        return True
    return False


def is_h3_test_or_failed(note: NoteSample) -> bool:
    """H3: test agents, failed-task echoes, test-project tag."""
    if note.agent == "tester":
        return True
    if "test-project" in note.tags:
        return True
    if note.content.startswith("**Task fehlgeschlagen:**"):
        return True
    return False


def classify(note: NoteSample) -> tuple[str, float] | None:
    """Return (bucket, confidence) if any heuristic matches, else None.

    Order matters: H1 wins over H2 when both match (more specific) — same
    for H2 over H3 when ambiguous. Confidence values are empirical from
    the operator's 2026-05-15 data sample (881 notes)."""
    if is_h1_audit_trail(note):
        return ("H1", 0.98)
    if is_h2_reflection_or_stub(note):
        return ("H2", 0.95)
    if is_h3_test_or_failed(note):
        return ("H3", 0.95)
    return None


def dryrun_to_csv(
    notes: list[NoteSample],
    output: Path,
    whitelist: set[str] | None = None,
) -> int:
    """Classify notes, write CSV of soft-archive candidates. Returns count.

    Output columns: path, agent, type, length, tags, bucket, confidence.
    Sorted by (bucket, descending confidence) so high-confidence H1 entries
    appear first in the operator's review."""
    whitelist = whitelist or set()
    fieldnames = ["path", "agent", "type", "length", "tags", "bucket", "confidence"]
    rows: list[dict] = []
    for n in notes:
        if n.path in whitelist:
            continue
        classified = classify(n)
        if classified is None:
            continue
        bucket, conf = classified
        rows.append({
            "path": n.path,
            "agent": n.agent,
            "type": n.note_type,
            "length": len(n.content),
            "tags": ",".join(n.tags),
            "bucket": bucket,
            "confidence": f"{conf:.2f}",
        })
    rows.sort(key=lambda r: (r["bucket"], -float(r["confidence"])))
    with output.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def load_notes_from_vault(vault_root: Path) -> list[NoteSample]:
    """Walk the vault, parse frontmatter, return NoteSample list.

    Skips _inbox/, _rejected/, _conflicts/ — those are operational, not
    canonical knowledge. Notes without frontmatter are returned with empty
    agent/type/tags but with their content preserved."""
    SKIP_DIRS = {"_inbox", "_rejected", "_conflicts"}
    out: list[NoteSample] = []
    for path in vault_root.rglob("*.md"):
        rel = path.relative_to(vault_root)
        if rel.parts[0] in SKIP_DIRS:
            continue
        try:
            post = frontmatter.load(path)
        except Exception:
            continue
        meta = post.metadata or {}
        out.append(NoteSample(
            path=str(rel),
            agent=meta.get("agent", "") or "",
            note_type=meta.get("type", "") or "",
            tags=meta.get("tags", []) or [],
            content=post.content or "",
        ))
    return out


@dataclass
class ArchiveResult:
    """Result of a single soft-archive attempt."""

    ok: bool
    already_archived: bool = False
    error: str | None = None


_BUCKET_REASONS = {
    "H1": "auto_system_journal",
    "H2": "reflection_fold_or_stub",
    "H3": "test_or_failed",
}


def soft_archive_note(
    vault_root: Path,
    archive_root: Path,
    rel_path: str,
    bucket: str,
) -> ArchiveResult:
    """Move a single note from the live vault into the archive root.

    Annotates frontmatter with archived_at + archive_bucket + archive_reason.
    Idempotent: if the source is already moved (missing from vault, present
    in archive) returns ok=True, already_archived=True. If neither location
    has the file, returns ok=False with a 'not found' error."""
    src = vault_root / rel_path
    dst = archive_root / rel_path

    if not src.exists() and dst.exists():
        return ArchiveResult(ok=True, already_archived=True)
    if not src.exists() and not dst.exists():
        return ArchiveResult(ok=False, error="source not found")

    try:
        post = frontmatter.load(src)
        post.metadata["archived_at"] = dt.datetime.utcnow().isoformat()
        post.metadata["archive_bucket"] = bucket
        post.metadata["archive_reason"] = _BUCKET_REASONS.get(bucket, "unknown")
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(frontmatter.dumps(post))
        src.unlink()
        return ArchiveResult(ok=True, already_archived=False)
    except Exception as e:
        return ArchiveResult(ok=False, error=str(e))


@dataclass
class BatchResult:
    """Aggregate result of an archive_batch call."""

    total: int
    moved: int
    failed: int
    errors: list[tuple[str, str]] = field(default_factory=list)


async def archive_batch(
    session: "AsyncSession",
    vault_root: Path,
    archive_root: Path,
    plan: list[tuple[str, uuid.UUID, str]],
) -> BatchResult:
    """Process a list of (rel_path, board_memory_id, bucket) atomically per item.

    For each tuple: soft_archive_note + update BoardMemory row. Individual
    failures don't stop the batch — they're collected in `errors` for the
    operator to review afterwards.

    The transaction is committed once at the end. If a runtime error escapes
    here, the caller's session is responsible for rollback handling."""
    from app.models.memory import BoardMemory

    moved = 0
    failed = 0
    errors: list[tuple[str, str]] = []
    archived_at = dt.datetime.utcnow()

    for rel, bm_id, bucket in plan:
        result = soft_archive_note(vault_root, archive_root, rel, bucket)
        if not result.ok:
            failed += 1
            errors.append((rel, result.error or "unknown"))
            continue
        if not result.already_archived:
            row = await session.get(BoardMemory, bm_id)
            if row is not None:
                row.archived_at = archived_at
                row.archive_bucket = bucket
                row.archive_reason = _BUCKET_REASONS.get(bucket, "unknown")
                session.add(row)
        moved += 1

    await session.commit()
    return BatchResult(total=len(plan), moved=moved, failed=failed, errors=errors)


@dataclass
class FinalizeResult:
    ok: bool
    tarball_path: Path | None
    archive_removed: bool
    git_committed: bool
    error: str | None = None


async def finalize_cleanup(
    state: "VaultCleanupState",
    vault_root: Path,
    archive_root: Path,
    backups_root: Path,
    skip_git: bool = False,
) -> FinalizeResult:
    """Final phase of W1: tar.gz snapshot of live vault → vault-git-commit
    → hard-delete the soft-archive directory.

    Always safe to re-run: returns ok=True with archive_removed=False if
    there's nothing to clean up. skip_git=True is for unit tests that don't
    run inside a git repo."""
    state.log("INFO", "finalize_cleanup start")

    backups_root.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    tarball: Path | None = backups_root / f"vault-pre-cleanup-{state.run_id}-{timestamp}.tar.gz"

    if vault_root.exists():
        with tarfile.open(tarball, "w:gz") as tar:
            tar.add(vault_root, arcname="vault")
        state.log("INFO", f"tarball created: {tarball} ({tarball.stat().st_size} bytes)")
    else:
        state.log("WARN", f"vault root missing: {vault_root}")
        tarball = None

    git_committed = False
    if not skip_git and vault_root.exists() and (vault_root / ".git").exists():
        try:
            subprocess.run(
                ["git", "-C", str(vault_root), "add", "-A"],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(vault_root), "commit",
                 "-m", f"cleanup: soft-archive run {state.run_id}"],
                check=True, capture_output=True,
            )
            git_committed = True
            state.log("INFO", f"vault git commit done for run {state.run_id}")
        except subprocess.CalledProcessError as e:
            err_out = e.stderr.decode() if e.stderr else ""
            if "nothing to commit" in err_out:
                state.log("INFO", "vault git: nothing to commit (clean tree)")
            else:
                state.log("WARN", f"vault git commit failed: {err_out[:200]}")

    archive_removed = False
    if archive_root.exists():
        try:
            shutil.rmtree(archive_root)
            archive_removed = True
            state.log("INFO", f"archive hard-deleted: {archive_root}")
        except OSError as e:
            state.log("WARN", f"archive delete failed: {e}")
            return FinalizeResult(
                ok=False,
                tarball_path=tarball,
                archive_removed=False,
                git_committed=git_committed,
                error=str(e),
            )

    state.log("INFO", "finalize_cleanup done")
    return FinalizeResult(
        ok=True,
        tarball_path=tarball,
        archive_removed=archive_removed,
        git_committed=git_committed,
    )
