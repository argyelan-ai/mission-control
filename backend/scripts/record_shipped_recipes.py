"""Record the fingerprints of the builtin recipe catalog in local-recipes.shipped.json.

Why: on startup MC brings every builtin recipe row up to the shipped catalog,
but only when the row still holds a version that was shipped before — anything
else was edited locally and is kept (services/local_registry.
sync_builtin_recipes). This file is the list of "versions that were shipped".

Run it after every change to ``backend/config/local-recipes.json``:

    cd backend && python scripts/record_shipped_recipes.py

``--from-git`` also walks every committed version of the catalog (used once to
backfill the versions shipped before this file existed). Fingerprints are only
ever added, never removed: an install can still hold any of them.
``tests/test_local_registry_catalog_sync.py`` fails when the current catalog is
not recorded.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.services.local_registry import (  # noqa: E402
    _SEED_PATH,
    _parse_entries,
    _shipped_path,
    spec_fingerprint,
)

_COMMENT = (
    "Fingerprints of every builtin recipe version MC has shipped. A builtin row "
    "that still matches one of them follows the current catalog on startup; any "
    "other content counts as a local edit and is kept. Written by "
    "backend/scripts/record_shipped_recipes.py - append only."
)


def _fingerprints(payload, origin: str) -> dict[str, set[str]]:
    table: dict[str, set[str]] = {}
    for spec in _parse_entries(payload, origin=origin, reasons=[]):
        table.setdefault(spec.slug, set()).add(spec_fingerprint(spec))
    return table


def _git_versions() -> list[tuple[str, object]]:
    rel = _SEED_PATH.relative_to(BACKEND.parent).as_posix()
    shas = subprocess.run(
        ["git", "-C", str(BACKEND.parent), "log", "--format=%H", "--", rel],
        check=True, capture_output=True, text=True,
    ).stdout.split()
    versions = []
    for sha in shas:
        blob = subprocess.run(
            ["git", "-C", str(BACKEND.parent), "show", f"{sha}:{rel}"],
            capture_output=True, text=True,
        )
        if blob.returncode != 0:
            continue
        try:
            versions.append((sha[:10], json.loads(blob.stdout)))
        except json.JSONDecodeError:
            continue
    return versions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from-git", action="store_true", help="also record every committed catalog version")
    parser.add_argument("--check", action="store_true", help="exit 1 if the current catalog is not recorded")
    args = parser.parse_args()

    path = _shipped_path()
    existing: dict[str, set[str]] = {}
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        existing = {slug: set(fps) for slug, fps in (raw.get("fingerprints") or {}).items()}

    sources = [("working tree", json.loads(_SEED_PATH.read_text(encoding="utf-8")))]
    if args.from_git:
        sources += _git_versions()

    merged = {slug: set(fps) for slug, fps in existing.items()}
    for origin, payload in sources:
        for slug, fps in _fingerprints(payload, origin).items():
            merged.setdefault(slug, set()).update(fps)

    added = sum(len(fps - existing.get(slug, set())) for slug, fps in merged.items())
    if args.check:
        if added:
            print(f"{added} catalog fingerprint(s) not recorded - run {Path(__file__).name}")
            return 1
        print("catalog fingerprints recorded")
        return 0

    body = {
        "_comment": _COMMENT,
        "fingerprints": {slug: sorted(fps) for slug, fps in sorted(merged.items())},
    }
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{path.name}: {added} new fingerprint(s), {sum(map(len, merged.values()))} total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
