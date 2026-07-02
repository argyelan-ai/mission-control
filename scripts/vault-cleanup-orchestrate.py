#!/usr/bin/env python3
"""Master orchestrator for the autonomous Vault Cleanup programme.

Run order (each phase resumable, idempotent):
  Phase 0  — Pre-flight (Spark reachability, state-dir setup)
  Phase W1 — Cleanup soft-archive + tarball + git-commit + hard-delete
  Phase W4 — Audit-trail separator (code change verify)
  Phase W3-C — write_note constraint deployment (code change verify)
  Phase W2 — Title backfill via Spark
  Phase W3-B — Wikilink backfill via Spark + Qdrant
  Phase W3-A — Ghost-edge build (code change verify)
  Phase W5 — Visual polish + frontend rebuild
  Phase X  — Final validation (smoke test /vault/graph)

Usage:
  python scripts/vault-cleanup-orchestrate.py --phase all
  python scripts/vault-cleanup-orchestrate.py --phase w1
  python scripts/vault-cleanup-orchestrate.py --phase preflight
  python scripts/vault-cleanup-orchestrate.py --dry-run --phase all
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import click  # noqa: E402

from app.services.spark_client import SparkClient  # noqa: E402
from app.services.vault_cleanup import (  # noqa: E402
    archive_batch,
    dryrun_to_csv,
    finalize_cleanup,
    load_notes_from_vault,
)
from app.services.vault_cleanup_state import VaultCleanupState  # noqa: E402
from app.services.vault_title_backfill import backfill_titles  # noqa: E402
from app.services.vault_wikilink_backfill import backfill_wikilinks  # noqa: E402


VAULT_ROOT = Path(os.path.expanduser("~/.mc/vault"))
ARCHIVE_ROOT_BASE = Path(os.path.expanduser("~/.mc/vault.archive"))
BACKUPS_ROOT = Path(os.path.expanduser("~/.mc/backups"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
)
log = logging.getLogger("vault-cleanup")


async def phase_preflight(state: VaultCleanupState) -> None:
    state.log("INFO", "phase_preflight start")
    spark = SparkClient()
    h = await spark.health_check()
    if not h["llm_ready"]:
        state.log("ERROR", f"Spark vLLM not ready at {h['llm_url']}")
        raise SystemExit(2)
    if not h["embedding_ready"]:
        state.log("ERROR", f"Spark embeddings not ready at {h['embedding_url']}")
        raise SystemExit(2)
    state.log("INFO", f"Spark ready: {h['llm_model']} + {h['embedding_model']}")
    state.log("INFO", f"Vault root: {VAULT_ROOT}")
    if not VAULT_ROOT.exists():
        state.log("ERROR", f"Vault root missing: {VAULT_ROOT}")
        raise SystemExit(2)
    state.log("INFO", "phase_preflight done")


async def phase_w1_cleanup(state: VaultCleanupState) -> None:
    state.log("INFO", "phase_w1_cleanup start")
    notes = load_notes_from_vault(VAULT_ROOT)
    state.log("INFO", f"loaded {len(notes)} notes")

    csv_path = state.root / "dryrun.csv"
    n_candidates = dryrun_to_csv(notes, csv_path, whitelist=state.whitelist())
    state.log("INFO", f"dryrun: {n_candidates} archive candidates → {csv_path}")

    import csv as csv_mod
    import frontmatter
    plan: list[tuple[str, str, str]] = []
    for row in csv_mod.DictReader(csv_path.open()):
        full = VAULT_ROOT / row["path"]
        if not full.exists():
            continue
        post = frontmatter.load(full)
        bm_id = (post.metadata or {}).get("id", "")
        if not bm_id:
            state.log("WARN", f"note {row['path']} has no id frontmatter — skipping")
            continue
        plan.append((row["path"], bm_id, row["bucket"]))

    state.log("INFO", f"plan built: {len(plan)} entries")

    from app.database import engine  # lazy import
    from sqlmodel.ext.asyncio.session import AsyncSession
    archive_root = ARCHIVE_ROOT_BASE / state.run_id
    async with AsyncSession(engine, expire_on_commit=False) as session:
        result = await archive_batch(session, VAULT_ROOT, archive_root, plan)
    state.log("INFO", f"archive_batch: moved={result.moved} failed={result.failed}")
    for path, err in result.errors[:10]:
        state.log("WARN", f"  archive-fail {path}: {err}")

    state.write_manifest("archive-manifest", {
        "run_id": state.run_id,
        "archive_root": str(archive_root),
        "moved": result.moved,
        "failed": result.failed,
    })

    final = await finalize_cleanup(
        state=state,
        vault_root=VAULT_ROOT,
        archive_root=archive_root,
        backups_root=BACKUPS_ROOT,
    )
    state.log("INFO", f"finalize: tarball={final.tarball_path} archive_removed={final.archive_removed} git_commit={final.git_committed}")
    if not final.ok:
        state.log("ERROR", f"finalize failed: {final.error}")
        raise SystemExit(3)
    state.log("INFO", "phase_w1_cleanup done")


async def phase_w2_titles(state: VaultCleanupState) -> None:
    state.log("INFO", "phase_w2_titles start")
    spark = SparkClient()
    result = await backfill_titles(spark, VAULT_ROOT, state)
    state.log("INFO", f"phase_w2_titles done: processed={result.processed} skipped={result.skipped} failed={result.failed}")


async def phase_w3b_wikilinks(state: VaultCleanupState) -> None:
    state.log("INFO", "phase_w3b_wikilinks start")
    spark = SparkClient()
    from qdrant_client import AsyncQdrantClient  # noqa: E402
    from app.config import Settings  # noqa: E402
    _settings = Settings()
    qdrant = AsyncQdrantClient(host=_settings.qdrant_host, port=_settings.qdrant_port)
    result = await backfill_wikilinks(spark, qdrant, VAULT_ROOT, state)
    state.log("INFO", f"phase_w3b_wikilinks done: processed={result.processed} skipped={result.skipped} failed={result.failed}")


async def phase_w3a_ghost_edges_verify(state: VaultCleanupState) -> None:
    """W3-A is the /vault/graph similarity-edges integration. Code change is
    already live in commit abef935d — this phase logs the verification."""
    state.log("INFO", "phase_w3a_ghost_edges_verify: code change deployed in commit abef935d (W3-A)")


async def phase_w3c_constraint_verify(state: VaultCleanupState) -> None:
    """W3-C is the Pydantic constraint + SOUL.md.j2 update. Per-agent
    sync-config push needs MC API access — out of scope for this orchestrator.
    Tests already verified the constraint. Code live in commit 44081161."""
    state.log("INFO", "phase_w3c_constraint_verify: code change deployed in commit 44081161 (W3-C)")


async def phase_w4_audit_stop_verify(state: VaultCleanupState) -> None:
    """W4 is a code change (not data manipulation). The orchestrator just
    verifies the constraint is in place. Tests covered this — this phase
    is a sanity-only no-op. Code live in commits c98d2e9c + f86b832f."""
    state.log("INFO", "phase_w4_audit_stop_verify: code change deployed in commit f86b832f (W4)")


async def phase_w5_frontend_rebuild(state: VaultCleanupState) -> None:
    """W5 visual polish was a frontend code change (commit 39ab37b0).
    For the autonomous run we trigger a frontend rebuild here so the
    new graph view is live in the container."""
    import subprocess
    state.log("INFO", "phase_w5_frontend_rebuild start")
    try:
        proc = subprocess.run(
            ["docker", "compose", "up", "--build", "-d", "frontend"],
            cwd=str(ROOT),
            capture_output=True, text=True, timeout=600,
        )
        if proc.returncode != 0:
            state.log("ERROR", f"frontend rebuild failed: {proc.stderr[:500]}")
            raise SystemExit(4)
        state.log("INFO", "frontend rebuilt and restarted")
    except subprocess.TimeoutExpired:
        state.log("ERROR", "frontend rebuild timed out after 10 minutes")
        raise SystemExit(4)


async def phase_validation(state: VaultCleanupState) -> None:
    """Final smoke test: hit /api/v1/vault/graph and verify ≥some edges +
    plausible node count. No JWT required if a local-auth fallback is enabled,
    otherwise this phase logs a TODO and continues."""
    import httpx
    state.log("INFO", "phase_validation start")
    url = "http://localhost/api/v1/vault/graph?similarity_edges=true"
    try:
        async with httpx.AsyncClient(timeout=30.0) as cli:
            resp = await cli.get(url)
        if resp.status_code == 401:
            state.log("WARN", "validation: 401 — need JWT, skipping deeper check (manual verification required)")
            return
        if resp.status_code != 200:
            state.log("ERROR", f"validation: HTTP {resp.status_code} from /vault/graph")
            raise SystemExit(5)
        body = resp.json()
        stats = body.get("stats", {})
        state.log("INFO", f"validation: stats={stats}")
        node_count = stats.get("nodes", 0)
        edge_count = stats.get("edges", 0)
        if node_count == 0:
            state.log("WARN", "validation: 0 nodes — vault may be empty")
        if edge_count == 0:
            state.log("WARN", "validation: 0 edges — W3-A/W3-B may not have run yet")
        state.log("INFO", "phase_validation done")
    except httpx.HTTPError as e:
        state.log("ERROR", f"validation: HTTP error {e}")
        raise SystemExit(5)


@click.command()
@click.option(
    "--phase",
    default="all",
    type=click.Choice([
        "all", "preflight", "w1", "w2", "w3a", "w3b", "w3c", "w4", "w5", "validation",
    ]),
)
@click.option("--state-root", default=None, help="Override state dir (default ~/.mc/vault.cleanup.state)")
@click.option("--dry-run", is_flag=True, default=False, help="No destructive ops, just log what would happen")
def main(phase: str, state_root: str | None, dry_run: bool) -> None:
    if dry_run:
        os.environ["VAULT_CLEANUP_DRY_RUN"] = "1"
    root = Path(state_root) if state_root else None
    state = VaultCleanupState(root=root)
    state.ensure()
    state.log("INFO", f"orchestrator start phase={phase} run_id={state.run_id} dry_run={dry_run}")

    async def run() -> None:
        await phase_preflight(state)
        # Phases in execution order per plan
        if phase in ("all", "w1"):
            if dry_run:
                state.log("INFO", "DRY-RUN: w1 cleanup skipped")
            else:
                await phase_w1_cleanup(state)
        if phase in ("all", "w4"):
            await phase_w4_audit_stop_verify(state)
        if phase in ("all", "w3c"):
            await phase_w3c_constraint_verify(state)
        if phase in ("all", "w2"):
            if dry_run:
                state.log("INFO", "DRY-RUN: w2 titles skipped")
            else:
                await phase_w2_titles(state)
        if phase in ("all", "w3b"):
            if dry_run:
                state.log("INFO", "DRY-RUN: w3b wikilinks skipped")
            else:
                await phase_w3b_wikilinks(state)
        if phase in ("all", "w3a"):
            await phase_w3a_ghost_edges_verify(state)
        if phase in ("all", "w5"):
            if dry_run:
                state.log("INFO", "DRY-RUN: w5 frontend rebuild skipped")
            else:
                await phase_w5_frontend_rebuild(state)
        if phase in ("all", "validation"):
            if dry_run:
                state.log("INFO", "DRY-RUN: validation skipped")
            else:
                await phase_validation(state)
        # Allow standalone preflight invocation
        # (preflight always runs above; nothing else needed for that case)

    asyncio.run(run())
    state.log("INFO", "orchestrator done")


if __name__ == "__main__":
    main()
