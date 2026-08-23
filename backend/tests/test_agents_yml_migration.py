"""Bestehende Installationen muessen den OSS-Split ueberleben.

`docker/docker-compose.agents.yml` lag in der Versionsverwaltung UND wird zur
Laufzeit fortgeschrieben. Beim `git pull` auf den Commit, der sie aus dem Repo
nimmt, passiert ohne Migration eines von zwei Dingen — beide schlecht:

(a) Entspricht die lokale Datei exakt dem eingecheckten Stand, loescht git sie
    STILLSCHWEIGEND. `start-all.sh` legt danach eine leere an: alle
    `mc-agent-*`-Dienste sind aus der Compose verschwunden, samt
    Hand-Anpassungen (eigene Mounts), die der Renderer nie wieder erzeugt.
(b) Weicht sie ab — der Normalfall, sie schreibt sich ja selbst fort — bricht
    `git pull --ff-only` ab. `install.sh --update` laeuft unter
    `set -euo pipefail`, also scheitert das Update komplett.

`scripts/migrate-agents-yml.sh` loest beides: `save` vor dem Pull, `restore`
danach. Diese Tests spielen echte git-Repos durch, keine Attrappen.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATE_SH = _REPO_ROOT / "scripts" / "migrate-agents-yml.sh"

AGENTS_REL = "docker/docker-compose.agents.yml"
BACKUP_REL = "docker/docker-compose.agents.yml.pre-oss-split"

# So sah die Datei vor dem Split aus — inklusive einer Hand-Anpassung, die der
# Renderer nie wieder erzeugen wuerde.
FLEET_BEFORE = """\
services:
  mc-agent-meiner:
    image: mc-claude-agent:latest
    container_name: mc-agent-meiner
    volumes:
      - ${HOME}/Projekte:/workspace-ref:ro
"""
# Und so nach ein paar Wochen Laufzeit (der Renderer hat fortgeschrieben).
FLEET_NOW = FLEET_BEFORE + """
  mc-agent-zweiter:
    image: mc-agent-base:latest
    container_name: mc-agent-zweiter
"""

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        env=_GIT_ENV, timeout=60,
    )


def _run_migration(cwd: Path, mode: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(MIGRATE_SH), mode], cwd=cwd, capture_output=True, text=True,
        env=_GIT_ENV, timeout=60,
    )


@pytest.fixture
def installation(tmp_path: Path) -> tuple[Path, Path]:
    """Ein 'upstream'-Repo im Vorher-Zustand plus eine geklonte Installation."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-b", "main")
    (upstream / "docker").mkdir()
    (upstream / AGENTS_REL).write_text(FLEET_BEFORE, encoding="utf-8")
    (upstream / ".gitignore").write_text("# nichts\n", encoding="utf-8")
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-m", "vorher: Flotte im Repo")

    install = tmp_path / "install"
    assert _git(tmp_path, "clone", str(upstream), str(install)).returncode == 0

    # Der Split passiert upstream: Datei raus, Vorlage rein, .gitignore ergaenzt.
    _git(upstream, "rm", "--quiet", AGENTS_REL)
    (upstream / "docker").mkdir(exist_ok=True)  # git raeumt das leere Verzeichnis weg
    (upstream / "docker" / "docker-compose.agents.example.yml").write_text(
        "services: {}\n", encoding="utf-8"
    )
    (upstream / ".gitignore").write_text(f"{AGENTS_REL}\n", encoding="utf-8")
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-m", "oss: eigene Flotte raus")

    return upstream, install


def test_pull_without_migration_loses_the_fleet(installation):
    """Wache: ohne Migration geht es kaputt. Faellt dieser Test aus, ist die
    Migration ueberfluessig geworden — dann darf sie weg."""
    _upstream, install = installation

    # Fall (b): die Datei ist fortgeschrieben worden.
    (install / AGENTS_REL).write_text(FLEET_NOW, encoding="utf-8")
    pull = _git(install, "pull", "--ff-only")
    assert pull.returncode != 0, "git pull haette abbrechen muessen"

    # Fall (a): unveraendert → git loescht sie kommentarlos.
    _git(install, "checkout", "--", AGENTS_REL)
    assert _git(install, "pull", "--ff-only").returncode == 0
    assert not (install / AGENTS_REL).exists(), "git hat die Datei doch behalten?"


def test_modified_fleet_survives_the_update(installation):
    """Der Normalfall (b): eigene, fortgeschriebene Datei — Pull laeuft durch,
    Inhalt bleibt Zeile fuer Zeile erhalten."""
    _upstream, install = installation
    (install / AGENTS_REL).write_text(FLEET_NOW, encoding="utf-8")

    save = _run_migration(install, "save")
    assert save.returncode == 0, save.stderr

    pull = _git(install, "pull", "--ff-only")
    assert pull.returncode == 0, (
        f"Pull scheitert trotz Migration:\n{pull.stdout}\n{pull.stderr}"
    )

    restore = _run_migration(install, "restore")
    assert restore.returncode == 0, restore.stderr

    assert (install / AGENTS_REL).read_text(encoding="utf-8") == FLEET_NOW
    # Die Hand-Anpassung, die der Renderer nie wieder erzeugt, ist noch da.
    assert "/workspace-ref:ro" in (install / AGENTS_REL).read_text(encoding="utf-8")
    # Und die Datei ist jetzt nicht mehr getrackt.
    tracked = _git(install, "ls-files", "--error-unmatch", AGENTS_REL)
    assert tracked.returncode != 0, "die Datei ist immer noch in der Versionsverwaltung"


def test_untouched_fleet_is_not_silently_deleted(installation):
    """Fall (a): lokale Datei entspricht dem eingecheckten Stand. Ohne
    Migration loescht git sie stillschweigend — mit Migration bleibt sie."""
    _upstream, install = installation

    assert _run_migration(install, "save").returncode == 0
    assert _git(install, "pull", "--ff-only").returncode == 0
    assert _run_migration(install, "restore").returncode == 0

    assert (install / AGENTS_REL).exists(), "die Flotte wurde stumm geloescht"
    assert (install / AGENTS_REL).read_text(encoding="utf-8") == FLEET_BEFORE


def test_second_update_is_a_no_op(installation):
    """Idempotenz: nach der Migration ist die Datei untracked. Ein weiteres
    Update darf sie nicht anfassen."""
    _upstream, install = installation
    (install / AGENTS_REL).write_text(FLEET_NOW, encoding="utf-8")
    _run_migration(install, "save")
    _git(install, "pull", "--ff-only")
    _run_migration(install, "restore")

    # Zweiter Durchlauf, nichts mehr zu migrieren.
    assert _run_migration(install, "save").returncode == 0
    assert _git(install, "pull", "--ff-only").returncode == 0
    assert _run_migration(install, "restore").returncode == 0
    assert (install / AGENTS_REL).read_text(encoding="utf-8") == FLEET_NOW


def test_save_leaves_the_fleet_intact_when_the_pull_never_happens(installation):
    """Abbruch mittendrin: `save` lief, der Pull fiel aus. `restore` muss den
    Vorher-Zustand wiederherstellen statt die Arbeit des Betreibers im Backup
    verschwinden zu lassen."""
    _upstream, install = installation
    (install / AGENTS_REL).write_text(FLEET_NOW, encoding="utf-8")

    assert _run_migration(install, "save").returncode == 0
    # KEIN Pull.
    assert _run_migration(install, "restore").returncode == 0

    assert (install / AGENTS_REL).read_text(encoding="utf-8") == FLEET_NOW


def test_backup_pattern_is_gitignored():
    """Das Backup darf nicht als untracked Datei im Repo herumliegen und beim
    naechsten `git add -A` mitcommittet werden."""
    check = subprocess.run(
        ["git", "check-ignore", "-q", BACKUP_REL],
        cwd=_REPO_ROOT, capture_output=True, text=True, timeout=30,
    )
    assert check.returncode == 0, f"{BACKUP_REL} steht nicht in der .gitignore"


def test_updating_doc_names_the_migration():
    """`docs/setup/updating.md` dokumentiert weiterhin schlichtes `git pull` —
    dort muss der Schritt stehen, sonst laeuft jeder Handbetrieb ins Messer."""
    doc = (_REPO_ROOT / "docs" / "setup" / "updating.md").read_text(encoding="utf-8")
    assert "migrate-agents-yml.sh" in doc


def test_the_documented_four_commands_carry_the_fleet_across(installation, tmp_path):
    """Der Weg, den die meisten gehen werden: die vier Befehle aus
    `docs/setup/updating.md` — ohne Skript, denn das kommt erst MIT diesem
    Pull und kann den Pull, der es bringt, nicht retten."""
    _upstream, install = installation
    (install / AGENTS_REL).write_text(FLEET_NOW, encoding="utf-8")
    aussen = tmp_path / "my-agent-fleet.yml"

    # 1. Kopie beiseite
    aussen.write_text((install / AGENTS_REL).read_text(encoding="utf-8"), encoding="utf-8")
    # 2. eingecheckten Stand herstellen, damit der Pull nichts findet
    assert _git(install, "checkout", "--", AGENTS_REL).returncode == 0
    # 3. Pull
    pull = _git(install, "pull", "--ff-only")
    assert pull.returncode == 0, f"{pull.stdout}\n{pull.stderr}"
    # 4. eigene Fassung zurueck
    (install / AGENTS_REL).write_text(aussen.read_text(encoding="utf-8"), encoding="utf-8")

    assert (install / AGENTS_REL).read_text(encoding="utf-8") == FLEET_NOW
    assert "/workspace-ref:ro" in (install / AGENTS_REL).read_text(encoding="utf-8")
    assert _git(install, "ls-files", "--error-unmatch", AGENTS_REL).returncode != 0


def test_updating_doc_spells_out_the_manual_commands():
    """Steht der Handweg nicht da, steht der Nutzer vor einem abgebrochenen
    Pull und einem Skript, das er noch gar nicht hat."""
    doc = (_REPO_ROOT / "docs" / "setup" / "updating.md").read_text(encoding="utf-8")
    assert f"git checkout -- {AGENTS_REL}" in doc
    assert "git pull --ff-only" in doc
