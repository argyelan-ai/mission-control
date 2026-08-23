"""Die Wache gegen neue Einschleppungen — und gegen sich selbst.

Der Sweep von Hand war zu 93 % unvollstaendig, und das ist auch der Grund,
warum Handarbeit hier der falsche Mechanismus ist: Namen kommen wieder rein,
sobald niemand hinschaut. `scripts/privacy-scan.py` schaut bei jedem Push hin.

Die Baseline (docs/privacy-sweep-backlog.md) macht den Bestand gruen, ohne ihn
zu amnestieren: verschwindet ein Name aus einer gelisteten Datei, wird die
Pruefung ebenfalls rot und verlangt, die Zeile zu streichen. So kann der
Rueckstand nur kuerzer werden.

Geprueft wird gegen echte Wegwerf-Repos, nicht gegen Attrappen.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN = _REPO_ROOT / "scripts" / "privacy-scan.py"
BACKLOG_REL = "docs/privacy-sweep-backlog.md"

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}

DOC_TEMPLATE = """# Privacy sweep

<!-- privacy-baseline:begin -->
```
{baseline}
```
<!-- privacy-baseline:end -->
"""


def _run(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", str(SCAN), "--root", str(root), *args],
        cwd=root, capture_output=True, text=True, env=_GIT_ENV, timeout=120,
    )


@pytest.fixture
def repo(tmp_path: Path):
    """Ein Wegwerf-Repo mit einer bekannten Altlast und leerer Weste sonst."""
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "src").mkdir()
    # Altlast: eine Datei, die den Namen schon traegt.
    (root / "src" / "legacy.py").write_text("# sparky runs the omp bridge\n", encoding="utf-8")
    (root / "src" / "clean.py").write_text("# nothing to see here\n", encoding="utf-8")
    (root / BACKLOG_REL).write_text(
        DOC_TEMPLATE.format(baseline="src/legacy.py: sparky"), encoding="utf-8"
    )
    subprocess.run(["git", "init", "-b", "main"], cwd=root, capture_output=True, env=_GIT_ENV)
    subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True, env=_GIT_ENV)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, capture_output=True, env=_GIT_ENV)
    return root


def test_known_backlog_is_green(repo: Path):
    """Der Bestand blockiert niemanden — sonst schaltet die Pruefung beim
    ersten roten Lauf jemand wieder ab."""
    proc = _run(repo)
    assert proc.returncode == 0, proc.stderr


def test_a_new_occurrence_turns_it_red(repo: Path):
    """Der eigentliche Zweck: derselbe Name, aber in einer Datei, die die
    Baseline nicht kennt."""
    (repo / "src" / "clean.py").write_text("AGENT = 'davinci'\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, env=_GIT_ENV)

    proc = _run(repo)
    assert proc.returncode != 0, "die neue Fundstelle blieb unbemerkt"
    assert "src/clean.py" in proc.stderr
    assert "davinci" in proc.stderr


def test_a_second_name_in_a_known_file_turns_it_red(repo: Path):
    """Eine gelistete Datei ist kein Freibrief fuer weitere Namen."""
    (repo / "src" / "legacy.py").write_text(
        "# sparky and shakespeare\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, env=_GIT_ENV)

    proc = _run(repo)
    assert proc.returncode != 0
    assert "shakespeare" in proc.stderr


def test_untracked_files_are_out_of_scope(repo: Path):
    """Nur was versioniert ist, kann veroeffentlicht werden — lokale Notizen
    duerfen niemandes CI rot faerben."""
    (repo / "src" / "scratch.py").write_text("# sparky\n", encoding="utf-8")
    assert _run(repo).returncode == 0


def test_cleaning_a_file_up_asks_for_the_backlog_entry(repo: Path):
    """Die andere Richtung: der Rueckstand muss schrumpfen, nicht bloss
    beschreiben, was frueher mal da war."""
    (repo / "src" / "legacy.py").write_text("# the omp bridge\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, env=_GIT_ENV)

    proc = _run(repo)
    assert proc.returncode != 0
    assert "no longer contain" in proc.stderr
    assert "--update-baseline" in proc.stderr


def test_update_baseline_writes_back_what_it_found(repo: Path):
    (repo / "src" / "legacy.py").write_text("# the omp bridge\n", encoding="utf-8")
    (repo / "src" / "clean.py").write_text("# freecode\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, env=_GIT_ENV)

    assert _run(repo, "--update-baseline").returncode == 0
    doc = (repo / BACKLOG_REL).read_text(encoding="utf-8")
    assert "src/clean.py: freecode" in doc
    assert "src/legacy.py" not in doc
    # Und danach ist wieder gruen.
    assert _run(repo).returncode == 0


def test_the_scanner_and_its_backlog_do_not_report_themselves(repo: Path):
    """Beide muessen die Namen tragen, um ihre Arbeit zu tun — der Wach-Test
    aus Befund 5 in gross."""
    proc = _run(repo)
    assert "privacy-scan.py" not in proc.stderr
    assert BACKLOG_REL not in proc.stderr


# ── Und gegen das echte Repo ────────────────────────────────────────────────


def test_this_repository_is_green_right_now():
    """Waere sie rot, waere der leak-gate-Job rot — und jemand schaltete sie ab."""
    proc = subprocess.run(
        ["python3", str(SCAN)], cwd=_REPO_ROOT,
        capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stderr


def test_the_office_page_is_off_the_backlog():
    """Die produktsichtbarste Stelle — ein vollstaendiges Organigramm der
    Flotte, live unter /office — muss aufgeraeumt sein, nicht nur gelistet.

    Geprueft wird der Baseline-Block, nicht der Fliesstext: die Prosa nennt die
    Datei absichtlich als Beispiel fuer erledigte Arbeit.
    """
    doc = (_REPO_ROOT / BACKLOG_REL).read_text(encoding="utf-8")
    baseline = doc.split("<!-- privacy-baseline:begin -->", 1)[1].split(
        "<!-- privacy-baseline:end -->", 1
    )[0]
    assert "org-chart-data.ts" not in baseline


def test_every_exemption_is_earned_and_the_list_stays_small():
    """`SELF` ist die Ausnahmeliste — und damit die einzige blinde Stelle.

    Zwei Zusicherungen: jeder Eintrag existiert und traegt wirklich einen der
    Namen (sonst ist die Ausnahme Dekoration und niemand merkt, dass sie
    veraltet ist), und die Liste bleibt kurz. Waechst sie, ist das kein
    Wartungsdetail, sondern das Ende der Pruefung auf Raten.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("privacy_scan", SCAN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert len(mod.SELF) <= 6, f"die Ausnahmeliste waechst: {mod.SELF}"
    for rel in mod.SELF:
        path = _REPO_ROOT / rel
        assert path.exists(), f"{rel} steht in SELF, existiert aber nicht"
        text = path.read_text(encoding="utf-8")
        assert any(n in text.lower() for n in mod.FLEET_NAMES), (
            f"{rel} braucht die Ausnahme gar nicht (mehr) — raus damit"
        )
