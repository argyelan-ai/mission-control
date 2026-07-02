import pytest
from fakeredis import aioredis as fakeredis_async
from app.services.vault_activity import VaultActivity


@pytest.fixture
async def activity():
    redis = fakeredis_async.FakeRedis(decode_responses=True)
    yield VaultActivity(redis=redis)
    await redis.flushall()
    await redis.aclose()


@pytest.mark.asyncio
async def test_track_view_increments_score(activity):
    await activity.track_view("agents/sparky/lessons/x.md", user_id="mark")
    await activity.track_view("agents/sparky/lessons/x.md", user_id="mark")

    top = await activity.top_n(limit=10, window="30d")
    assert len(top) == 1
    assert top[0]["path"] == "agents/sparky/lessons/x.md"
    assert top[0]["score"] == 2.0


@pytest.mark.asyncio
async def test_top_n_returns_sorted(activity):
    await activity.track_view("a.md", user_id="mark")
    for _ in range(3):
        await activity.track_view("b.md", user_id="mark")
    for _ in range(2):
        await activity.track_view("c.md", user_id="mark")

    top = await activity.top_n(limit=10, window="30d")
    assert [t["path"] for t in top] == ["b.md", "c.md", "a.md"]


@pytest.mark.asyncio
async def test_track_write_uses_separate_sortedset(activity):
    await activity.track_write("agents/sparky/lessons/x.md", source="watcher")
    await activity.track_write("agents/sparky/lessons/x.md", source="watcher")

    # writes should NOT appear in track_view heatmap
    view_top = await activity.top_n_views(limit=10)
    assert view_top == []

    write_top = await activity.top_n_writes(limit=10)
    assert len(write_top) == 1
    assert write_top[0]["path"] == "agents/sparky/lessons/x.md"
    assert write_top[0]["score"] == 2.0


@pytest.mark.asyncio
async def test_track_view_unchanged(activity):
    """Existing track_view semantics still work for the view-only path."""
    await activity.track_view("agents/sparky/lessons/x.md", user_id="mark")
    top = await activity.top_n_views(limit=10)
    assert len(top) == 1
    assert top[0]["score"] == 1.0


@pytest.mark.asyncio
async def test_top_n_alias_backward_compat(activity):
    """Old top_n() still works as alias for top_n_views()."""
    await activity.track_view("foo.md", user_id="mark")
    old_api = await activity.top_n(limit=10)
    new_api = await activity.top_n_views(limit=10)
    assert old_api == new_api
