"""Tests fuer sync_agent_skills_to_disk — Skill-Files landen im Worker-Container.

Bug-Repro 2026-04-24 (Boss Acme-Corp-Steckbrief Reflection):
Shakespeare hatte acme-corp-brand in cli_skills, aber der Skill-Ordner fehlte
im Container. Er musste WebFetch nutzen statt die Skill-Dateien zu lesen.

Die Funktion existierte bereits in plugin_manager.py:479, wurde aber von NIEMANDEM
aufgerufen — sync_docker_agent_files hat sie nie getriggered. Fix: Integration in
sync_docker_agent_files + Symlink-Resolution fuer Skills die auf Deliverables
zeigen (z.B. acme-corp-brand → /Users/testuser/.mc/deliverables/sparky/...).
"""
from pathlib import Path

import pytest

from app.services.plugin_manager import sync_agent_skills_to_disk


def _setup_shared_skills(tmp_path: Path, skill_names: list[str]) -> Path:
    """Simuliert das shared ~/.mc/skills/ Verzeichnis mit Test-Skills."""
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
    """cli_skills=None → alle Custom-Skills aus shared dir kopiert."""
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    _setup_shared_skills(tmp_path, ["skill-a", "skill-b", "skill-c"])

    result = sync_agent_skills_to_disk("shakespeare", cli_skills=None)

    target = _agent_skills_dir(tmp_path, "shakespeare")
    assert (target / "skill-a" / "SKILL.md").exists()
    assert (target / "skill-b" / "SKILL.md").exists()
    assert (target / "skill-c" / "SKILL.md").exists()
    assert all(result[s] for s in ["skill-a", "skill-b", "skill-c"])


def test_sync_empty_list_removes_all_skills(tmp_path, monkeypatch):
    """cli_skills=[] → Ziel-Dir wird geleert (keine Skills erlaubt)."""
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    _setup_shared_skills(tmp_path, ["skill-a"])

    # Erst syncen mit None (alle), dann mit [] (keine)
    sync_agent_skills_to_disk("bot", cli_skills=None)
    target = _agent_skills_dir(tmp_path, "bot")
    assert (target / "skill-a").exists()

    result = sync_agent_skills_to_disk("bot", cli_skills=[])
    assert not (target / "skill-a").exists(), "Leere Allowlist muss skill-a entfernen"
    assert result == {}


def test_sync_allowlist_only_copies_wanted(tmp_path, monkeypatch):
    """cli_skills=['skill-a'] → nur skill-a landet im Ziel, skill-b NICHT."""
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    _setup_shared_skills(tmp_path, ["skill-a", "skill-b", "skill-c"])

    result = sync_agent_skills_to_disk("agent1", cli_skills=["skill-a"])

    target = _agent_skills_dir(tmp_path, "agent1")
    assert (target / "skill-a" / "SKILL.md").exists()
    assert not (target / "skill-b").exists()
    assert not (target / "skill-c").exists()
    assert result == {"skill-a": True}


def test_sync_resolves_symlinks_to_real_content(tmp_path, monkeypatch):
    """Der Real-World-Case: medewo-gruppe-brand liegt als Symlink auf ein
    Deliverable. Sync muss den Symlink aufloesen und echte Files kopieren,
    weil der Docker-Mount-Boundary Symlinks auf Pfade ausserhalb des Mounts
    bricht (dangling im Container)."""
    monkeypatch.setenv("HOME_HOST", str(tmp_path))

    # Realer Skill im Deliverables-Pfad (ausserhalb des shared skills dirs)
    real_dir = tmp_path / "deliverables" / "some-task" / "brand-skill-content"
    real_dir.mkdir(parents=True)
    (real_dir / "SKILL.md").write_text("---\nname: brand\n---\n# Brand-Content\n")
    (real_dir / "colors.md").write_text("# Primary #005850")

    # Shared skills dir mit Symlink (wie ~/.mc/skills/acme-corp-brand
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
    """Wenn cli_skills einen Namen hat der nicht im shared dir existiert,
    wird er schlicht ignoriert (kein error) — nur existierende werden kopiert."""
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    _setup_shared_skills(tmp_path, ["good"])

    result = sync_agent_skills_to_disk("ag", cli_skills=["good", "missing-skill"])

    target = _agent_skills_dir(tmp_path, "ag")
    assert (target / "good" / "SKILL.md").exists()
    assert not (target / "missing-skill").exists()
    # missing-skill wird aus available rausgefiltert (weil nicht in available list)
    assert "missing-skill" not in result
    assert result == {"good": True}


def test_sync_replaces_stale_skill(tmp_path, monkeypatch):
    """Wenn Skill im Ziel existiert mit altem Content, wird er beim Re-Sync
    komplett ersetzt (vollstaendiges rmtree + copytree)."""
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
    """Wenn ~/.mc/skills/ nicht existiert → kein crash, empty result."""
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    # shared skills dir NICHT angelegt
    result = sync_agent_skills_to_disk("ag", cli_skills=None)
    assert result == {}
    # Target dir sollte trotzdem existieren (sauberer Zustand)
    target = _agent_skills_dir(tmp_path, "ag")
    assert target.exists()
