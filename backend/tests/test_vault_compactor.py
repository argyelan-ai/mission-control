import asyncio
import pytest
import frontmatter
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from app.services.vault_compactor import VaultCompactor


@pytest.fixture
def compactor(tmp_path):
    redis_mock = MagicMock(set=AsyncMock(return_value=True), publish=AsyncMock())
    return VaultCompactor(vault_path=tmp_path, redis=redis_mock)


def _envelope(vault, name, target, agent, content, sha=None, idempotency=None):
    from hashlib import sha256
    envelope = vault / "_inbox" / name
    envelope.parent.mkdir(parents=True, exist_ok=True)
    body_sha = sha or sha256(content.encode()).hexdigest()
    fm = {
        "op": "upsert",
        "target": target,
        "agent_id": agent,
        "agent": agent,  # for canonical frontmatter
        "type": "lesson",
        "date": "2026-05-14T15:00:00Z",
        "id": f"{agent}-{name}",
        "sha256": body_sha,
        "idempotency_key": idempotency,
    }
    envelope.write_text(frontmatter.dumps(frontmatter.Post(content, **fm)))
    return envelope


@pytest.mark.asyncio
async def test_inbox_envelope_merged_to_canonical(compactor, tmp_path):
    env = _envelope(tmp_path, "20260514T1500_henry_x.md", "global/decisions/foo.md", "henry", "decision body")
    await compactor.compact()
    canonical = tmp_path / "global" / "decisions" / "foo.md"
    assert canonical.exists()
    assert not env.exists()  # envelope consumed


@pytest.mark.asyncio
async def test_idempotent_dedup(compactor, tmp_path):
    # Same idempotency_key twice → second should skip (Redis SET NX returns False)
    env1 = _envelope(tmp_path, "a.md", "global/x.md", "henry", "body", idempotency="henry-x")
    await compactor.compact()
    # Simulate Redis already-seen for second call
    compactor.redis.set = AsyncMock(return_value=False)  # NX failed = already seen
    env2 = _envelope(tmp_path, "b.md", "global/x.md", "henry", "body", idempotency="henry-x")
    await compactor.compact()
    canonical = tmp_path / "global" / "x.md"
    assert canonical.exists()
    # No conflicts dir if dedup worked
    assert not (tmp_path / "_conflicts").exists() or not any((tmp_path / "_conflicts").iterdir())


@pytest.mark.asyncio
async def test_conflict_writes_to_conflicts_dir(compactor, tmp_path):
    env1 = _envelope(tmp_path, "a.md", "global/x.md", "henry", "body version A")
    await compactor.compact()
    env2 = _envelope(tmp_path, "b.md", "global/x.md", "cody", "body version B")
    await compactor.compact()
    canonical = tmp_path / "global" / "x.md"
    assert canonical.exists()
    conflicts = tmp_path / "_conflicts"
    assert conflicts.exists() and any(conflicts.iterdir())


@pytest.mark.asyncio
async def test_envelope_missing_target_skipped(compactor, tmp_path, caplog):
    """Malformed envelope without 'target' frontmatter is skipped with log."""
    bad = tmp_path / "_inbox" / "no_target.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("---\nop: upsert\nagent_id: henry\n---\nbody")
    await compactor.compact()
    # Envelope should remain (logged but not consumed) OR be moved to _rejected — either is acceptable
    # The important thing: no canonical was written
    assert not (tmp_path / "global").exists()
