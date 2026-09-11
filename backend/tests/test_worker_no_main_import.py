"""Architektur E Teil 2: der Worker darf app.main NICHT importieren —
weder beim Modul-Import noch im Boot-Pfad.

Der Sinn des eigenen Worker-Prozesses ist Prozesstrennung: der komplette
FastAPI-Rumpf (app = FastAPI(...) + 61 include_router() + CORS/Rate-Limit-
Middleware + Verticals-Discovery) soll nur in der API laufen. Zieht der
Worker app.main an, ist die Trennung nichts wert — ein Importfehler in
irgendeinem Router legt dann wieder beide Prozesse lahm (die Schuld aus
Teil 1, siehe Docstring in app/worker.py).

Geprueft wird dreifach (Rex-Review PR #500, Blocker B2: die Vorversion
mass ausschliesslich die Import-Zeit und stand gruen, waehrend
prepare_process() per `from app.main import _seed_*` doch app.main anzog):

1. statisch: die Quelltexte von app/worker.py UND app/background.py
   enthalten keinen app.main-Import (der Regress sass in background.py —
   worker.py allein zu pruefen findet ihn prinzipiell nicht),
2. dynamisch Import-Zeit: nach `import app.worker` ist app.main nicht in
   sys.modules,
3. dynamisch Boot-Pfad: `asyncio.run(prepare_process())` im Subprozess
   (validate_boot_secrets gestubbt, DB-Zugriffe schlagen fehl — Abbruch
   wird toleriert, solange kein app.main-Routing geladen wurde) laesst
   app.main NICHT in sys.modules zurueck. Genau das ist die Eigenschaft,
   die im Container gilt.
"""

import ast
import subprocess
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_NO_MAIN_MODULES = ("app/worker.py", "app/background.py")


def test_worker_and_background_source_have_no_app_main_import():
    for rel in _NO_MAIN_MODULES:
        src = (_BACKEND_ROOT / rel).read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
            else:
                continue
            for m in mods:
                assert not m.startswith("app.main"), (
                    f"{rel} importiert '{m}' — der Worker darf app.main "
                    "nicht anziehen (FastAPI-Rumpf gehoert nur in die API, "
                    "Architektur E Teil 2). Seed-Helfer leben in app.seeds."
                )


def test_importing_worker_leaves_app_main_out_of_sys_modules():
    # Eigener Interpreter: im Testprozess kann app.main schon geladen sein
    # (conftest/client fixture importiert es fuer die API-Tests). sys.modules
    # dort zu pruefen waere ein False-Negative/False-Positive — der Subprozess
    # ist die echte Boot-Situation `python -m app.worker`.
    code = (
        "import sys, app.worker; "
        "assert 'app.main' not in sys.modules, "
        "'app.worker import zog app.main an: ' + str(sorted(m for m in sys.modules if m.startswith('app.')))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=_BACKEND_ROOT,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"worker import failed or pulled app.main:\n{result.stdout}\n{result.stderr}"
    )


def test_worker_boot_path_leaves_app_main_out_of_sys_modules():
    # B2-Kern: der BOOT-PFAD (prepare_process, Bootschritt 1 in worker.run())
    # darf app.main anziehen — nicht nur der Modul-Import. Hier sass der
    # Regress (from app.main import _seed_*), den die Vorversion nicht fand.
    #
    # validate_boot_secrets wird gestubbt (keine Env im CI); danach brechen
    # die DB-Seeds real (kein Postgres im Subprozess) — gewollt: egal wo
    # prepare_process abbricht, darf bis dahin kein app.main-Routing
    # geladen worden sein. Der Abbruch selbst wird toleriert; only failure
    # mode ist app.main in sys.modules (bzw. ein Importfehler davor).
    code = (
        "import asyncio, sys\n"
        "import app.worker\n"
        "import app.background as bg\n"
        "bg.validate_boot_secrets = lambda *a, **k: None\n"
        "try:\n"
        "    asyncio.run(bg.prepare_process())\n"
        "except BaseException:\n"
        "    pass\n"
        "loaded = sorted(m for m in sys.modules if m.startswith('app.routers'))\n"
        "assert 'app.main' not in sys.modules, (\n"
        "    'prepare_process() zog app.main an: ' + str(loaded)\n"
        ")\n"
        # B7 (Rex-Review PR #500, Folgefund 38d1d1b): die Fehlerklasse
        # "Router-Import zieht in den Worker" kehrte innerhalb dieses PRs
        # schon einmal zurueck (obsidian_export imports _attachments_root
        # aus app.routers.memory — 38d1d1b zog den Resolver nach
        # app.services.fs_roots). app.main bleibt False, solange nur der
        # Router geladen wird — deshalb die staerkere Eigenschaft: der
        # Worker-Boot-Pfad laedt GAR KEIN app.routers.*-Modul.
        "assert not loaded, (\n"
        "    'prepare_process() zog Router-Module in den Worker: '\n"
        "    + str(loaded)\n"
        ")\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=_BACKEND_ROOT,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"worker boot path pulled app.main:\n{result.stdout}\n{result.stderr}"
    )
