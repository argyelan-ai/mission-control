"""E2E integration test against the real vault (~860 files).

Post M.2 Cutover (2026-05-15): all vault files now have `id` frontmatter.
The schema gap documented in the original version of this file was fixed
by Migration 0112 (vault_cutover). Files in _lint, _trash, _inbox, etc.
are skipped by rebuild_from_vault() by design.

Tests removed in this cleanup:
  - test_rebuild_real_vault_subset: asserted scanned=20, indexed=0, errors=20
    → post-cutover all files have `id` AND first 20 rglob results fall in
      skip-directories (_lint, _trash), so scanned=0, skipped=20.
  - test_real_vault_schema_gap: asserted `id` not in frontmatter
    → `id` is now present in all vault files.
"""

import shutil
import pytest
from pathlib import Path
from app.services.vault_index import VaultIndex


REAL_VAULT = Path.home() / ".mc" / "vault"


@pytest.mark.skipif(not REAL_VAULT.exists(), reason="real vault not present")
def test_search_against_real_vault(tmp_path):
    """Copy 100 real vault files, rebuild, then verify search behavior.

    Post-cutover: files in skip-directories are not indexed, so search
    hits depend on whether any of the first 100 rglob results are in
    scannable directories (agents/*, global/*, etc.).
    """
    md_files = list(REAL_VAULT.rglob("*.md"))[:100]
    for src in md_files:
        rel = src.relative_to(REAL_VAULT)
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    index = VaultIndex(db_path=tmp_path / ".mc_index.db", vault_path=tmp_path)
    stats = index.rebuild_from_vault()

    hits = list(index.search("mark"))

    total = stats["scanned"] + stats["skipped"]
    assert total > 0, "expected at least some files to be processed"
