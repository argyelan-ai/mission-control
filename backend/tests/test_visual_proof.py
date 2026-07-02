"""Tests fuer Visual-Proof Evidence Validation (Phase 5B + 5B.1 Security Hardening).

Testmatrix:
- Pfad-Extraktion: MEDIA, shared-artifacts, kein Pfad, mehrere
- Root-Begrenzung: erlaubte Roots OK, beliebige Pfade FAIL
- Path Traversal: ../-Tricks FAIL
- Symlink Escape: Symlinks ausserhalb Root FAIL
- Dateiendung: erlaubte OK, falsche FAIL
- Dateigroesse: >= 5KB OK, < 5KB FAIL
- expected_content: Keywords geprueft
- Regression: non-visual_proof unberuehrt
"""
import os
import tempfile
from unittest.mock import MagicMock

import pytest

from app.services.visual_proof import (
    ALLOWED_EVIDENCE_ROOTS,
    ALLOWED_EXTENSIONS,
    MIN_SCREENSHOT_BYTES,
    extract_evidence_paths,
    validate_evidence_file,
    validate_visual_proof_evidence,
)


def _comment(content: str) -> MagicMock:
    c = MagicMock()
    c.content = content
    return c


def _create_valid_file(directory: str, name: str = "test.png", size: int = 20_000) -> str:
    """Create a valid evidence file in the given directory."""
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    with open(path, "wb") as f:
        f.write(b"x" * size)
    return path


# ── Path Extraction ─────────────────────────────────────

def test_extract_media_path():
    """MEDIA:-Pfad wird korrekt extrahiert."""
    comments = [_comment("Evidence: MEDIA:~/.openclaw/media/browser/abc.png")]
    paths = extract_evidence_paths(comments)
    assert len(paths) == 1
    assert paths[0].endswith("/.openclaw/media/browser/abc.png")


def test_extract_shared_artifacts_path():
    """shared-artifacts Pfad wird extrahiert."""
    root = ALLOWED_EVIDENCE_ROOTS[1]  # shared-artifacts root
    comments = [_comment(f"Evidence: {root}/task-123/screenshot.png")]
    paths = extract_evidence_paths(comments)
    assert len(paths) == 1
    assert "shared-artifacts" in paths[0]


def test_extract_no_path():
    """Kommentar ohne Pfad → leere Liste."""
    comments = [_comment("**Update** — Alles fertig")]
    assert extract_evidence_paths(comments) == []


def test_extract_multiple_paths():
    """Mehrere Pfade in verschiedenen Kommentaren."""
    comments = [
        _comment("MEDIA:~/.openclaw/media/browser/a.png"),
        _comment(f"Auch: {ALLOWED_EVIDENCE_ROOTS[0]}/b.jpg"),
    ]
    paths = extract_evidence_paths(comments)
    assert len(paths) == 2


# ── Root Begrenzung ─────────────────────────────────────

def test_valid_file_in_media_root():
    """Datei im erlaubten MEDIA-Root → akzeptiert."""
    root = ALLOWED_EVIDENCE_ROOTS[0]
    path = _create_valid_file(root, "test-valid.png")
    try:
        valid, reason = validate_evidence_file(path)
        assert valid is True
    finally:
        os.unlink(path)


def test_valid_file_in_shared_artifacts():
    """Datei im erlaubten shared-artifacts Root → akzeptiert."""
    root = ALLOWED_EVIDENCE_ROOTS[1]
    subdir = os.path.join(root, "test-task-123")
    path = _create_valid_file(subdir, "screenshot.png")
    try:
        valid, reason = validate_evidence_file(path)
        assert valid is True
    finally:
        os.unlink(path)
        os.rmdir(subdir)


def test_arbitrary_absolute_path_rejected():
    """Beliebiger absoluter Pfad ausserhalb erlaubter Roots → abgelehnt."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"x" * 20_000)
        f.flush()
        valid, reason = validate_evidence_file(f.name)
    os.unlink(f.name)
    assert valid is False
    assert "ausserhalb" in reason.lower()


def test_home_directory_file_rejected():
    """Datei in Home-Verzeichnis (aber nicht in erlaubtem Root) → abgelehnt."""
    path = _create_valid_file(os.path.expanduser("~/Desktop"), "fake-evidence.png")
    try:
        valid, reason = validate_evidence_file(path)
        assert valid is False
        assert "ausserhalb" in reason.lower()
    finally:
        os.unlink(path)


# ── Path Traversal ──────────────────────────────────────

def test_path_traversal_rejected():
    """../-Traversal aus erlaubtem Root heraus → abgelehnt."""
    # Pfad sieht aus als waere er im Root, ist es aber nach realpath nicht
    traversal_path = os.path.join(ALLOWED_EVIDENCE_ROOTS[0], "..", "..", "etc", "passwd.png")
    valid, reason = validate_evidence_file(traversal_path)
    assert valid is False
    # Entweder "ausserhalb" (Root-Check) oder "existiert nicht"
    assert "ausserhalb" in reason.lower() or "existiert nicht" in reason.lower()


# ── Symlink Escape ──────────────────────────────────────

def test_symlink_escape_rejected():
    """Symlink im erlaubten Root der auf Datei ausserhalb zeigt → abgelehnt."""
    root = ALLOWED_EVIDENCE_ROOTS[0]
    os.makedirs(root, exist_ok=True)

    # Erstelle Datei AUSSERHALB des Roots
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False, dir="/tmp") as f:
        f.write(b"x" * 20_000)
        outside_path = f.name

    # Erstelle Symlink INNERHALB des Roots
    link_path = os.path.join(root, "symlink-escape-test.png")
    try:
        os.symlink(outside_path, link_path)
        valid, reason = validate_evidence_file(link_path)
        assert valid is False
        assert "ausserhalb" in reason.lower()
    finally:
        os.unlink(link_path)
        os.unlink(outside_path)


# ── Dateiendung ─────────────────────────────────────────

def test_allowed_extensions():
    """Alle erlaubten Endungen werden akzeptiert."""
    root = ALLOWED_EVIDENCE_ROOTS[0]
    for ext in ALLOWED_EXTENSIONS:
        path = _create_valid_file(root, f"test{ext}")
        try:
            valid, reason = validate_evidence_file(path)
            assert valid is True, f"Extension {ext} should be allowed"
        finally:
            os.unlink(path)


def test_disallowed_extension_txt():
    """.txt Datei → abgelehnt."""
    root = ALLOWED_EVIDENCE_ROOTS[0]
    path = _create_valid_file(root, "fake.txt")
    try:
        valid, reason = validate_evidence_file(path)
        assert valid is False
        assert "dateiendung" in reason.lower()
    finally:
        os.unlink(path)


def test_disallowed_extension_pdf():
    """.pdf Datei → abgelehnt."""
    root = ALLOWED_EVIDENCE_ROOTS[0]
    path = _create_valid_file(root, "report.pdf")
    try:
        valid, reason = validate_evidence_file(path)
        assert valid is False
        assert "dateiendung" in reason.lower()
    finally:
        os.unlink(path)


def test_disallowed_extension_html():
    """.html Datei → abgelehnt."""
    root = ALLOWED_EVIDENCE_ROOTS[0]
    path = _create_valid_file(root, "page.html")
    try:
        valid, reason = validate_evidence_file(path)
        assert valid is False
        assert "dateiendung" in reason.lower()
    finally:
        os.unlink(path)


# ── Dateigroesse ────────────────────────────────────────

def test_file_too_small():
    """Datei < 5KB → abgelehnt."""
    root = ALLOWED_EVIDENCE_ROOTS[0]
    path = _create_valid_file(root, "tiny.png", size=100)
    try:
        valid, reason = validate_evidence_file(path)
        assert valid is False
        assert "zu klein" in reason.lower()
    finally:
        os.unlink(path)


def test_file_not_exists():
    """Nicht existierende Datei → abgelehnt."""
    path = os.path.join(ALLOWED_EVIDENCE_ROOTS[0], "nonexistent-12345.png")
    valid, reason = validate_evidence_file(path)
    assert valid is False
    assert "existiert nicht" in reason.lower()


# ── Full Validation Pipeline ────────────────────────────

def test_full_validation_success():
    """Vollstaendige Validierung mit gueltiger MEDIA-Datei."""
    root = ALLOWED_EVIDENCE_ROOTS[0]
    path = _create_valid_file(root, "full-valid.png")
    try:
        comments = [_comment(f"MEDIA:{path}")]
        valid, issues = validate_visual_proof_evidence(comments)
        assert valid is True
        assert len(issues) == 0
    finally:
        os.unlink(path)


def test_full_validation_no_path():
    """Keine MEDIA-Referenz → ungueltig."""
    comments = [_comment("Alles fertig, sieht gut aus")]
    valid, issues = validate_visual_proof_evidence(comments)
    assert valid is False
    assert any("MEDIA" in i for i in issues)


def test_full_validation_arbitrary_path_fails():
    """Referenzierter Pfad ausserhalb erlaubter Roots → ungueltig."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"x" * 20_000)
        f.flush()
        comments = [_comment(f"MEDIA:{f.name}")]
        valid, issues = validate_visual_proof_evidence(comments)
    os.unlink(f.name)
    assert valid is False
    assert any("ausserhalb" in i for i in issues)


def test_full_validation_with_expected_content():
    """expected_content Keywords in Evidence → OK."""
    root = ALLOWED_EVIDENCE_ROOTS[0]
    path = _create_valid_file(root, "content-check.png")
    try:
        comments = [_comment(f"MEDIA:{path} — Login-Seite mit Mission Control Header sichtbar")]
        valid, issues = validate_visual_proof_evidence(
            comments, expected_content="Mission Control Login Header"
        )
        assert valid is True
    finally:
        os.unlink(path)


def test_full_validation_expected_content_missing():
    """expected_content Keywords fehlen → Warning aber valid (Datei ok)."""
    root = ALLOWED_EVIDENCE_ROOTS[0]
    path = _create_valid_file(root, "no-content.png")
    try:
        comments = [_comment(f"MEDIA:{path}")]
        valid, issues = validate_visual_proof_evidence(
            comments, expected_content="Spezifischer Dashboard Inhalt mit Sidebar"
        )
        assert valid is True  # Datei ist ok
        assert any("expected_content" in i for i in issues)  # Warning
    finally:
        os.unlink(path)


# ── Regression ──────────────────────────────────────────

def test_non_visual_proof_unaffected():
    """Validation wird nur fuer visual_proof aufgerufen — andere Typen nicht betroffen.
    Dieser Test dokumentiert die Grenze: die Funktion validiert immer streng,
    der AUFRUF im Guard passiert nur bei delegation_type=='visual_proof'.
    """
    comments = [_comment("Code geschrieben, Tests laufen")]
    valid, issues = validate_visual_proof_evidence(comments)
    assert valid is False  # Wuerde fehlschlagen — wird aber nie fuer code_change aufgerufen
