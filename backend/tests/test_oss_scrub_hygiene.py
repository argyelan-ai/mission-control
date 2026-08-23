"""Wachen fuer das Saeubern vor der Veroeffentlichung.

Zwei Lektionen, beide teuer bezahlt:

1. **Blinde Ersetzung zerstoert Woerter.** Ein `estrich`→`blueprint` ueber den
   ganzen Baum hat aus "Bindestrich" ein "Bindblueprint" gemacht —
   ausgerechnet in der Zeile, die die Bindestrich-Zerlegung von `_smart_query`
   erklaert. Kein Test hat das gemerkt, weil niemand nach kaputten Woertern
   sucht, nur nach dem entfernten Begriff.
2. **Der Leak-Scanner muss die Formen kennen, die hier wirklich leaken.**
   Nicht Passwoerter — Adressen und Pfade: der Tailscale-Bereich, `*.ts.net`
   und absolute `/Users/<name>`-Pfade. Die CI-Pruefung greppt sonst nur nach
   Dateinamen und laesst genau das durch, was schon durchgerutscht ist.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _tracked_text_files() -> list[Path]:
    """Alle versionierten Dateien, die sich als Text lesen lassen."""
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=_REPO_ROOT, capture_output=True, text=True, timeout=60,
    )
    files = []
    for rel in out.stdout.split("\0"):
        if not rel:
            continue
        p = _REPO_ROOT / rel
        if p.is_file() and p.stat().st_size < 2_000_000:
            files.append(p)
    return files


# ── 1. Kaputte Woerter aus einer blinden Ersetzung ──────────────────────────

# Was eine Massen-Ersetzung aus einem harmlosen Wort machen kann. Waechst,
# sobald wieder eine Ersetzung ueber den Baum laeuft.
BROKEN_WORDS = ("Bindblueprint", "bindblueprint")


def test_no_word_was_destroyed_by_a_bulk_replacement():
    # Diese Datei traegt die kaputten Woerter selbst — sie muss, um nach ihnen
    # suchen zu koennen. Genau die Falle aus Befund 5, nur eine Ebene hoeher:
    # ein Wach-Test, der sich selbst meldet, wird abgeschaltet statt beachtet.
    myself = Path(__file__).resolve()
    hits: list[str] = []
    for path in _tracked_text_files():
        if path.resolve() == myself:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for word in BROKEN_WORDS:
            if word in text:
                hits.append(f"{path.relative_to(_REPO_ROOT)}: {word}")
    assert not hits, (
        "Eine Massen-Ersetzung hat Woerter zerstoert:\n  " + "\n  ".join(hits)
    )


# ── 2. Der Leak-Scanner kennt die Formen, die hier leaken ───────────────────

_GITLEAKS = shutil.which("gitleaks")
_needs_gitleaks = pytest.mark.skipif(
    _GITLEAKS is None,
    reason="gitleaks not installed locally (CI runs it in a pinned container)",
)


def _scan(tmp_path: Path, filename: str, content: str) -> subprocess.CompletedProcess:
    """gitleaks mit DER Konfiguration des Repos ueber eine einzelne Datei."""
    (tmp_path / filename).write_text(content, encoding="utf-8")
    return subprocess.run(
        [
            _GITLEAKS, "dir", str(tmp_path),
            "--config", str(_REPO_ROOT / ".gitleaks.toml"),
            "--no-banner", "--exit-code", "1",
        ],
        capture_output=True, text=True, timeout=120,
    )


@_needs_gitleaks
@pytest.mark.parametrize(
    "name, leak",
    [
        ("tailscale-ip", 'SPARK_URL = "http://100.83.41.7:8000/v1"'),
        ("magic-dns", 'BASE = "https://laptop-3.taildeadbeef.ts.net"'),
        ("home-path", 'REPO = "/Users/marianne/Workspace/Projects/mission-control"'),
    ],
)
def test_scanner_catches_the_addresses_that_actually_leak(tmp_path, name, leak):
    """Sabotage-Probe als Test: genau die drei Formen, die aus diesem Repo
    schon herausgerutscht sind, muessen anschlagen."""
    result = _scan(tmp_path, f"{name}.py", leak + "\n")
    assert result.returncode != 0, (
        f"gitleaks laesst '{leak}' durch — die naechste private Adresse "
        f"rutscht genauso durch.\n{result.stdout}"
    )


@_needs_gitleaks
@pytest.mark.parametrize(
    "name, harmless",
    [
        # Platzhalter, die im Repo bereits stehen und stehen bleiben duerfen.
        ("doc-ip", 'SPARK_URL = "http://198.51.100.7:8000/v1"'),
        ("doc-host", 'PUBLIC_HOST=your-machine.tailnet-name.ts.net'),
        ("test-home", 'HOME = "/Users/testuser"'),
        ("placeholder-home", 'path = "/Users/YOUR_USER/.mc"'),
        # Bewusst aus dem 10er-Bereich: Die lokale Muster-Liste des Betreibers
        # wertet das gaengigste Heimnetz-Praefix pauschal als persoenlich (ein
        # Geraet von ihm stand dort einmal), und ein solcher Testwert blockiert
        # seinen eigenen Push. Die Aussage bleibt dieselbe: ein generischer
        # Adressbereich aus einem privaten Netz ist ein Platzhalter und darf
        # die Wache nicht ausloesen.
        ("private-range", 'LM_STUDIO = "http://10.0.0.50:1234"'),
    ],
)
def test_scanner_leaves_the_placeholders_alone(tmp_path, name, harmless):
    """Die Regeln duerfen die dokumentierten Platzhalter nicht anschreien —
    sonst schaltet sie beim ersten roten CI-Lauf jemand wieder ab."""
    result = _scan(tmp_path, f"{name}.py", harmless + "\n")
    assert result.returncode == 0, (
        f"gitleaks schlaegt auf den Platzhalter '{harmless}' an.\n{result.stdout}"
    )


@_needs_gitleaks
def test_the_repo_itself_is_clean_under_the_new_rules(tmp_path):
    """Die schaerferen Regeln duerfen den bestehenden Baum nicht rot faerben —
    sonst schaltet sie beim ersten roten CI-Lauf jemand wieder ab.

    Geprueft wird der VERSIONIERTE Stand, nicht das Arbeitsverzeichnis: die CI
    checkt aus und scannt, sieht also weder __pycache__ noch lokale Notizen.
    """
    export = tmp_path / "tree"
    export.mkdir()
    listing = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=_REPO_ROOT, capture_output=True, timeout=60,
    )
    tar = subprocess.run(
        ["tar", "-cf", "-", "--null", "-T", "-"],
        cwd=_REPO_ROOT, input=listing.stdout, capture_output=True, timeout=120,
    )
    subprocess.run(["tar", "-xf", "-", "-C", str(export)],
                   input=tar.stdout, capture_output=True, timeout=120)

    result = subprocess.run(
        [
            _GITLEAKS, "dir", str(export),
            "--config", str(_REPO_ROOT / ".gitleaks.toml"),
            "--no-banner", "--exit-code", "1",
        ],
        capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stdout
