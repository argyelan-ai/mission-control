"""Task-Delete räumt Playwright-MCP-Medien (E2E-Videos) mit."""
import uuid

from app.services import mcp_media_cleanup


def test_deletes_task_dir_only(tmp_path, monkeypatch):
    root = tmp_path / "mcp-screenshots"
    tid = uuid.uuid4()
    keep = uuid.uuid4()
    (root / str(tid)).mkdir(parents=True)
    (root / str(tid) / "e2e-run.webm").write_bytes(b"x")
    (root / str(keep)).mkdir()
    monkeypatch.setattr(mcp_media_cleanup, "_candidate_roots", lambda: [str(root)])

    assert mcp_media_cleanup.delete_mcp_media_for_task(tid) == 1
    assert not (root / str(tid)).exists()
    assert (root / str(keep)).exists()  # fremde Tasks unberührt


def test_missing_dir_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_media_cleanup, "_candidate_roots", lambda: [str(tmp_path)])
    assert mcp_media_cleanup.delete_mcp_media_for_task(uuid.uuid4()) == 0


def test_traversal_guard(tmp_path, monkeypatch):
    (tmp_path / "outside").mkdir()
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(mcp_media_cleanup, "_candidate_roots", lambda: [str(root)])
    assert mcp_media_cleanup.delete_mcp_media_for_task("../outside") == 0
    assert (tmp_path / "outside").exists()
