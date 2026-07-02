"""Phase 6 RedisKeys static-method tests (Plan 06-01 lands the methods).

Production code references:
  - backend/app/redis_client.py — RedisKeys.compaction_lock(agent_id)
  - backend/app/redis_client.py — RedisKeys.recovery_inprogress(agent_id, task_id)

Plan 06-01 deviation (Rule 3 — auto-fix blocking): Wave 0 (Plan 06-00) was not
applied to this worktree's base, so the canonical xfail stub file did not yet
exist. The Plan 06-01 action item 4 directs the executor to flip the stubs to
real assertions; since the stubs would have flipped immediately upon Plan 06-01
landing anyway, this file is created in its final PASS state directly. See
.planning/phases/06-context-management-auto-recovery/06-01-SUMMARY.md.
"""
from app.redis_client import RedisKeys


def test_redis_keys_compaction_lock_returns_mc_compaction_agent_id():
    """CTX-02 dedup key must be exactly 'mc:compaction:{agent_id}'."""
    assert RedisKeys.compaction_lock("abc-123") == "mc:compaction:abc-123"


def test_redis_keys_recovery_inprogress_returns_mc_recovery_inprogress_agent_task():
    """REC-01 tiered-recovery in-progress key must be exactly
    'mc:recovery:inprogress:{agent_id}:{task_id}'."""
    assert (
        RedisKeys.recovery_inprogress("a-1", "t-2")
        == "mc:recovery:inprogress:a-1:t-2"
    )
