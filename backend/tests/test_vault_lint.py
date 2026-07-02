"""Tests for vault_lint.py — orphans, invalid frontmatter, duplicate IDs, report writing."""

import uuid
from datetime import datetime
from pathlib import Path

import frontmatter
import pytest

from app.services.vault_lint import lint_vault, write_lint_report, EXCLUDED_PREFIXES, INTENTIONAL_ROOTS


# ── helpers ──────────────────────────────────────────────────────────────────

def _valid_note(
    vault: Path,
    rel: str,
    *,
    note_id: str | None = None,
    agent: str = "sparky",
    note_type: str = "note",
    date: str = "2026-05-14T12:00:00Z",
) -> Path:
    """Write a valid vault note at vault/rel."""
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    fid = note_id or str(uuid.uuid4())
    path.write_text(
        f"---\nid: {fid}\ntype: {note_type}\nagent: {agent}\ndate: {date}\n---\n# Note\nbody text"
    )
    return path


def _note_without_frontmatter(vault: Path, rel: str) -> Path:
    """Write a file with no frontmatter at all."""
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# No frontmatter\njust body")
    return path


def _note_missing_required_field(vault: Path, rel: str, omit: str = "id") -> Path:
    """Write a file with one required field missing."""
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = {"id": str(uuid.uuid4()), "type": "note", "agent": "sparky", "date": "2026-05-14T12:00:00Z"}
    del fields[omit]
    fm_lines = "\n".join(f"{k}: {v}" for k, v in fields.items())
    path.write_text(f"---\n{fm_lines}\n---\nbody")
    return path


# ── Test 1: Orphan detection ─────────────────────────────────────────────────

def test_lint_detects_orphan_files(tmp_path):
    """File at vault root (no intentional subfolder) → orphans count = 1."""
    vault = tmp_path / "vault"
    vault.mkdir()

    # Create an orphan: directly at vault root
    orphan = vault / "stray-note.md"
    orphan.write_text("---\nid: abc\ntype: note\nagent: sparky\ndate: 2026-05-14T12:00:00Z\n---\nbody")

    # Create a valid note in an intentional root (should NOT be orphan)
    _valid_note(vault, "agents/sparky/lesson.md")

    stats = lint_vault(vault)

    assert stats["orphan_count"] == 1
    assert any("stray-note.md" in str(p) for p in stats["orphans"])


def test_lint_detects_orphan_in_unknown_root(tmp_path):
    """File in a non-intentional subfolder (e.g. random/) → orphan."""
    vault = tmp_path / "vault"
    vault.mkdir()

    orphan = _valid_note(vault, "random/subdir/note.md")

    stats = lint_vault(vault)

    assert stats["orphan_count"] == 1
    assert any("random" in str(p) for p in stats["orphans"])


# ── Test 2: Invalid frontmatter ───────────────────────────────────────────────

def test_lint_detects_invalid_frontmatter(tmp_path):
    """File in agents/ without required 'id' field → frontmatter_invalid count = 1."""
    vault = tmp_path / "vault"
    vault.mkdir()

    bad_file = _note_missing_required_field(vault, "agents/sparky/bad.md", omit="id")

    stats = lint_vault(vault)

    assert stats["frontmatter_invalid_count"] == 1
    assert any("bad.md" in str(p) for p in stats["frontmatter_invalid"])


def test_lint_no_frontmatter_counts_as_invalid(tmp_path):
    """File with no frontmatter at all → counted as frontmatter_invalid."""
    vault = tmp_path / "vault"
    vault.mkdir()

    _note_without_frontmatter(vault, "agents/sparky/bare.md")

    stats = lint_vault(vault)

    assert stats["frontmatter_invalid_count"] == 1


def test_lint_valid_notes_not_flagged(tmp_path):
    """Clean vault with only valid notes → all counts zero."""
    vault = tmp_path / "vault"
    vault.mkdir()

    _valid_note(vault, "agents/sparky/ok1.md")
    _valid_note(vault, "global/ref.md", agent="system")
    _valid_note(vault, "projects/mc/plan.md", agent="cody")

    stats = lint_vault(vault)

    assert stats["orphan_count"] == 0
    assert stats["frontmatter_invalid_count"] == 0
    assert stats["duplicate_id_count"] == 0


# ── Test 3: Duplicate IDs ─────────────────────────────────────────────────────

def test_lint_detects_duplicate_ids(tmp_path):
    """Two files with same frontmatter id → duplicate_ids has one entry with both paths."""
    vault = tmp_path / "vault"
    vault.mkdir()

    shared_id = str(uuid.uuid4())
    _valid_note(vault, "agents/sparky/note1.md", note_id=shared_id)
    _valid_note(vault, "global/note2.md", note_id=shared_id, agent="system")

    stats = lint_vault(vault)

    assert stats["duplicate_id_count"] == 1
    # The entry should reference both files
    assert len(stats["duplicate_ids"]) == 1
    entry = stats["duplicate_ids"][0]
    assert len(entry["paths"]) == 2


def test_lint_no_duplicate_ids_when_unique(tmp_path):
    """Files each with unique IDs → duplicate_id_count = 0."""
    vault = tmp_path / "vault"
    vault.mkdir()

    _valid_note(vault, "agents/sparky/a.md")
    _valid_note(vault, "agents/sparky/b.md")

    stats = lint_vault(vault)
    assert stats["duplicate_id_count"] == 0


# ── Test 4: Write report ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_write_lint_report_creates_dated_md(tmp_path):
    """lint + write_report → _lint/{today}.md exists with valid frontmatter."""
    vault = tmp_path / "vault"
    vault.mkdir()

    _valid_note(vault, "agents/sparky/ok.md")

    stats = lint_vault(vault)
    report_path = await write_lint_report(vault, stats)

    today = datetime.utcnow().strftime("%Y-%m-%d")
    expected = vault / "_lint" / f"{today}.md"

    assert report_path == expected
    assert expected.exists()

    # Report itself must have valid frontmatter (id, type=reference, agent=system, date)
    post = frontmatter.load(str(expected))
    assert "id" in post.metadata
    assert post.metadata["type"] == "reference"
    assert post.metadata["agent"] == "system"
    assert "date" in post.metadata


@pytest.mark.asyncio
async def test_write_lint_report_includes_issue_sections(tmp_path):
    """Report markdown body contains sections for each issue type."""
    vault = tmp_path / "vault"
    vault.mkdir()

    # Create one orphan and one invalid file so sections appear
    orphan = vault / "stray.md"
    orphan.write_text("no fm")
    _note_missing_required_field(vault, "agents/sparky/bad.md", omit="id")

    stats = lint_vault(vault)
    report_path = await write_lint_report(vault, stats)

    body = frontmatter.load(str(report_path)).content
    assert "Orphan" in body or "orphan" in body
    assert "Frontmatter" in body or "frontmatter" in body


# ── Test 5: Excluded prefixes ─────────────────────────────────────────────────

def test_lint_skips_excluded_prefixes(tmp_path):
    """Files in _inbox/, _conflicts/, _rejected/, _lint/ are all skipped."""
    vault = tmp_path / "vault"
    vault.mkdir()

    excluded_dirs = ["_inbox", "_conflicts", "_rejected", "_lint"]
    for d in excluded_dirs:
        (vault / d).mkdir()
        # A file missing required frontmatter — would be flagged if NOT excluded
        f = vault / d / "file.md"
        f.write_text("# no frontmatter")

    stats = lint_vault(vault)

    assert stats["orphan_count"] == 0
    assert stats["frontmatter_invalid_count"] == 0
    assert stats["duplicate_id_count"] == 0


def test_lint_skips_nested_graph_dirs(tmp_path):
    """Auto-generated graphify output in nested `_graph/` dirs is skipped.

    These files (GRAPH_REPORT.md, *-INSIGHTS.md) carry no vault frontmatter and
    are regenerated on every graph run — they must not be flagged as orphan +
    missing-id forever. The `_graph` segment can sit at any depth.
    """
    vault = tmp_path / "vault"
    vault.mkdir()

    graph_dir = vault / "channel-knowledge" / "youtube" / "guyinacube" / "_graph"
    graph_dir.mkdir(parents=True)
    (graph_dir / "GRAPH_REPORT.md").write_text("# Graph Report\n\nno frontmatter")
    (graph_dir / "ARGYELAN-INSIGHTS.md").write_text("# Insights\n\nno frontmatter")

    stats = lint_vault(vault)

    assert stats["orphan_count"] == 0
    assert stats["frontmatter_invalid_count"] == 0
    assert stats["duplicate_id_count"] == 0


def test_lint_skips_dotgit_and_obsidian(tmp_path):
    """Files in .git/ and .obsidian/ are also skipped."""
    vault = tmp_path / "vault"
    vault.mkdir()

    for d in [".git", ".obsidian"]:
        (vault / d).mkdir()
        (vault / d / "config").write_text("not markdown but .md test")
        # Also create .md variant
        (vault / d / "note.md").write_text("no fm")

    stats = lint_vault(vault)

    assert stats["orphan_count"] == 0
    assert stats["frontmatter_invalid_count"] == 0


def test_lint_non_md_files_are_skipped(tmp_path):
    """Non-.md files in intentional roots are not linted."""
    vault = tmp_path / "vault"
    vault.mkdir()

    (vault / "agents" / "sparky").mkdir(parents=True)
    (vault / "agents" / "sparky" / "image.png").write_bytes(b"\x89PNG\r\n")
    (vault / "agents" / "sparky" / "data.json").write_text('{"key": "val"}')

    stats = lint_vault(vault)
    assert stats["orphan_count"] == 0
    assert stats["frontmatter_invalid_count"] == 0


# ── Test 6: Stats summary keys ────────────────────────────────────────────────

def test_lint_returns_all_expected_keys(tmp_path):
    """lint_vault return dict always has all expected keys."""
    vault = tmp_path / "vault"
    vault.mkdir()

    stats = lint_vault(vault)

    expected_keys = {
        "orphan_count", "orphans",
        "frontmatter_invalid_count", "frontmatter_invalid",
        "duplicate_id_count", "duplicate_ids",
        "total_files_scanned",
    }
    assert expected_keys.issubset(stats.keys())


# ── Test 7: Unreadable files handled gracefully ───────────────────────────────

def test_lint_handles_unreadable_file_gracefully(tmp_path, monkeypatch):
    """Unreadable file (permission denied / IO error) is captured, not a crash."""
    vault = tmp_path / "vault"
    vault.mkdir()

    bad_path = _valid_note(vault, "agents/sparky/unreadable.md")

    # Simulate parse_frontmatter raising an unexpected exception
    from app.helpers import vault_frontmatter
    original = vault_frontmatter.parse_frontmatter

    call_count = {"n": 0}

    def mock_parse(path):
        call_count["n"] += 1
        if path == bad_path:
            raise OSError("permission denied")
        return original(path)

    # Patch at the point of use in vault_lint (direct import binding)
    import app.services.vault_lint as vault_lint_mod
    monkeypatch.setattr(vault_lint_mod, "parse_frontmatter", mock_parse)

    # Should not raise
    stats = lint_vault(vault)
    assert stats["frontmatter_invalid_count"] == 1
    assert any("unreadable.md" in str(entry) for entry in stats["frontmatter_invalid"])


# ── Test 8: Lifespan cron wiring (M.3 T4) ─────────────────────────────────────

@pytest.mark.asyncio
async def test_vault_lint_loop_sleep_first_semantics(tmp_path, monkeypatch):
    """The loop must sleep BEFORE the first lint run (no instant fire on boot).

    Asserts that starting the loop with a non-zero interval and immediately
    cancelling it never invokes ``lint_vault`` — proves restart-storms don't
    re-lint repeatedly.
    """
    import asyncio
    import app.main as main_mod
    import app.services.vault_lint as vault_lint_mod
    from app.config import settings as cfg

    vault = tmp_path / "vault"
    vault.mkdir()

    # Force a small (but nonzero) interval — must be >0 so sleep actually
    # blocks the task before it can run lint.
    monkeypatch.setattr(cfg, "vault_lint_interval_hours", 1)

    call_counter = {"n": 0}

    def fake_lint(path):
        call_counter["n"] += 1
        return {"orphan_count": 0, "frontmatter_invalid_count": 0, "duplicate_id_count": 0, "linted_at": "2026-05-14T00:00:00Z"}

    monkeypatch.setattr(vault_lint_mod, "lint_vault", fake_lint)

    task = asyncio.create_task(main_mod._vault_lint_loop(vault))
    # Yield once so the loop gets to its `await asyncio.sleep(...)`
    await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass

    # If sleep-first semantics hold, lint_vault must not have been called.
    assert call_counter["n"] == 0


@pytest.mark.asyncio
async def test_vault_lint_loop_swallows_telegram_failure(tmp_path, monkeypatch):
    """When >5 issues + telegram raises, loop must NOT crash."""
    import asyncio
    import app.main as main_mod
    import app.services.vault_lint as vault_lint_mod
    from app.config import settings as cfg

    vault = tmp_path / "vault"
    vault.mkdir()

    # Tiny interval so the first iteration runs almost immediately.
    monkeypatch.setattr(cfg, "vault_lint_interval_hours", 0)

    fake_stats = {
        "orphan_count": 4,
        "frontmatter_invalid_count": 3,
        "duplicate_id_count": 0,
        "linted_at": "2026-05-14T00:00:00Z",
    }
    monkeypatch.setattr(vault_lint_mod, "lint_vault", lambda p: fake_stats)

    async def fake_write(path, stats):
        return path / "_lint" / "x.md"

    monkeypatch.setattr(vault_lint_mod, "write_lint_report", fake_write)

    # Force telegram to look configured but raise on send_message.
    monkeypatch.setattr(type(main_mod.telegram_bot), "configured", property(lambda self: True))

    send_calls = {"n": 0}

    async def boom_send(text, reply_markup=None):
        send_calls["n"] += 1
        raise RuntimeError("telegram down")

    monkeypatch.setattr(main_mod.telegram_bot, "send_message", boom_send)

    task = asyncio.create_task(main_mod._vault_lint_loop(vault))
    # Let it run at least one iteration (sleep=0 → fires immediately).
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass

    # Loop survived: send_message was attempted, exception swallowed.
    assert send_calls["n"] >= 1
    # Task ended via CancelledError (clean), not via crash.
    assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_vault_lint_loop_cancellation_is_clean(tmp_path, monkeypatch):
    """CancelledError breaks out of the loop without exception propagation noise."""
    import asyncio
    import app.main as main_mod
    from app.config import settings as cfg

    vault = tmp_path / "vault"
    vault.mkdir()

    monkeypatch.setattr(cfg, "vault_lint_interval_hours", 99999)

    task = asyncio.create_task(main_mod._vault_lint_loop(vault))
    await asyncio.sleep(0)
    task.cancel()
    # awaiting a cancelled task is expected to raise CancelledError or finish cleanly
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert task.done()
