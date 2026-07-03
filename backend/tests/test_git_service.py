"""Tests for GitService — git operations for agent projects."""
import pytest

from app.services.git_service import GitService, slugify_project, slugify_workspace_slug


def test_slugify_project():
    assert slugify_project("Agar.io Copy") == "agar-io-copy"
    assert slugify_project("My Cool App!") == "my-cool-app"
    assert slugify_project("  Spaces  ") == "spaces"
    assert slugify_project("already-slugged") == "already-slugged"


@pytest.mark.asyncio
async def test_run_cmd_returns_stdout():
    gs = GitService()
    result = await gs._run_cmd("echo", "hello")
    assert result.strip() == "hello"


@pytest.mark.asyncio
async def test_run_cmd_raises_on_failure():
    gs = GitService()
    with pytest.raises(RuntimeError, match="Git command failed"):
        await gs._run_cmd("false")


def test_slugify_workspace_slug():
    # D-02: short title -> no hash, exact pass-through from slugify_project
    assert slugify_workspace_slug("Short title") == "short-title"
    assert len(slugify_workspace_slug("Short title")) <= 50

    # D-01: long ASCII title -> capped at 50 chars, ends with -<6hex>
    long_title = (
        "Argyelan ai Logo austauschen und auf der Webseite live testen "
        "und danach noch etwas mehr"
    )
    result = slugify_workspace_slug(long_title)
    assert len(result) <= 50
    assert result[-7] == "-"
    assert result[-6:].isalnum()  # 6-char hex suffix

    # D-01: long unicode title -> <=50 chars, no crash
    unicode_title = (
        "Στεφ Αλέξ αλφα βήτα 日本語 テスト 123 XYZ extra words to make it long"
    )
    unicode_result = slugify_workspace_slug(unicode_title)
    assert len(unicode_result) <= 50

    # D-03: two titles sharing the same 43-char prefix -> different hashes
    base = "a" * 80
    slug_a = slugify_workspace_slug(base + "X")
    slug_b = slugify_workspace_slug(base + "Y")
    assert slug_a != slug_b  # collision avoidance
    assert len(slug_a) <= 50
    assert len(slug_b) <= 50
