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
