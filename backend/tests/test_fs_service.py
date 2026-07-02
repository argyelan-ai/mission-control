"""Tests for the sandboxed fs_service — containment guard + list/stat/stream."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services import fs_service
from app.services.fs_roots import FsRoot


def _root_at(tmp: Path) -> FsRoot:
    return FsRoot(
        key="test", label="Test", icon="x", subpath="",
        native_open=True, sensitive=False, container_override=str(tmp),
    )


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = _root_at(tmp_path)
    monkeypatch.setattr(fs_service, "get_browsable_root", lambda key: r)
    return r


# --- containment guard -----------------------------------------------------

def test_safe_join_allows_inside(root, tmp_path):
    (tmp_path / "sub").mkdir()
    assert fs_service.safe_join(root, "sub") == (tmp_path / "sub").resolve()


def test_safe_join_rejects_dotdot(root):
    with pytest.raises(fs_service.FsAccessError):
        fs_service.safe_join(root, "../../etc/passwd")


def test_safe_join_rejects_absolute(root):
    # leading slash is stripped → stays inside, but an attempt to climb fails
    with pytest.raises(fs_service.FsAccessError):
        fs_service.safe_join(root, "/../../../etc")


def test_safe_join_rejects_nul(root):
    with pytest.raises(fs_service.FsAccessError):
        fs_service.safe_join(root, "a\0b")


def test_safe_join_rejects_symlink_escape(root, tmp_path):
    outside = tmp_path.parent / "outside_secret"
    outside.mkdir()
    (outside / "loot.txt").write_text("secret")
    (tmp_path / "link").symlink_to(outside)
    with pytest.raises(fs_service.FsAccessError):
        fs_service.safe_join(root, "link/loot.txt")


# --- list / stat -----------------------------------------------------------

def test_list_dir_sorts_dirs_first(root, tmp_path):
    (tmp_path / "zeta.txt").write_text("z")
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta.md").write_text("# hi")
    names = [e.name for e in fs_service.list_dir("test", "")]
    assert names == ["alpha", "beta.md", "zeta.txt"]
    entries = {e.name: e for e in fs_service.list_dir("test", "")}
    assert entries["alpha"].is_directory is True
    assert entries["beta.md"].mime == "text/markdown"
    assert entries["zeta.txt"].size == 1


def test_list_dir_missing_raises(root):
    with pytest.raises(fs_service.FsNotFound):
        fs_service.list_dir("test", "nope")


def test_stat_file(root, tmp_path):
    (tmp_path / "f.json").write_text("{}")
    e = fs_service.stat("test", "f.json")
    assert e.is_directory is False
    assert e.mime == "application/json"
    assert e.size == 2


# --- stream / download -----------------------------------------------------

def test_read_stream_download_sets_attachment(root, tmp_path):
    (tmp_path / "report.pdf").write_bytes(b"%PDF-1.4")
    resp = fs_service.read_stream("test", "report.pdf", download=True)
    cd = resp.headers.get("content-disposition", "")
    assert cd.startswith("attachment")
    assert "report.pdf" in cd


def test_read_stream_inline_has_no_attachment(root, tmp_path):
    (tmp_path / "pic.png").write_bytes(b"\x89PNG")
    resp = fs_service.read_stream("test", "pic.png", download=False)
    assert "attachment" not in resp.headers.get("content-disposition", "")


def test_read_stream_directory_raises(root, tmp_path):
    (tmp_path / "d").mkdir()
    with pytest.raises(fs_service.FsNotFound):
        fs_service.read_stream("test", "d", download=False)


# --- runtime-aware deliverable resolution ----------------------------------

class _FakeAgent:
    def __init__(self, name, runtime, slug=None):
        self.name = name
        self.agent_runtime = runtime
        self.slug = slug


class _FakeDeliv:
    def __init__(self, path, agent_id=None):
        self.path = path
        self.agent_id = agent_id


class _FakeSession:
    def __init__(self, agent=None):
        self._agent = agent

    async def get(self, model, _id):
        return self._agent


HOME = "/Users/testuser"  # arbitrary pinned value for path-building assertions (see _pin_home)


@pytest.fixture(autouse=True)
def _pin_home(monkeypatch):
    monkeypatch.setattr(fs_service.settings, "home_host", HOME)


async def test_resolve_cli_bridge_injects_slug():
    d = _FakeDeliv("/deliverables/task1/report.pdf", agent_id="a")
    s = _FakeSession(_FakeAgent("Free Code", "cli-bridge"))
    assert await fs_service.resolve_deliverable(d, s, target="container") == "/deliverables/free-code/task1/report.pdf"
    assert await fs_service.resolve_deliverable(d, s, target="host") == f"{HOME}/.mc/deliverables/free-code/task1/report.pdf"


async def test_resolve_uses_stable_slug_column_over_name():
    # agent renamed but slug column pinned → on-disk path stays stable
    d = _FakeDeliv("/deliverables/task1/x.txt", agent_id="a")
    s = _FakeSession(_FakeAgent("Renamed Agent", "cli-bridge", slug="free-code"))
    assert await fs_service.resolve_deliverable(d, s, target="container") == "/deliverables/free-code/task1/x.txt"


async def test_resolve_host_worker_no_slug_legacy():
    d = _FakeDeliv(f"{HOME}/.mc/deliverables/task9/out.md", agent_id="h")
    s = _FakeSession(_FakeAgent("Hermes", "host"))
    assert await fs_service.resolve_deliverable(d, s, target="container") == "/deliverables/task9/out.md"
    assert await fs_service.resolve_deliverable(d, s, target="host") == f"{HOME}/.mc/deliverables/task9/out.md"


async def test_resolve_host_worker_slugged_post_normalization():
    # post-T9: host workers write <slug>/<task>; path already encodes it → no double-inject
    d = _FakeDeliv("~/.mc/deliverables/hermes/task9/out.md", agent_id="h")
    s = _FakeSession(_FakeAgent("Hermes", "host"))
    assert await fs_service.resolve_deliverable(d, s, target="container") == "/deliverables/hermes/task9/out.md"
    assert await fs_service.resolve_deliverable(d, s, target="host") == f"{HOME}/.mc/deliverables/hermes/task9/out.md"


async def test_resolve_named_volume_no_host_path():
    d = _FakeDeliv("/shared-deliverables/task1/shot.png", agent_id=None)
    s = _FakeSession(None)
    assert await fs_service.resolve_deliverable(d, s, target="container") == "/shared-deliverables/task1/shot.png"
    assert await fs_service.resolve_deliverable(d, s, target="host") is None


async def test_resolve_mcp_screenshot():
    d = _FakeDeliv("/shared-mcp/task1/page.png", agent_id=None)
    s = _FakeSession(None)
    assert await fs_service.resolve_deliverable(d, s, target="host") == f"{HOME}/.mc/mcp-screenshots/task1/page.png"


async def test_resolve_url_and_nonexistent_return_none():
    s = _FakeSession(None)
    assert await fs_service.resolve_deliverable(_FakeDeliv("https://x.com/a"), s) is None
    # unknown prefix that does NOT exist on disk → None (no phantom path)
    assert await fs_service.resolve_deliverable(_FakeDeliv("/home/agent/does-not-exist-xyz"), s) is None


async def test_resolve_legacy_absolute_path_that_exists(tmp_path):
    # legacy/backend-internal absolute deliverable paths (write-validated) resolve
    f = tmp_path / "legacy.txt"
    f.write_text("x")
    s = _FakeSession(None)
    assert await fs_service.resolve_deliverable(_FakeDeliv(str(f)), s) == str(f)
