import pytest
from pathlib import Path
from app.services.vault_cleanup import load_notes_from_vault


def test_loader_parses_frontmatter_and_content(tmp_path):
    vault = tmp_path / "vault"
    (vault / "memory" / "global").mkdir(parents=True)
    (vault / "memory" / "global" / "abc.md").write_text(
        "---\n"
        "agent: system\n"
        "type: journal\n"
        "tags: [auto, task_done]\n"
        "---\n"
        "**Task erledigt:** something\n"
    )
    notes = load_notes_from_vault(vault)
    assert len(notes) == 1
    n = notes[0]
    assert n.agent == "system"
    assert n.note_type == "journal"
    assert n.tags == ["auto", "task_done"]
    assert n.content.strip().startswith("**Task erledigt:**")
    assert n.path == "memory/global/abc.md"


def test_loader_skips_inbox_and_rejected(tmp_path):
    vault = tmp_path / "vault"
    (vault / "_inbox").mkdir(parents=True)
    (vault / "_inbox" / "x.md").write_text("---\nagent: a\n---\nstub")
    (vault / "_rejected").mkdir(parents=True)
    (vault / "_rejected" / "y.md").write_text("---\nagent: a\n---\nstub")
    (vault / "memory").mkdir(parents=True)
    (vault / "memory" / "z.md").write_text("---\nagent: a\ntype: knowledge\n---\nbody")
    notes = load_notes_from_vault(vault)
    paths = [n.path for n in notes]
    assert paths == ["memory/z.md"]


def test_loader_handles_missing_frontmatter_gracefully(tmp_path):
    vault = tmp_path / "vault"
    (vault / "memory").mkdir(parents=True)
    (vault / "memory" / "no-fm.md").write_text("just plain content, no frontmatter")
    notes = load_notes_from_vault(vault)
    assert len(notes) == 1
    assert notes[0].agent == ""
    assert notes[0].note_type == ""
    assert notes[0].tags == []
    assert notes[0].content.startswith("just plain content")
