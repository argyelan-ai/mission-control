"""Tests for visual-proof evidence validation (Phase 5B + 5B.1 security hardening).

Test matrix:
- Path extraction: MEDIA, shared-artifacts, no path, multiple
- Root boundary: allowed roots OK, arbitrary paths FAIL
- Path traversal: ../ tricks FAIL
- Symlink escape: symlinks outside root FAIL
- File extension: allowed OK, wrong FAIL
- File size: >= 5KB OK, < 5KB FAIL
- expected_content: keywords checked
- Regression: non-visual_proof unaffected
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
    """MEDIA: path is extracted correctly."""
    comments = [_comment("Evidence: MEDIA:~/.openclaw/media/browser/abc.png")]
    paths = extract_evidence_paths(comments)
    assert len(paths) == 1
    assert paths[0].endswith("/.openclaw/media/browser/abc.png")


def test_extract_shared_artifacts_path():
    """shared-artifacts path is extracted."""
    root = ALLOWED_EVIDENCE_ROOTS[1]  # shared-artifacts root
    comments = [_comment(f"Evidence: {root}/task-123/screenshot.png")]
    paths = extract_evidence_paths(comments)
    assert len(paths) == 1
    assert "shared-artifacts" in paths[0]


def test_extract_no_path():
    """Comment without path → empty list."""
    comments = [_comment("**Update** — Alles fertig")]
    assert extract_evidence_paths(comments) == []


def test_extract_multiple_paths():
    """Multiple paths in different comments."""
    comments = [
        _comment("MEDIA:~/.openclaw/media/browser/a.png"),
        _comment(f"Auch: {ALLOWED_EVIDENCE_ROOTS[0]}/b.jpg"),
    ]
    paths = extract_evidence_paths(comments)
    assert len(paths) == 2


# ── Root Boundary ─────────────────────────────────────

def test_valid_file_in_media_root():
    """File in allowed MEDIA root → accepted."""
    root = ALLOWED_EVIDENCE_ROOTS[0]
    path = _create_valid_file(root, "test-valid.png")
    try:
        valid, reason = validate_evidence_file(path)
        assert valid is True
    finally:
        os.unlink(path)


def test_valid_file_in_shared_artifacts():
    """File in allowed shared-artifacts root → accepted."""
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
    """Arbitrary absolute path outside allowed roots → rejected."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"x" * 20_000)
        f.flush()
        valid, reason = validate_evidence_file(f.name)
    os.unlink(f.name)
    assert valid is False
    assert "ausserhalb" in reason.lower()


def test_home_directory_file_rejected():
    """File in home directory (but not in allowed root) → rejected."""
    path = _create_valid_file(os.path.expanduser("~/Desktop"), "fake-evidence.png")
    try:
        valid, reason = validate_evidence_file(path)
        assert valid is False
        assert "ausserhalb" in reason.lower()
    finally:
        os.unlink(path)


# ── Path Traversal ──────────────────────────────────────

def test_path_traversal_rejected():
    """../-traversal out of an allowed root → rejected."""
    # Path looks like it's within the root, but isn't after realpath resolution
    traversal_path = os.path.join(ALLOWED_EVIDENCE_ROOTS[0], "..", "..", "etc", "passwd.png")
    valid, reason = validate_evidence_file(traversal_path)
    assert valid is False
    # Either "ausserhalb" (root check) or "existiert nicht"
    assert "ausserhalb" in reason.lower() or "existiert nicht" in reason.lower()


# ── Symlink Escape ──────────────────────────────────────

def test_symlink_escape_rejected():
    """Symlink in an allowed root pointing to a file outside → rejected."""
    root = ALLOWED_EVIDENCE_ROOTS[0]
    os.makedirs(root, exist_ok=True)

    # Create file OUTSIDE the root
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False, dir="/tmp") as f:
        f.write(b"x" * 20_000)
        outside_path = f.name

    # Create symlink INSIDE the root
    link_path = os.path.join(root, "symlink-escape-test.png")
    try:
        os.symlink(outside_path, link_path)
        valid, reason = validate_evidence_file(link_path)
        assert valid is False
        assert "ausserhalb" in reason.lower()
    finally:
        os.unlink(link_path)
        os.unlink(outside_path)


# ── File Extension ─────────────────────────────────────────

def test_allowed_extensions():
    """All allowed extensions are accepted."""
    root = ALLOWED_EVIDENCE_ROOTS[0]
    for ext in ALLOWED_EXTENSIONS:
        path = _create_valid_file(root, f"test{ext}")
        try:
            valid, reason = validate_evidence_file(path)
            assert valid is True, f"Extension {ext} should be allowed"
        finally:
            os.unlink(path)


def test_disallowed_extension_txt():
    """.txt file → rejected."""
    root = ALLOWED_EVIDENCE_ROOTS[0]
    path = _create_valid_file(root, "fake.txt")
    try:
        valid, reason = validate_evidence_file(path)
        assert valid is False
        assert "dateiendung" in reason.lower()
    finally:
        os.unlink(path)


def test_disallowed_extension_pdf():
    """.pdf file → rejected."""
    root = ALLOWED_EVIDENCE_ROOTS[0]
    path = _create_valid_file(root, "report.pdf")
    try:
        valid, reason = validate_evidence_file(path)
        assert valid is False
        assert "dateiendung" in reason.lower()
    finally:
        os.unlink(path)


def test_disallowed_extension_html():
    """.html file → rejected."""
    root = ALLOWED_EVIDENCE_ROOTS[0]
    path = _create_valid_file(root, "page.html")
    try:
        valid, reason = validate_evidence_file(path)
        assert valid is False
        assert "dateiendung" in reason.lower()
    finally:
        os.unlink(path)


# ── File Size ────────────────────────────────────────

def test_file_too_small():
    """File < 5KB → rejected."""
    root = ALLOWED_EVIDENCE_ROOTS[0]
    path = _create_valid_file(root, "tiny.png", size=100)
    try:
        valid, reason = validate_evidence_file(path)
        assert valid is False
        assert "zu klein" in reason.lower()
    finally:
        os.unlink(path)


def test_file_not_exists():
    """Non-existent file → rejected."""
    path = os.path.join(ALLOWED_EVIDENCE_ROOTS[0], "nonexistent-12345.png")
    valid, reason = validate_evidence_file(path)
    assert valid is False
    assert "existiert nicht" in reason.lower()


# ── Full Validation Pipeline ────────────────────────────

def test_full_validation_success():
    """Full validation with a valid MEDIA file."""
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
    """No MEDIA reference → invalid."""
    comments = [_comment("Alles fertig, sieht gut aus")]
    valid, issues = validate_visual_proof_evidence(comments)
    assert valid is False
    assert any("MEDIA" in i for i in issues)


def test_full_validation_arbitrary_path_fails():
    """Referenced path outside allowed roots → invalid."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"x" * 20_000)
        f.flush()
        comments = [_comment(f"MEDIA:{f.name}")]
        valid, issues = validate_visual_proof_evidence(comments)
    os.unlink(f.name)
    assert valid is False
    assert any("ausserhalb" in i for i in issues)


def test_full_validation_with_expected_content():
    """expected_content keywords present in evidence → OK."""
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
    """expected_content keywords missing → warning but valid (file ok)."""
    root = ALLOWED_EVIDENCE_ROOTS[0]
    path = _create_valid_file(root, "no-content.png")
    try:
        comments = [_comment(f"MEDIA:{path}")]
        valid, issues = validate_visual_proof_evidence(
            comments, expected_content="Spezifischer Dashboard Inhalt mit Sidebar"
        )
        assert valid is True  # file is ok
        assert any("expected_content" in i for i in issues)  # Warning
    finally:
        os.unlink(path)


# ── Regression ──────────────────────────────────────────

def test_non_visual_proof_unaffected():
    """Validation is only invoked for visual_proof — other types unaffected.
    This test documents the boundary: the function always validates strictly,
    the CALL in the guard only happens for delegation_type=='visual_proof'.
    """
    comments = [_comment("Code geschrieben, Tests laufen")]
    valid, issues = validate_visual_proof_evidence(comments)
    assert valid is False  # Would fail — but is never invoked for code_change
