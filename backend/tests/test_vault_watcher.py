import asyncio
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import frontmatter
from app.services.vault_watcher import VaultWatcher


@pytest.fixture
def services(tmp_path):
    return {
        "index": MagicMock(upsert=MagicMock()),
        "activity": MagicMock(track_view=AsyncMock(), track_write=AsyncMock()),
        "embeddings": MagicMock(upsert=AsyncMock(return_value={"ok": True})),
        "git": MagicMock(stage=MagicMock()),
        "redis": MagicMock(publish=AsyncMock()),
    }


@pytest.fixture
def watcher(tmp_path, services):
    return VaultWatcher(
        vault_path=tmp_path,
        index=services["index"],
        activity=services["activity"],
        embeddings=services["embeddings"],
        git=services["git"],
        redis=services["redis"],
    )


def _make_valid_note(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(
        "body content",
        id="abc-123",
        type="lesson",
        agent="sparky",
        date="2026-05-14T15:00:00Z",
        tags=["test"],
    )
    path.write_text(frontmatter.dumps(post))


@pytest.mark.asyncio
async def test_valid_file_triggers_index_upsert(watcher, tmp_path, services):
    file = tmp_path / "agents" / "sparky" / "lessons" / "x.md"
    _make_valid_note(file)
    await watcher._handle_create_or_modify(file)

    services["index"].upsert.assert_called_once()
    services["embeddings"].upsert.assert_awaited_once()
    services["git"].stage.assert_called_once_with(file)
    services["redis"].publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalid_frontmatter_goes_to_rejected(watcher, tmp_path, services):
    file = tmp_path / "agents" / "sparky" / "broken.md"
    file.parent.mkdir(parents=True)
    file.write_text("---\ntype: BANANA\n---\nbody")  # invalid type

    await watcher._handle_create_or_modify(file)

    services["index"].upsert.assert_not_called()
    rejected = tmp_path / "_rejected"
    assert rejected.exists()
    # Expect at least one file moved to _rejected/
    assert any(rejected.iterdir())


@pytest.mark.asyncio
async def test_path_traversal_rejects_file_outside_agent_folder(watcher, tmp_path, services):
    """Sparky writing under cody's folder should be rejected."""
    file = tmp_path / "agents" / "cody" / "stolen.md"
    file.parent.mkdir(parents=True)
    post = frontmatter.Post("body", id="x", type="lesson", agent="sparky",  # mismatch!
                             date="2026-05-14T15:00:00Z")
    file.write_text(frontmatter.dumps(post))

    await watcher._handle_create_or_modify(file)
    services["index"].upsert.assert_not_called()
    rejected = tmp_path / "_rejected"
    assert rejected.exists()


@pytest.mark.asyncio
async def test_excluded_paths_ignored(watcher, tmp_path, services):
    file = tmp_path / "_inbox" / "envelope.md"
    file.parent.mkdir()
    _make_valid_note(file)
    await watcher._handle_create_or_modify(file)
    services["index"].upsert.assert_not_called()


@pytest.mark.asyncio
async def test_trash_paths_not_reindexed(watcher, tmp_path, services):
    """Regression guard for the 2026-05-16 vault-delete leak.

    Soft-delete moves a note into _trash/<ts>-foo.md. The watcher's
    on_moved event fires with the new path; without _trash/ in
    EXCLUDED_PREFIXES the file got re-indexed under its trash path,
    making the deleted note re-appear in the list view (under a path
    the GET endpoint refuses to open → 404 on click).
    """
    trashed = tmp_path / "_trash" / "20260516T103000-agents__sparky__lessons__foo.md"
    trashed.parent.mkdir()
    _make_valid_note(trashed)
    await watcher._handle_create_or_modify(trashed)
    services["index"].upsert.assert_not_called()


class TestCommitTimerWiring:
    """Guards the 30s batch-commit wiring (PR #614 rework, Rex finding 1).

    Rex deleted the create_task from start() and the whole suite stayed
    green — nothing caught the timer silently disappearing again. These
    tests pin it: no timer task, no wiring.
    """

    @pytest.mark.asyncio
    async def test_start_spawns_commit_timer_task(self, watcher, tmp_path, services):
        assert getattr(watcher, "_commit_task", None) is None
        await watcher.start()
        try:
            task = getattr(watcher, "_commit_task", None)
            assert task is not None, (
                "start() did not spawn the commit timer — vault writes would "
                "stage forever without ever committing (the original defect)"
            )
            assert not task.done()
            assert watcher._commit_loop.__name__ in repr(task.get_coro())
        finally:
            await watcher.stop()

    @pytest.mark.asyncio
    async def test_commit_loop_flushes_with_interval(self, watcher, monkeypatch):
        from app.services import vault_watcher as vw_mod

        calls = []

        async def fake_flush(author):
            calls.append(author)
            return False

        monkeypatch.setattr(watcher.git, "flush_if_pending", fake_flush)
        monkeypatch.setattr(vw_mod, "COMMIT_INTERVAL_SECONDS", 0.01)

        task = asyncio.create_task(watcher._commit_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(calls) >= 2, (
            "commit loop did not flush repeatedly — interval wiring broken"
        )

    @pytest.mark.asyncio
    async def test_stop_drains_inflight_handler_before_flush(
        self, watcher, tmp_path, services
    ):
        """Shutdown must wait for in-flight handlers, else the note's
        stage() lands after the final flush and is lost (Rex finding 2)."""
        flush_done = []

        async def slow_flush(author):
            flush_done.append(author)
            return True

        async def slow_embeddings(*a, **k):
            await asyncio.sleep(0.05)
            return {"ok": True}

        services["git"].flush_if_pending = AsyncMock(side_effect=slow_flush)
        services["embeddings"].upsert = AsyncMock(side_effect=slow_embeddings)

        watcher._loop = asyncio.get_running_loop()
        note = tmp_path / "agents" / "sparky" / "drain.md"
        _make_valid_note(note)
        fut = asyncio.run_coroutine_threadsafe(
            watcher._handle_create_or_modify(note), watcher._loop
        )
        watcher._inflight.add(fut)
        await asyncio.sleep(0)  # let the handler start, it's now in flight

        await watcher.stop()

        services["git"].stage.assert_called_with(note)  # handler finished
        assert flush_done == ["shutdown"]  # flush ran AFTER the drain
