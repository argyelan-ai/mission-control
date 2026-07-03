"""Tests for sync_agent_skills_to_disk — skill files land in the worker container.

Bug repro 2026-04-24 (Boss Acme Corp brand profile reflection):
Shakespeare had acme-corp-brand in cli_skills, but the skill folder was missing
in the container. He had to use WebFetch instead of reading the skill files.

The function already existed in plugin_manager.py:479, but was called by NOBODY —
sync_docker_agent_files never triggered it. Fix: integration into
sync_docker_agent_files + symlink resolution for skills that point to
deliverables (e.g. acme-corp-brand → /Users/testuser/.mc/deliverables/sparky/...).
"""
from pathlib import Path

import pytest

from app.services.plugin_manager import sync_agent_skills_to_disk


def _setup_shared_skills(tmp_path: Path, skill_names: list[str]) -> Path:
    """Simulates the shared ~/.mc/skills/ directory with test skills."""
    shared = tmp_path / ".mc" / "skills"
    shared.mkdir(parents=True)
    for name in skill_names:
        skill_dir = shared / name
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\n---\n# {name}\nTest content.\n"
        )
    return shared


def _agent_skills_dir(tmp_path: Path, slug: str) -> Path:
    return tmp_path / ".mc" / "agents" / slug / "claude-config" / "skills"


def test_sync_all_skills_when_cli_skills_is_none(tmp_path, monkeypatch):
    """cli_skills=None → all custom skills copied from the shared dir."""
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    _setup_shared_skills(tmp_path, ["skill-a", "skill-b", "skill-c"])

    result = sync_agent_skills_to_disk("shakespeare", cli_skills=None)

    target = _agent_skills_dir(tmp_path, "shakespeare")
    assert (target / "skill-a" / "SKILL.md").exists()
    assert (target / "skill-b" / "SKILL.md").exists()
    assert (target / "skill-c" / "SKILL.md").exists()
    assert all(result[s] for s in ["skill-a", "skill-b", "skill-c"])


def test_sync_empty_list_removes_all_skills(tmp_path, monkeypatch):
    """cli_skills=[] → target dir gets emptied (no skills allowed)."""
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    _setup_shared_skills(tmp_path, ["skill-a"])

    # First sync with None (all), then with [] (none)
    sync_agent_skills_to_disk("bot", cli_skills=None)
    target = _agent_skills_dir(tmp_path, "bot")
    assert (target / "skill-a").exists()

    result = sync_agent_skills_to_disk("bot", cli_skills=[])
    assert not (target / "skill-a").exists(), "Leere Allowlist muss skill-a entfernen"
    assert result == {}


def test_sync_allowlist_only_copies_wanted(tmp_path, monkeypatch):
    """cli_skills=['skill-a'] → only skill-a lands in the target, skill-b does NOT."""
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    _setup_shared_skills(tmp_path, ["skill-a", "skill-b", "skill-c"])

    result = sync_agent_skills_to_disk("agent1", cli_skills=["skill-a"])

    target = _agent_skills_dir(tmp_path, "agent1")
    assert (target / "skill-a" / "SKILL.md").exists()
    assert not (target / "skill-b").exists()
    assert not (target / "skill-c").exists()
    assert result == {"skill-a": True}


def test_sync_resolves_symlinks_to_real_content(tmp_path, monkeypatch):
    """The real-world case: medewo-gruppe-brand sits as a symlink pointing to a
    deliverable. Sync must resolve the symlink and copy the real files,
    because the Docker mount boundary breaks symlinks pointing to paths
    outside the mount (dangling inside the container)."""
    monkeypatch.setenv("HOME_HOST", str(tmp_path))

    # Real skill in the deliverables path (outside the shared skills dir)
    real_dir = tmp_path / "deliverables" / "some-task" / "brand-skill-content"
    real_dir.mkdir(parents=True)
    (real_dir / "SKILL.md").write_text("---\nname: brand\n---\n# Brand-Content\n")
    (real_dir / "colors.md").write_text("# Primary #005850")

    # Shared skills dir with symlink (like ~/.mc/skills/acme-corp-brand
    # → /Users/testuser/.mc/deliverables/sparky/.../brand-guidelines-acme-corp)
    shared = tmp_path / ".mc" / "skills"
    shared.mkdir(parents=True)
    (shared / "brand").symlink_to(real_dir)

    result = sync_agent_skills_to_disk("ag", cli_skills=["brand"])

    target = _agent_skills_dir(tmp_path, "ag") / "brand"
    assert target.exists()
    assert not target.is_symlink(), "Target muss echter Dir sein, nicht Symlink"
    assert (target / "SKILL.md").read_text() == "---\nname: brand\n---\n# Brand-Content\n"
    assert (target / "colors.md").read_text() == "# Primary #005850"
    assert result == {"brand": True}


def test_sync_cli_skills_filters_unknown_names(tmp_path, monkeypatch):
    """If cli_skills has a name that doesn't exist in the shared dir,
    it's simply ignored (no error) — only existing ones are copied."""
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    _setup_shared_skills(tmp_path, ["good"])

    result = sync_agent_skills_to_disk("ag", cli_skills=["good", "missing-skill"])

    target = _agent_skills_dir(tmp_path, "ag")
    assert (target / "good" / "SKILL.md").exists()
    assert not (target / "missing-skill").exists()
    # missing-skill gets filtered out of available (because it's not in the available list)
    assert "missing-skill" not in result
    assert result == {"good": True}


def test_sync_replaces_stale_skill(tmp_path, monkeypatch):
    """If the skill exists in the target with old content, it gets fully
    replaced on re-sync (complete rmtree + copytree)."""
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    shared = _setup_shared_skills(tmp_path, ["brand"])
    (shared / "brand" / "SKILL.md").write_text("---\nname: brand\n---\n# VERSION_A\n")

    sync_agent_skills_to_disk("ag", cli_skills=["brand"])
    target = _agent_skills_dir(tmp_path, "ag") / "brand" / "SKILL.md"
    assert "VERSION_A" in target.read_text()

    # Update shared + re-sync
    (shared / "brand" / "SKILL.md").write_text("---\nname: brand\n---\n# VERSION_B\n")
    sync_agent_skills_to_disk("ag", cli_skills=["brand"])
    assert "VERSION_B" in target.read_text()
    assert "VERSION_A" not in target.read_text()


def test_sync_graceful_when_shared_dir_missing(tmp_path, monkeypatch):
    """If ~/.mc/skills/ doesn't exist → no crash, empty result."""
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    # shared skills dir NOT created
    result = sync_agent_skills_to_disk("ag", cli_skills=None)
    assert result == {}
    # Target dir should still exist (clean state)
    target = _agent_skills_dir(tmp_path, "ag")
    assert target.exists()
