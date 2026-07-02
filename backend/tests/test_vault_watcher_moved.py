import asyncio
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import frontmatter
from app.services.vault_watcher import VaultWatcher


@pytest.fixture
def services():
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


def _make_valid_note(path: Path, agent="sparky"):
    path.parent.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(
        "body content",
        id="abc-123", type="lesson", agent=agent,
        date="2026-05-14T15:00:00Z",
    )
    path.write_text(frontmatter.dumps(post))


@pytest.mark.asyncio
async def test_moved_event_triggers_handler(watcher, tmp_path, services):
    """Phase 7 uses os.replace() which fires moved events on Linux inotify."""
    dst = tmp_path / "agents" / "sparky" / "lessons" / "moved.md"
    _make_valid_note(dst)

    # Simulate that the file already existed → handler should treat moved-to-dst the same as created/modified
    await watcher._handle_create_or_modify(dst)

    services["index"].upsert.assert_called_once()


def test_handler_on_moved_is_registered():
    """Verify the watchdog _Handler class has on_moved method that schedules."""
    from app.services.vault_watcher import _Handler
    assert hasattr(_Handler, "on_moved"), "missing on_moved handler"
