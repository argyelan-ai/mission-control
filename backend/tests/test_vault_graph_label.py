"""Tests for vault_graph.resolve_label — three-step fallback chain (W2.3)."""

from app.services.vault_graph import resolve_label


def test_resolve_label_prefers_frontmatter_title():
    label = resolve_label(
        frontmatter={"title": "My Title"},
        content="# Heading One\nbody",
        filename="abc-123.md",
    )
    assert label == "My Title"


def test_resolve_label_falls_back_to_first_heading():
    label = resolve_label(
        frontmatter={},
        content="# Heading One\nbody",
        filename="abc-123.md",
    )
    assert label == "Heading One"


def test_resolve_label_falls_back_to_filename_stem():
    label = resolve_label(
        frontmatter={},
        content="no heading body",
        filename="abc-123.md",
    )
    assert label == "abc-123"


def test_resolve_label_handles_nested_path():
    label = resolve_label(
        frontmatter={},
        content="",
        filename="memory/global/abc-123.md",
    )
    assert label == "abc-123"


def test_resolve_label_strips_heading_whitespace():
    label = resolve_label(
        frontmatter={},
        content="##   Heading With Spaces   \n",
        filename="x.md",
    )
    assert label == "Heading With Spaces"


def test_resolve_label_ignores_empty_title():
    label = resolve_label(
        frontmatter={"title": "   "},
        content="# Real Heading\n",
        filename="x.md",
    )
    assert label == "Real Heading"
