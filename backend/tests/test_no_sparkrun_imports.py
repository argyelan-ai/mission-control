"""Regression: sparkrun ist kein Konzept mehr (Vertrag Rezept-Umschalter, 02.09.2026).

Nach dem Rückbau darf unter ``backend/app/`` nichts mehr ``sparkrun_manager``
importieren, es darf keinen ``switch-recipe``-Endpunkt und kein
``sparkrun_managed``-Feld mehr geben. Ein sparkrun-Rezept ist ein gewöhnlicher
Startbefehl (``uvx sparkrun run …``) — der darf natürlich weiter in
Startbefehlen und im Container-Namensmuster ``sparkrun_*_solo`` der
Verdrängung vorkommen. Muster wie ``tests/test_no_gateway_imports.py``.
"""

from __future__ import annotations

import pathlib
import re

import pytest


def _find_app_root() -> pathlib.Path:
    here = pathlib.Path(__file__).resolve()
    for ancestor in here.parents:
        if (ancestor / "app" / "main.py").is_file():
            return ancestor / "app"
    raise RuntimeError(f"app/main.py nicht gefunden oberhalb von {here}")


APP_ROOT = _find_app_root()
EXCLUDED_PARTS = ("__pycache__", "alembic", "versions")

FORBIDDEN = (
    re.compile(r"\bsparkrun_manager\b"),
    re.compile(r"switch-recipe"),
    re.compile(r"\bswitch_recipe\("),  # der alte Aufruf; der Grace-Quellwert "switch_recipe" in Redis bleibt
    re.compile(r"\bsparkrun_managed\b"),
    re.compile(r"uvx sparkrun list"),
)


def _iter_python_files():
    for path in APP_ROOT.rglob("*.py"):
        if any(p in EXCLUDED_PARTS for p in path.parts):
            continue
        yield path


@pytest.mark.parametrize("pattern", FORBIDDEN, ids=lambda p: p.pattern)
def test_no_sparkrun_concept_left_in_app(pattern):
    offenders = []
    for path in _iter_python_files():
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            line_no = text[: match.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(APP_ROOT.parent)}:{line_no} — {match.group()}")
    assert not offenders, f"sparkrun-Konzept {pattern.pattern!r} noch vorhanden:\n  " + "\n  ".join(offenders)


def test_sparkrun_manager_file_is_gone():
    assert not (APP_ROOT / "services" / "sparkrun_manager.py").exists()


def test_recipe_switcher_is_the_only_recipe_path():
    assert (APP_ROOT / "services" / "recipe_switcher.py").exists()
    assert (APP_ROOT / "routers" / "host_recipes.py").exists()
