"""Tests for last_viewed_at tracking via Redis queue + DB batch flush."""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.services.vault_activity import VaultActivity, VIEW_QUEUE_KEY


def _make_pipeline_mock(redis_mock):
    """Create a pipeline mock that collects commands and returns results on execute()."""
    pipe = AsyncMock()
    # Store references to the redis mock's return values so pipeline
    # commands return the same data as the underlying mock.
    pipe._results = []

    def _make_pipe_lrange(key, start, end):
        pipe._results.append(redis_mock.lrange.return_value)
        return pipe

    def _make_pipe_ltrim(key, start, end):
        pipe._results.append("OK")
        return pipe

    pipe.lrange = _make_pipe_lrange
    pipe.ltrim = _make_pipe_ltrim

    async def _execute():
        return pipe._results

    pipe.execute = _execute
    return pipe


@pytest.fixture
def redis_mock():
    mock = AsyncMock()
    mock.zincrby = AsyncMock()
    mock.expire = AsyncMock()
    mock.lpush = AsyncMock()
    mock.lrange = AsyncMock(return_value=[])
    mock.ltrim = AsyncMock()
    mock.llen = AsyncMock(return_value=0)

    # pipeline() is a sync method in redis.asyncio — returns Pipeline object
    mock.pipeline = lambda: _make_pipeline_mock(mock)

    return mock


class TestEnqueueViewForDb:
    def test_enqueue_pushes_note_id_to_redis_list(self, redis_mock):
        activity = VaultActivity(redis_mock)

        asyncio.get_event_loop().run_until_complete(
            activity.enqueue_view_for_db("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        )

        redis_mock.lpush.assert_called_once_with(
            VIEW_QUEUE_KEY,
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        )

    def test_enqueue_also_tracks_redis_heatmap(self, redis_mock):
        activity = VaultActivity(redis_mock)

        asyncio.get_event_loop().run_until_complete(
            activity.enqueue_view_for_db("test-id", path="agents/researcher/lessons/test.md")
        )

        # Should still call zincrby for the heatmap
        redis_mock.zincrby.assert_called()


class TestFlushViewsToDb:
    def test_flush_reads_queue_and_returns_ids(self, redis_mock):
        redis_mock.lrange.return_value = [
            b"id-1",
            b"id-2",
            b"id-3",
        ]
        redis_mock.llen.return_value = 3

        activity = VaultActivity(redis_mock)

        ids = asyncio.get_event_loop().run_until_complete(
            activity.flush_view_queue()
        )

        assert ids == ["id-1", "id-2", "id-3"]
        # Pipeline approach: ltrim is called on the pipeline, not directly on redis

    def test_flush_empty_queue_returns_empty(self, redis_mock):
        redis_mock.lrange.return_value = []
        redis_mock.llen.return_value = 0

        activity = VaultActivity(redis_mock)

        ids = asyncio.get_event_loop().run_until_complete(
            activity.flush_view_queue()
        )

        assert ids == []


class TestViewQueueKey:
    def test_key_name(self):
        assert VIEW_QUEUE_KEY == "mc:vault:view_queue"
