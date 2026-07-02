"""Tests for the Phase B.1 PDF text extractor + wrapper patcher."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import frontmatter as fm_lib
import pytest

from app.services.deliverable_extractor import (
    EXTRACTION_PLACEHOLDER,
    PDF_TEXT_CHAR_CAP,
    extract_pdf_text,
    patch_wrapper_with_extracted,
)


def _make_pdf(tmp_path: Path, pages_text: list[str]) -> Path:
    """Generate a tiny real PDF via reportlab so pdfplumber can read it back.
    Skipped if reportlab isn't importable (keeps the test suite from gaining
    a heavy mandatory dep just for one round-trip test)."""
    try:
        from reportlab.pdfgen import canvas  # type: ignore
        from reportlab.lib.pagesizes import letter  # type: ignore
    except ImportError:
        pytest.skip("reportlab not installed — skipping PDF round-trip test")

    pdf_path = tmp_path / "doc.pdf"
    c = canvas.Canvas(str(pdf_path), pagesize=letter)
    for text in pages_text:
        c.drawString(72, 720, text)
        c.showPage()
    c.save()
    return pdf_path


def test_extract_pdf_text_round_trip(tmp_path: Path):
    pdf = _make_pdf(tmp_path, ["Wetterbericht Staufen 5603", "Sonnig, 22 Grad"])
    text = extract_pdf_text(pdf)
    assert "Wetterbericht Staufen 5603" in text
    assert "Sonnig" in text


def test_extract_pdf_text_returns_empty_for_missing_file(tmp_path: Path):
    text = extract_pdf_text(tmp_path / "ghost.pdf")
    assert text == ""


def test_extract_pdf_text_caps_at_50k(tmp_path: Path):
    # Mock pdfplumber to return a giant single page so we don't have to
    # actually generate a 100k-char real PDF.
    long_text = "X" * (PDF_TEXT_CHAR_CAP * 2)

    class _FakePage:
        def extract_text(self):
            return long_text

    class _FakePDF:
        is_encrypted = False
        pages = [_FakePage()]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    stub = tmp_path / "any.pdf"
    stub.write_bytes(b"")
    with patch("app.services.deliverable_extractor.pdfplumber.open", return_value=_FakePDF()):
        text = extract_pdf_text(stub)
    assert len(text) == PDF_TEXT_CHAR_CAP


def test_extract_pdf_text_skips_encrypted(tmp_path: Path):
    class _FakePDF:
        is_encrypted = True
        pages: list = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    stub = tmp_path / "encrypted.pdf"
    stub.write_bytes(b"")
    with patch("app.services.deliverable_extractor.pdfplumber.open", return_value=_FakePDF()):
        text = extract_pdf_text(stub)
    assert text == ""


def test_patch_wrapper_replaces_placeholder(tmp_path: Path):
    wrapper = tmp_path / "wrapper.md"
    body = (
        "# Wetterbericht\n\n"
        "## Auto-extracted\n\n"
        f"{EXTRACTION_PLACEHOLDER}\n"
    )
    post = fm_lib.Post(
        body,
        id="deliverable-x",
        title="Wetter",
        agent="researcher",
        type="deliverable",
        date="2026-05-15T13:00:00+00:00",
    )
    wrapper.write_text(fm_lib.dumps(post))

    rewritten = patch_wrapper_with_extracted(wrapper, "Sonnig 22 Grad")
    assert rewritten is True

    loaded = fm_lib.load(str(wrapper))
    assert EXTRACTION_PLACEHOLDER not in loaded.content
    assert "Sonnig 22 Grad" in loaded.content
    # Frontmatter preserved
    assert loaded["title"] == "Wetter"


def test_patch_wrapper_is_idempotent(tmp_path: Path):
    """Re-patching an already-patched wrapper is a no-op (no placeholder left)."""
    wrapper = tmp_path / "wrapper.md"
    post = fm_lib.Post(
        "# Title\n\n## Auto-extracted\n\nAlready filled.",
        id="x", title="T", agent="x", type="deliverable",
        date="2026-05-15T13:00:00+00:00",
    )
    wrapper.write_text(fm_lib.dumps(post))

    rewritten = patch_wrapper_with_extracted(wrapper, "new text")
    assert rewritten is False  # no placeholder → no-op


def test_patch_wrapper_inserts_no_extractable_text_marker(tmp_path: Path):
    """Empty extraction (e.g. encrypted PDF) leaves a helpful sentinel so
    the wrapper isn't visually misleading."""
    wrapper = tmp_path / "wrapper.md"
    post = fm_lib.Post(
        f"# Title\n\n## Auto-extracted\n\n{EXTRACTION_PLACEHOLDER}\n",
        id="x", title="T", agent="x", type="deliverable",
        date="2026-05-15T13:00:00+00:00",
    )
    wrapper.write_text(fm_lib.dumps(post))

    patch_wrapper_with_extracted(wrapper, "")
    loaded = fm_lib.load(str(wrapper))
    assert "(no extractable text)" in loaded.content
