"""Architektur E Teil 2: der Worker darf app.main NICHT importieren.

Der Sinn des eigenen Worker-Prozesses ist Prozesstrennung: der komplette
FastAPI-Rumpf (app = FastAPI(...) + 61 include_router() + CORS/Rate-Limit-
Middleware + Verticals-Discovery) soll nur in der API laufen. Zieht der
Worker app.main an, ist die Trennung nichts wert — ein Importfehler in
irgendeinem Router legt dann wieder beide Prozesse lahm (die Schuld aus
Teil 1, siehe Docstring in app/worker.py).

Geprueft wird doppelt:
1. statisch: der Quelltext von app/worker.py enthaelt keinen app.main-Import
   (fängt auch future Re-Imports),
2. dynamisch: nach `import app.worker` ist app.main nicht in sys.modules —
   der Beweis, dass der Importgraph wirklich ohne main auskommt.
"""

import ast
import inspect
import subprocess
import sys
from pathlib import Path


def test_worker_source_has_no_app_main_import():
    here = Path(__file__).resolve()
    worker_src = (here.parent.parent / "app" / "worker.py").read_text()
    tree = ast.parse(worker_src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mods = [node.module or ""]
        else:
            continue
        for m in mods:
            assert not m.startswith("app.main"), (
                f"app/worker.py importiert '{m}' — der Worker darf app.main "
                "nicht anziehen (FastAPI-Rumpf gehoert nur in die API, "
                "Architektur E Teil 2). Boot-Pfad lives in app.background."
            )


def test_importing_worker_leaves_app_main_out_of_sys_modules():
    # Eigener Interpreter: im Testprozess kann app.main schon geladen sein
    # (conftest/client fixture importiert es fuer die API-Tests). sys.modules
    # dort zu pruefen waere ein False-Negative/False-Positive — der Subprozess
    # ist die echte Boot-Situation `python -m app.worker`.
    backend_root = Path(__file__).resolve().parent.parent
    code = (
        "import sys, app.worker; "
        "assert 'app.main' not in sys.modules, "
        "'app.worker import zog app.main an: ' + str(sorted(m for m in sys.modules if m.startswith('app.')))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=backend_root,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"worker import failed or pulled app.main:\n{result.stdout}\n{result.stderr}"
    )
