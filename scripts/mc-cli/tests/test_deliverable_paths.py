"""Tests for `mc deliverable` host-vs-container root selection.

Background (2026-07-11, grok's first live task): host-harness agents
(hermes, grok — launchd on the Mac, not Docker) hit an unwritable root
because `_cmd_deliverable` hardcoded `/deliverables/<task_id>/` as the
deliverables root. The host root filesystem is read-only there; the
backend already accepts `~/.mc/deliverables/<task_id>/` (and its expanded
`$HOME_HOST/...` form) as a valid prefix — see
`backend/app/services/deliverable_paths.py::accepted_path_prefixes`, and
docker-compose mounts `${HOME}/.mc` into the backend at the same path, so
host-path deliverables are fully servable.

These tests monkeypatch `os.path.isdir` (container-detection probe on
`/deliverables`) and `os.path.expanduser` (home resolution) to force each
branch deterministically, without touching the real filesystem.
"""
import os
import sys
from unittest.mock import MagicMock

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from mc_cli import commands  # noqa: E402
from mc_cli.errors import UsageError  # noqa: E402


BOARD_ID = "11111111-1111-1111-1111-111111111111"
TASK_ID = "22222222-2222-2222-2222-222222222222"
FAKE_HOME = "/Users/fakehost"


class _Args:
    def __init__(self, path=None, type_="file", title="t", description=None,
                 content=None, reusable=False):
        self.path = path
        self.type = type_
        self.title = title
        self.description = description
        self.content = content
        self.reusable = reusable


def _mock_cfg():
    cfg = MagicMock()
    cfg.require_task_context.return_value = (BOARD_ID, TASK_ID)
    return cfg


def _mock_client(response=None):
    client = MagicMock()
    client.request.return_value = response or {"id": "deliverable-1"}
    return client


def _force_container(monkeypatch):
    """Simulate running inside the Docker agent container: /deliverables exists."""
    monkeypatch.setattr(os.path, "isdir", lambda p: p == "/deliverables")


def _force_host(monkeypatch):
    """Simulate running on the macOS host: /deliverables does not exist."""
    monkeypatch.setattr(os.path, "isdir", lambda p: False)
    monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", FAKE_HOME, 1))
    # Auto-created directory on host — avoid touching the real filesystem.
    monkeypatch.setattr(os, "makedirs", lambda *a, **kw: None)


# ── Container (default) behavior unchanged ─────────────────────────────────


def test_container_relative_path_rewrites_to_deliverables_root(monkeypatch):
    _force_container(monkeypatch)
    monkeypatch.setattr(os.path, "isfile",
                        lambda p: p == f"/deliverables/{TASK_ID}/report.md")
    client = _mock_client()
    cfg = _mock_cfg()
    commands._cmd_deliverable(_Args(path="report.md"), client, cfg)
    body = client.request.call_args.kwargs["body"]
    assert body["path"] == f"/deliverables/{TASK_ID}/report.md"


def test_container_accepted_absolute_prefix_passes_through(monkeypatch):
    _force_container(monkeypatch)
    client = _mock_client()
    cfg = _mock_cfg()
    given = f"/deliverables/{TASK_ID}/foo.pdf"
    monkeypatch.setattr(os.path, "isfile", lambda p: p == given)
    commands._cmd_deliverable(_Args(path=given), client, cfg)
    body = client.request.call_args.kwargs["body"]
    assert body["path"] == given


def test_container_rejected_absolute_path_raises_with_deliverables_hint(monkeypatch):
    _force_container(monkeypatch)
    monkeypatch.setattr(os.path, "isfile", lambda p: False)
    client = _mock_client()
    cfg = _mock_cfg()
    with pytest.raises(UsageError) as exc:
        commands._cmd_deliverable(_Args(path="/workspace/foo.txt"), client, cfg)
    assert f"/deliverables/{TASK_ID}/" in str(exc.value)


# ── Host runtime: root rewritten to ~/.mc/deliverables/<task_id> ───────────


def test_host_relative_path_rewrites_to_home_mc_deliverables(monkeypatch):
    _force_host(monkeypatch)
    monkeypatch.setattr(
        os.path, "isfile",
        lambda p: p == f"{FAKE_HOME}/.mc/deliverables/{TASK_ID}/report.md")
    client = _mock_client()
    cfg = _mock_cfg()
    commands._cmd_deliverable(_Args(path="report.md"), client, cfg)
    body = client.request.call_args.kwargs["body"]
    assert body["path"] == f"{FAKE_HOME}/.mc/deliverables/{TASK_ID}/report.md"


def test_host_accepted_absolute_prefix_passes_through(monkeypatch):
    _force_host(monkeypatch)
    client = _mock_client()
    cfg = _mock_cfg()
    given = f"{FAKE_HOME}/.mc/deliverables/{TASK_ID}/foo.pdf"
    monkeypatch.setattr(os.path, "isfile", lambda p: p == given)
    commands._cmd_deliverable(_Args(path=given), client, cfg)
    body = client.request.call_args.kwargs["body"]
    assert body["path"] == given


def test_host_rejected_absolute_path_raises_with_home_mc_hint(monkeypatch):
    _force_host(monkeypatch)
    monkeypatch.setattr(os.path, "isfile", lambda p: False)
    client = _mock_client()
    cfg = _mock_cfg()
    with pytest.raises(UsageError) as exc:
        commands._cmd_deliverable(_Args(path="/deliverables/other/foo.txt"), client, cfg)
    msg = str(exc.value)
    assert f"{FAKE_HOME}/.mc/deliverables/{TASK_ID}/" in msg
    # Auto-copy suggestion must target the host root, not the bare
    # container-only `/deliverables/<task_id>/` path.
    assert f"'{FAKE_HOME}/.mc/deliverables/{TASK_ID}/foo.txt'" in msg


def test_host_legacy_marker_path_still_extracts_filename(monkeypatch):
    _force_host(monkeypatch)
    client = _mock_client()
    cfg = _mock_cfg()
    monkeypatch.setattr(
        os.path, "isfile",
        lambda p: p == f"{FAKE_HOME}/.mc/deliverables/{TASK_ID}/legacy.txt")
    commands._cmd_deliverable(
        _Args(path=f".mc-deliverables/{TASK_ID}/legacy.txt"), client, cfg
    )
    body = client.request.call_args.kwargs["body"]
    assert body["path"] == f"{FAKE_HOME}/.mc/deliverables/{TASK_ID}/legacy.txt"


def test_host_url_path_passes_through_unchanged(monkeypatch):
    _force_host(monkeypatch)
    client = _mock_client()
    cfg = _mock_cfg()
    commands._cmd_deliverable(_Args(path="https://example.com/x.png"), client, cfg)
    body = client.request.call_args.kwargs["body"]
    assert body["path"] == "https://example.com/x.png"


def test_host_content_only_no_path(monkeypatch):
    _force_host(monkeypatch)
    client = _mock_client()
    cfg = _mock_cfg()
    commands._cmd_deliverable(
        _Args(path=None, type_="document", content="inline text"), client, cfg
    )
    body = client.request.call_args.kwargs["body"]
    assert body["path"] is None
    assert body["content"] == "inline text"


# ── Phantom guard (Horror-Forest incident 2026-07-12) ──────────────────────
# A path inside the agent's own deliverables zone used to be registered
# WITHOUT checking the file exists — the agent passed the TARGET path
# without copying anything there, the DB row pointed into the void and the
# UI 404'd. Registration now requires the file to exist.


def test_container_phantom_path_in_own_zone_raises(monkeypatch):
    _force_container(monkeypatch)
    monkeypatch.setattr(os.path, "isfile", lambda p: False)
    client = _mock_client()
    cfg = _mock_cfg()
    with pytest.raises(UsageError) as exc:
        commands._cmd_deliverable(
            _Args(path=f"/deliverables/{TASK_ID}/index.html"), client, cfg
        )
    assert "existiert nicht" in str(exc.value)
    client.request.assert_not_called()


def test_host_phantom_path_in_own_zone_raises(monkeypatch):
    _force_host(monkeypatch)
    monkeypatch.setattr(os.path, "isfile", lambda p: False)
    client = _mock_client()
    cfg = _mock_cfg()
    with pytest.raises(UsageError) as exc:
        commands._cmd_deliverable(
            _Args(path=f"{FAKE_HOME}/.mc/deliverables/{TASK_ID}/index.html"),
            client, cfg,
        )
    assert "existiert nicht" in str(exc.value)
    client.request.assert_not_called()


def test_sidecar_prefix_is_not_existence_checked(monkeypatch):
    """On the host /shared-deliverables is not mounted — an existence check
    there would be a false negative. Sidecar paths stay pass-through."""
    _force_host(monkeypatch)
    monkeypatch.setattr(os.path, "isfile", lambda p: False)
    client = _mock_client()
    cfg = _mock_cfg()
    given = f"/shared-deliverables/{TASK_ID}/shot.png"
    commands._cmd_deliverable(_Args(path=given), client, cfg)
    body = client.request.call_args.kwargs["body"]
    assert body["path"] == given


def test_relative_workspace_file_is_auto_copied(monkeypatch):
    """Preferred flow per --path help: relative workspace path — the CLI must
    COPY the file into the deliverables zone, not just rewrite the string."""
    import shutil
    _force_container(monkeypatch)
    dest = f"/deliverables/{TASK_ID}/index.html"
    # index.html exists in the CWD, not yet at the destination
    monkeypatch.setattr(os.path, "isfile", lambda p: p == "index.html")
    copies = []
    monkeypatch.setattr(shutil, "copy2", lambda src, dst: copies.append((src, dst)))
    monkeypatch.setattr(os, "makedirs", lambda *a, **kw: None)
    client = _mock_client()
    cfg = _mock_cfg()
    with pytest.raises(UsageError):
        # copy2 is mocked away, so the file still does not exist at dest —
        # the phantom guard must catch that too (belt and braces).
        commands._cmd_deliverable(_Args(path="index.html"), client, cfg)
    assert copies == [("index.html", dest)]


# ── Zone-prefix doubling (Task 3a17837f, 2026-07-13) ────────────────────────
# An agent registered a workspace-relative path that already included the
# canonical zone prefix, e.g. `deliverables/<task_id>/index.html`. The old
# code blindly joined it onto `deliverables_root`, doubling the prefix to
# `/deliverables/<task_id>/deliverables/<task_id>/index.html` — auto-copy
# landed there, the DB row pointed at the doubled path, and the UI 404'd.
# The zone prefix must be stripped before the join, same as the legacy
# `.mc-deliverables/<task_id>/` marker already is.


def test_container_relative_path_with_zone_prefix_not_doubled(monkeypatch):
    import shutil
    _force_container(monkeypatch)
    given = f"deliverables/{TASK_ID}/index.html"
    dest = f"/deliverables/{TASK_ID}/index.html"
    # Source file lives at the given workspace-relative path (with prefix);
    # nothing exists at dest yet.
    monkeypatch.setattr(os.path, "isfile", lambda p: p == given)
    copies = []
    monkeypatch.setattr(shutil, "copy2", lambda src, dst: copies.append((src, dst)))
    monkeypatch.setattr(os, "makedirs", lambda *a, **kw: None)
    client = _mock_client()
    cfg = _mock_cfg()
    with pytest.raises(UsageError):
        # copy2 mocked away → dest still doesn't exist → phantom guard fires.
        # What we're asserting here is that dest/copy target is NOT doubled.
        commands._cmd_deliverable(_Args(path=given), client, cfg)
    assert copies == [(given, dest)]


def test_host_relative_path_with_dotmc_zone_prefix_not_doubled(monkeypatch):
    import shutil
    _force_host(monkeypatch)
    given = f".mc/deliverables/{TASK_ID}/index.html"
    dest = f"{FAKE_HOME}/.mc/deliverables/{TASK_ID}/index.html"
    monkeypatch.setattr(os.path, "isfile", lambda p: p == given)
    copies = []
    monkeypatch.setattr(shutil, "copy2", lambda src, dst: copies.append((src, dst)))
    client = _mock_client()
    cfg = _mock_cfg()
    with pytest.raises(UsageError):
        commands._cmd_deliverable(_Args(path=given), client, cfg)
    assert copies == [(given, dest)]


def test_host_relative_path_with_tilde_zone_prefix_not_doubled(monkeypatch):
    import shutil
    _force_host(monkeypatch)
    given = f"~/.mc/deliverables/{TASK_ID}/index.html"
    dest = f"{FAKE_HOME}/.mc/deliverables/{TASK_ID}/index.html"
    monkeypatch.setattr(os.path, "isfile", lambda p: p == given)
    copies = []
    monkeypatch.setattr(shutil, "copy2", lambda src, dst: copies.append((src, dst)))
    client = _mock_client()
    cfg = _mock_cfg()
    with pytest.raises(UsageError):
        commands._cmd_deliverable(_Args(path=given), client, cfg)
    assert copies == [(given, dest)]
