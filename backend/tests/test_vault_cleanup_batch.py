import uuid
import pytest
from pathlib import Path
from app.services.vault_cleanup import archive_batch


# Fixed UUIDs for deterministic tests
UUID_A = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
UUID_B = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
UUID_GOOD = uuid.UUID("00000000-0000-0000-0000-00000000600d")
UUID_MISS = uuid.UUID("00000000-0000-0000-0000-0000000077aa")


@pytest.mark.asyncio
async def test_archive_batch_moves_files_and_updates_postgres(tmp_path, session):
    """End-to-end: 2 notes archived, both moved to disk archive AND
    board_memory.archived_at set + archive_bucket/reason populated."""
    from app.models.memory import BoardMemory

    vault = tmp_path / "vault"
    archive = tmp_path / "archive" / "runA"
    (vault / "memory").mkdir(parents=True)
    (vault / "memory" / "a.md").write_text("---\nid: 0000-aaaa\nagent: system\n---\nbody")
    (vault / "memory" / "b.md").write_text("---\nid: 0000-bbbb\nagent: system\n---\nbody")

    bm_a = BoardMemory(id=UUID_A, agent_id=None, board_id=None, memory_type="journal", content="body", source="system")
    bm_b = BoardMemory(id=UUID_B, agent_id=None, board_id=None, memory_type="journal", content="body", source="system")
    session.add_all([bm_a, bm_b])
    await session.commit()

    plan = [
        ("memory/a.md", UUID_A, "H1"),
        ("memory/b.md", UUID_B, "H1"),
    ]
    result = await archive_batch(session, vault, archive, plan)
    assert result.total == 2
    assert result.moved == 2
    assert result.failed == 0

    # Files moved
    assert (archive / "memory" / "a.md").exists()
    assert not (vault / "memory" / "a.md").exists()

    # Postgres updated
    refreshed = await session.get(BoardMemory, UUID_A)
    assert refreshed.archived_at is not None
    assert refreshed.archive_bucket == "H1"
    assert refreshed.archive_reason == "auto_system_journal"


@pytest.mark.asyncio
async def test_archive_batch_continues_on_individual_failure(tmp_path, session):
    """One missing file should NOT abort the whole batch — failures are
    collected, the rest proceed."""
    from app.models.memory import BoardMemory

    vault = tmp_path / "vault"
    archive = tmp_path / "archive" / "runB"
    (vault / "memory").mkdir(parents=True)
    (vault / "memory" / "good.md").write_text("---\nid: 0000-good\n---\nbody")

    bm = BoardMemory(id=UUID_GOOD, agent_id=None, board_id=None, memory_type="journal", content="body", source="system")
    session.add(bm)
    await session.commit()

    plan = [
        ("memory/missing.md", UUID_MISS, "H1"),  # file doesn't exist
        ("memory/good.md", UUID_GOOD, "H1"),
    ]
    result = await archive_batch(session, vault, archive, plan)
    assert result.total == 2
    assert result.moved == 1
    assert result.failed == 1
    assert len(result.errors) == 1
    assert result.errors[0][0] == "memory/missing.md"
