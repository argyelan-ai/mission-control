"""The live recipe catalog (config/local-recipes.json) ships only what MC supports.

The catalog is what a fresh install seeds and what the Runtimes page offers.
It lists only recipes that install and start with one click on DGX Spark —
an entry without an install and a launch template would show a button that
cannot finish. The seed MECHANICS (every template shape, the legacy entries)
are tested against the frozen fixture in tests/fixtures/local-recipes-legacy.json.
"""

import json
from pathlib import Path

LIVE_SEED = Path(__file__).resolve().parents[1] / "config" / "local-recipes.json"


def _entries():
    return json.loads(LIVE_SEED.read_text(encoding="utf-8"))


def test_live_catalog_is_not_empty_and_has_unique_slugs():
    slugs = [e["slug"] for e in _entries()]
    assert slugs, "the live catalog must ship at least one recipe"
    assert len(slugs) == len(set(slugs)), "duplicate slug in the live catalog"


def test_every_live_recipe_installs_and_starts_with_one_click():
    incomplete = [
        e["slug"]
        for e in _entries()
        if not (e.get("install_template") and e.get("launch_template") and e.get("stop_template"))
    ]
    assert incomplete == [], f"recipes without install/launch/stop template: {incomplete}"


def test_every_live_recipe_credits_its_author():
    missing = [e["slug"] for e in _entries() if not (e.get("author") and e.get("author_url"))]
    assert missing == [], f"recipes without author credit: {missing}"


def test_live_catalog_is_public_safe():
    """The catalog ships in a PUBLIC repo — no private hosts, no home paths."""
    raw = LIVE_SEED.read_text(encoding="utf-8")
    scanned = raw.replace("ssh_process", "<engine>").replace("ssh-process", "<engine>")
    scanned = scanned.replace("{worker_ssh}", "<role>").replace("{head_ssh}", "<role>")
    for forbidden in ("192" ".168.", "100" ".", "/Users" "/", "tail", "ssh"):  # split literals: the leak scanner should hit the catalog, not this test
        assert forbidden not in scanned, f"live catalog leaks {forbidden!r}"
