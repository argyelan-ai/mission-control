from pathlib import Path
import threading
import pytest
import frontmatter
from app.services.vault_index import VaultIndex


@pytest.fixture
def index(tmp_path):
    db_path = tmp_path / "test_index.db"
    return VaultIndex(db_path=db_path, vault_path=tmp_path)


def _make_note(vault: Path, rel_path: str, **meta) -> Path:
    full = vault / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post("body content", **meta)
    full.write_text(frontmatter.dumps(post))
    return full


def test_upsert_new_note(index, tmp_path):
    file = _make_note(
        tmp_path, "agents/sparky/lessons/test.md",
        id="abc", type="lesson", agent="sparky",
        date="2026-05-14T15:00:00Z", tags=["api", "xai"]
    )
    post = frontmatter.load(str(file))
    index.upsert(file, post)

    rows = list(index.list_all())
    assert len(rows) == 1
    assert rows[0]["path"] == "agents/sparky/lessons/test.md"
    assert rows[0]["agent"] == "sparky"
    assert rows[0]["type"] == "lesson"


def test_upsert_updates_existing(index, tmp_path):
    file = _make_note(
        tmp_path, "agents/cody/lessons/x.md",
        id="abc", type="lesson", agent="cody", date="2026-05-14T15:00:00Z"
    )
    post = frontmatter.load(str(file))
    index.upsert(file, post)

    post.content = "updated body"
    index.upsert(file, post)

    rows = list(index.list_all())
    assert len(rows) == 1
    assert "updated body" in rows[0]["content"]


def test_search_by_query(index, tmp_path):
    _make_note(tmp_path, "a.md", id="1", type="lesson", agent="sparky",
               date="2026-05-14T15:00:00Z", tags=["api"])
    _make_note(tmp_path, "b.md", id="2", type="lesson", agent="cody",
               date="2026-05-14T15:00:00Z", tags=["frontend"])
    # Need to actually insert content
    for rel in ("a.md", "b.md"):
        file = tmp_path / rel
        post = frontmatter.load(str(file))
        post.content = "rate limiting xai" if rel == "a.md" else "react components"
        # Re-write to disk so content matches
        file.write_text(frontmatter.dumps(post))
        index.upsert(file, post)

    hits = list(index.search("xai"))
    assert len(hits) == 1
    assert hits[0]["agent"] == "sparky"


def test_search_filter_by_agent(index, tmp_path):
    _make_note(tmp_path, "a.md", id="1", type="lesson", agent="sparky",
               date="2026-05-14T15:00:00Z")
    _make_note(tmp_path, "b.md", id="2", type="lesson", agent="cody",
               date="2026-05-14T15:00:00Z")
    for rel in ("a.md", "b.md"):
        post = frontmatter.load(str(tmp_path / rel))
        index.upsert(tmp_path / rel, post)

    hits = list(index.search("body", agent="sparky"))
    assert all(h["agent"] == "sparky" for h in hits)


def test_search_handles_query_with_dashes_and_digits(index, tmp_path):
    """Real-world voice/agent queries like 'morgenbriefing-16-mai-2026'.

    Bug 2026-05-16: such queries crashed FTS5 with `no such column: 16`
    because FTS5 reads digits as column references and `-` as NOT operator.
    Fix: sanitize by wrapping each token in double quotes."""
    _make_note(tmp_path, "agents/researcher/deliverables/morgenbriefing-16-mai-2026.md",
               id="1", type="deliverable", agent="researcher",
               date="2026-05-16T05:00:00Z")
    post = frontmatter.load(str(tmp_path / "agents/researcher/deliverables/morgenbriefing-16-mai-2026.md"))
    post.content = "Morgenbriefing 16. Mai 2026 — Tech-News"
    index.upsert(tmp_path / "agents/researcher/deliverables/morgenbriefing-16-mai-2026.md", post)

    # Must not raise OperationalError
    hits = list(index.search("morgenbriefing-16-mai-2026"))
    assert len(hits) == 1
    assert hits[0]["agent"] == "researcher"


def test_search_handles_empty_query(index, tmp_path):
    """Empty/whitespace query short-circuits to no hits instead of crashing."""
    assert list(index.search("")) == []
    assert list(index.search("   ")) == []


def test_search_quotes_internal_special_chars(index, tmp_path):
    """Colons (column-filter), parens, and quotes inside the query must
    be neutralized rather than passed to the FTS5 parser."""
    _make_note(tmp_path, "a.md", id="1", type="lesson", agent="sparky",
               date="2026-05-14T15:00:00Z")
    post = frontmatter.load(str(tmp_path / "a.md"))
    post.content = "weird stuff with: colons"
    index.upsert(tmp_path / "a.md", post)

    # Each of these used to be either a parse error or a column-lookup error.
    for nasty in ("foo:bar", "(broken", 'has"quote', "left-right"):
        # Just assert it doesn't raise.
        list(index.search(nasty))


def test_search_filter_by_type(index, tmp_path):
    _make_note(tmp_path, "a.md", id="1", type="lesson", agent="sparky",
               date="2026-05-14T15:00:00Z")
    _make_note(tmp_path, "b.md", id="2", type="reference", agent="sparky",
               date="2026-05-14T15:00:00Z")
    for rel in ("a.md", "b.md"):
        post = frontmatter.load(str(tmp_path / rel))
        index.upsert(tmp_path / rel, post)

    hits = list(index.search("body", type="reference"))
    assert all(h["type"] == "reference" for h in hits)


def test_rebuild_from_vault_scans_all_md_files(index, tmp_path):
    # Create 3 valid files + 1 invalid + 1 _inbox (should be excluded)
    _make_note(tmp_path, "agents/sparky/a.md", id="1", type="lesson", agent="sparky",
               date="2026-05-14T15:00:00Z")
    _make_note(tmp_path, "agents/cody/b.md", id="2", type="lesson", agent="cody",
               date="2026-05-14T15:00:00Z")
    _make_note(tmp_path, "global/c.md", id="3", type="reference", agent="henry",
               date="2026-05-14T15:00:00Z")
    # Inbox file — should be skipped
    inbox = tmp_path / "_inbox" / "envelope.md"
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text("---\nop: upsert\n---\nbody")

    stats = index.rebuild_from_vault()
    assert stats["scanned"] == 3
    assert stats["indexed"] == 3
    assert stats["skipped"] >= 1  # at least the inbox file

    paths = {row["path"] for row in index.list_all()}
    assert "agents/sparky/a.md" in paths
    assert "_inbox/envelope.md" not in paths


def test_rebuild_handles_invalid_frontmatter(index, tmp_path, capsys):
    broken = tmp_path / "broken.md"
    broken.write_text("---\nbroken: : :\n---\nbody")

    stats = index.rebuild_from_vault()
    assert stats["errors"] == 1  # invalid file counted but not indexed
    assert stats["indexed"] == 0


def test_index_extracts_title_from_frontmatter(index, tmp_path):
    """list_all() must return the frontmatter title so build_graph can label nodes."""
    file = _make_note(
        tmp_path, "agents/sparky/references/my-note.md",
        id="t1", type="reference", agent="sparky",
        date="2026-05-15T00:00:00Z", title="My Human-Readable Title",
    )
    post = frontmatter.load(str(file))
    index.upsert(file, post)

    rows = list(index.list_all())
    assert len(rows) == 1
    assert rows[0]["title"] == "My Human-Readable Title"


def test_index_title_empty_when_missing(index, tmp_path):
    """Notes without a title field should return an empty string, not None or KeyError."""
    file = _make_note(
        tmp_path, "global/no-title.md",
        id="t2", type="lesson", agent="cody",
        date="2026-05-15T00:00:00Z",
    )
    post = frontmatter.load(str(file))
    index.upsert(file, post)

    rows = list(index.list_all())
    assert rows[0]["title"] == ""


def test_rebuild_preserves_title(index, tmp_path):
    """rebuild_from_vault() must index title so list_all() returns it after rebuild."""
    _make_note(
        tmp_path, "agents/rex/lessons/titled.md",
        id="t3", type="lesson", agent="rex",
        date="2026-05-15T00:00:00Z", title="Rex Lesson Title",
    )
    index.rebuild_from_vault()

    rows = list(index.list_all())
    assert len(rows) == 1
    assert rows[0]["title"] == "Rex Lesson Title"


def test_concurrent_upserts_are_serialized(index, tmp_path):
    """Two threads upserting same path should result in exactly one row (no race)."""
    file = _make_note(tmp_path, "agents/sparky/lessons/race.md",
                     id="r1", type="lesson", agent="sparky", date="2026-05-14T15:00:00Z")

    def writer(content_suffix):
        post = frontmatter.load(str(file))
        post.content = f"body {content_suffix}"
        index.upsert(file, post)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
    for t in threads: t.start()
    for t in threads: t.join()

    rows = list(index.list_all())
    assert len(rows) == 1  # No duplicate rows even with concurrent writes
