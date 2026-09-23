"""Grundgesamtheit der Entscheidungsdokumente — die Messung, auf der das
ADR-Gate steht (Vorfall PR #602, 2026-09-16).

Die Regel lautet "ein PR, der ein Entscheidungsdokument aendert, braucht eine
Freigabe des Textes". Damit sie nicht an einem Dateinamen klebt (Fall 1 der
Karte: ein ADR kann woanders liegen), ist die *Signatur im Text* massgeblich:
`# ADR-NNN` in der ersten nicht-leeren Zeile. Der Pfad `docs/decisions/NNN-*.md`
ist nur ein Vorfilter.

Dieser Test pinnt die gemessene Grundgesamtheit fest UND die Faelle, in denen
Pfad und Signatur auseinanderlaufen — in beide Richtungen:

  - Pfad passt, Signatur fehlt        → KEIN Entscheidungsdokument
  - Signatur passt, Pfad ist exotisch  → Entscheidungsdokument
  - Signatur widerspricht dem Dateinamen → KEIN Entscheidungsdokument
  - geloeschte Datei (content=None)    → Entscheidungsdokument (Loeschen ist
    entscheidungsrelevant, sonst waere das Loeschen eines ADR der Bypass)
"""

import subprocess
from pathlib import Path

import pytest

from app.services.decision_docs import (
    DECISION_DOCS_DIR,
    adr_signature_number,
    decision_doc_number,
    is_decision_doc,
    scan_decision_docs,
)

# ── Gemessene Grundgesamtheit dieses Repos ───────────────────────────────

# Gemessen am 2026-09-23 auf docs/adr-085-head-per-job (origin/main 2b16977e
# + ADR-085): 86 `.md`-Dateien unter docs/decisions/, davon 84 echte
# Entscheidungsdokumente; die zwei uebrigen sind README.md und _template.md.
EXPECTED_DECISION_DOCS = 84


def _repo_root() -> Path:
    root = Path(__file__).resolve().parents[2]
    assert (root / DECISION_DOCS_DIR).is_dir(), f"kein {DECISION_DOCS_DIR} in {root}"
    return root


@pytest.mark.asyncio
async def test_repo_has_exactly_the_measured_decision_docs():
    """Die Grundgesamtheit, auf die sich Blast-Radius und Freigabe-Pflicht
    beziehen. Aendert sich die Zahl, muss jemand die Regel erneut durchdenken."""
    docs = scan_decision_docs(_repo_root())
    assert len(docs) == EXPECTED_DECISION_DOCS, (
        f"{len(docs)} statt {EXPECTED_DECISION_DOCS} Entscheidungsdokumente — "
        "neue ADRs bitte bewusst bestaetigen"
    )
    assert all(p.startswith(f"{DECISION_DOCS_DIR}/") for p in docs)
    # Die Nummern sind der Bezugspunkt jeder Freigabe → eindeutig und dreistellig.
    numbers = list(docs.values())
    assert len(numbers) == len(set(numbers)), "doppelte ADR-Nummern"
    assert all(len(n) == 3 and n.isdigit() for n in numbers)


@pytest.mark.asyncio
async def test_readme_and_template_are_not_decision_docs():
    """Die beiden Nicht-Dokumente im Ordner. Ohne diese Zusicherung wuerde
    jede README-Aenderung eine ADR-Freigabe verlangen."""
    root = _repo_root()
    for name in ("README.md", "_template.md"):
        candidate = root / DECISION_DOCS_DIR / name
        if not candidate.is_file():
            pytest.skip(f"{name} existiert nicht mehr")
        content = candidate.read_text(encoding="utf-8")
        assert not is_decision_doc(content, f"{DECISION_DOCS_DIR}/{name}"), name
    # Gegenprobe: die Ordner-Regel allein wuerde beide faelschlich einschliessen —
    # genau darum entscheidet die Signatur.
    assert decision_doc_number(f"{DECISION_DOCS_DIR}/README.md") is None


@pytest.mark.asyncio
async def test_no_signature_bearing_doc_lives_outside_the_standard_dir():
    """Fall 1, gemessen statt angenommen: gibt es ein `# ADR-NNN` ausserhalb
    von docs/decisions/? Wenn ja, muss das Gate es trotzdem erfassen — sonst
    waere die Regel durch Verschieben der Datei umgehbar.

    Der Test haelt beide Haelften fest: heute existiert keine solche Datei,
    UND `is_decision_doc` wuerde sie erkennen.
    """
    root = _repo_root()
    outside = []
    for path in root.rglob("*.md"):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(f"{DECISION_DOCS_DIR}/") or "/.git/" in f"/{rel}":
            continue
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:4096]
        except OSError:
            continue
        if adr_signature_number(head) is not None:
            outside.append(rel)

    assert outside == [], (
        f"Entscheidungsdokument(e) ausserhalb {DECISION_DOCS_DIR}/: {outside} — "
        "das Gate erfasst sie (Signatur), aber der Ordner sollte die Wahrheit sein"
    )
    # Und die Erkennung greift dort tatsaechlich:
    assert is_decision_doc("# ADR-084 — irgendwas\n", "docs/foo/bar.md") is True


@pytest.mark.asyncio
async def test_every_named_decision_doc_carries_a_matching_signature():
    """Symmetrie Pfad ↔ Signatur ueber alle 83: andernfalls gaebe es Dokumente,
    die der Pfad-Vorfilter sieht, das Verdikt aber verwirft."""
    root = _repo_root()
    mismatches = []
    for rel, number in scan_decision_docs(root).items():
        content = (root / rel).read_text(encoding="utf-8", errors="replace")
        signature = adr_signature_number(content)
        if signature != number:
            mismatches.append((rel, signature))
    assert mismatches == [], f"Pfad und Signatur laufen auseinander: {mismatches}"


# ── Mutation-Guards: injizierte Faelle muessen erkannt werden ────────────


@pytest.mark.asyncio
async def test_mutation_guard_injected_deviant_cases(tmp_path):
    """Ein neu eingeschleuster schlechter Fall muss durchfallen.

    Vier Injektionen in einem sonst leeren Baum — faellt eine der Regeln weg,
    wird der Scanner zahnlos und der Test faengt es:
      1. korrekte Datei                     → erkannt
      2. Dateiname passt, Signatur fehlt     → verworfen
      3. Signatur passt, Pfad ist exotisch    → erkannt
      4. Signatur widerspricht dem Namen      → verworfen
    """
    docs = tmp_path / DECISION_DOCS_DIR
    docs.mkdir(parents=True)
    (docs / "001-gut.md").write_text("# ADR-001 — gut\n\nText.\n", encoding="utf-8")
    (docs / "002-ohne-signatur.md").write_text(
        "# Irgendein Entwurf\n\nKeine Signatur.\n", encoding="utf-8"
    )
    (docs / "003-falsche-nummer.md").write_text(
        "# ADR-099 — umbenannt\n\nSignatur passt nicht zum Namen.\n", encoding="utf-8"
    )
    exotic = tmp_path / "notes" / "decision.md"
    exotic.parent.mkdir()
    exotic.write_text("# ADR-004 — exotisch\n\nText.\n", encoding="utf-8")

    found = scan_decision_docs(tmp_path)

    assert set(found) == {
        f"{DECISION_DOCS_DIR}/001-gut.md",
        "notes/decision.md",
    }, found
    assert found[f"{DECISION_DOCS_DIR}/001-gut.md"] == "001"
    assert found["notes/decision.md"] == "004"


@pytest.mark.asyncio
async def test_mutation_guard_deletion_still_counts():
    """Der Bypass, den die Signatur-Regel nicht abdeckt: ein ADR loeschen.
    Unlesbarer Inhalt (None) muss als Entscheidungsdokument zaehlen."""
    assert is_decision_doc(None, f"{DECISION_DOCS_DIR}/084-foo.md") is True
    # Nicht-Entscheidungsdokumente bleiben auch geloescht draussen:
    assert is_decision_doc(None, f"{DECISION_DOCS_DIR}/README.md") is False
    assert is_decision_doc(None, "backend/app/main.py") is False
