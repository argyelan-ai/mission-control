"""Boot step for ~/.mc (app.services.mc_home_preflight) + the /health detail.

Background: on a Linux host whose login user is not uid 1000 the backend
(container uid 1000) could not write the ~/.mc bind mount; migration 0112
crashed on ``mkdir ~/.mc/vault`` and the stack never came up, without saying
why. The directories are now created by this boot step, and a permission
problem is reported in one log line and in /health instead of crashing.
"""

from __future__ import annotations

import inspect
import logging
import os
import stat
from pathlib import Path

import pytest

from app.services import mc_home_preflight as pf

needs_non_root = pytest.mark.skipif(
    os.getuid() == 0, reason="root ignores directory permissions"
)


@pytest.fixture(autouse=True)
def _reset():
    pf.reset_for_tests()
    yield
    pf.reset_for_tests()


def test_fresh_home_gets_every_core_dir(tmp_path):
    root = tmp_path / ".mc"  # does not exist yet — like a first start

    status = pf.ensure_mc_home(root)

    assert status.ok, status.as_dict()
    for rel, _mode in pf.CORE_DIRS:
        assert (root / rel).is_dir(), rel
    assert "~/.mc/vault" in status.created
    # Private trees are created closed (umask can only narrow, never widen).
    assert stat.S_IMODE((root / "secrets").stat().st_mode) & 0o077 == 0
    assert stat.S_IMODE((root / "ssh").stat().st_mode) & 0o077 == 0
    # The write probe cleans up after itself.
    assert not [p for p in root.iterdir() if p.name.startswith(".mc-write-probe-")]


def test_existing_dirs_are_left_alone(tmp_path):
    root = tmp_path / ".mc"
    (root / "vault").mkdir(parents=True)
    marker = root / "vault" / "note.md"
    marker.write_text("keep me")

    status = pf.ensure_mc_home(root)

    assert status.ok
    assert "~/.mc/vault" not in status.created
    assert marker.read_text() == "keep me"


@needs_non_root
def test_unwritable_home_is_reported_not_raised(tmp_path, caplog):
    root = tmp_path / ".mc"
    root.mkdir()
    root.chmod(0o555)  # what uid 1000 sees on a ~/.mc owned by uid 1001
    try:
        with caplog.at_level(logging.WARNING, logger="mc.startup"):
            status = pf.ensure_mc_home(root)
    finally:
        root.chmod(0o755)

    assert not status.ok
    reasons = {(p.path, p.reason) for p in status.problems}
    assert ("~/.mc", "not_writable") in reasons
    assert ("~/.mc/vault", "create_failed") in reasons
    # ONE clear error line that says what to do.
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "MC HOME NOT WRITABLE" in errors[0].getMessage()
    assert "docs/handbook/operations/platforms.md#host-user-id-linux-nas" in errors[0].getMessage()


def test_uid_mismatch_hint_names_puid_and_pgid(tmp_path, monkeypatch):
    root = tmp_path / ".mc"
    root.mkdir()
    # Simulate the measured case: ~/.mc belongs to uid 1001, process is 1000.
    monkeypatch.setattr(pf.os, "getuid", lambda: 1000)
    monkeypatch.setattr(pf.os, "getgid", lambda: 1000)
    monkeypatch.setattr(pf, "_writable", lambda p: False)
    monkeypatch.setattr(pf, "_owner", lambda p: (1001, 1002))
    monkeypatch.setattr(pf, "_probe_write", lambda p: "PermissionError: denied")

    def _deny(self, *a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "mkdir", _deny)

    status = pf.check_mc_home(root)

    assert not status.ok
    assert "PUID=1001" in status.hint
    assert "PGID=1002" in status.hint
    assert "uid 1000" in status.hint
    detail = status.as_dict()
    assert detail["process_uid"] == 1000
    # No absolute host path leaks into the (unauthenticated) /health answer.
    assert str(tmp_path) not in repr(detail)


def test_root_owned_subdir_gets_the_chown_hint(tmp_path, monkeypatch):
    root = tmp_path / ".mc"
    (root / "deliverables").mkdir(parents=True)
    monkeypatch.setattr(pf.os, "getuid", lambda: 1001)
    real_writable = pf._writable
    monkeypatch.setattr(
        pf, "_writable", lambda p: False if p.name == "deliverables" else real_writable(p)
    )
    monkeypatch.setattr(pf, "_owner", lambda p: (0, 0))

    status = pf.check_mc_home(root)

    assert [(p.path, p.reason) for p in status.problems] == [
        ("~/.mc/deliverables", "not_writable")
    ]
    assert "sudo chown -R $(id -u):$(id -g) ~/.mc" in status.hint


def test_missing_mount_says_so(tmp_path):
    root = tmp_path / "no-such-home" / ".mc"

    status = pf.ensure_mc_home(root)

    assert not status.ok
    assert status.problems[0].reason == "missing"
    assert "not mounted" in status.hint
    assert not root.exists()


def test_health_detail_none_when_fine_or_unchecked(tmp_path):
    assert pf.health_detail() is None  # never checked (tests, compose-free use)
    pf.ensure_mc_home(tmp_path / ".mc")
    assert pf.health_detail() is None


@needs_non_root
def test_health_detail_clears_after_the_fix(tmp_path, monkeypatch):
    root = tmp_path / ".mc"
    root.mkdir()
    root.chmod(0o555)
    try:
        pf.ensure_mc_home(root)
        detail = pf.health_detail()
        assert detail is not None and detail["ok"] is False
    finally:
        root.chmod(0o755)  # the operator fixes ownership …
    monkeypatch.setattr(pf, "RECHECK_SECONDS", 0.0)
    assert pf.health_detail() is None  # … and /health is fine without a restart
    assert (root / "vault").is_dir()


async def test_health_endpoint_reports_degraded(client, tmp_path, monkeypatch):
    root = tmp_path / ".mc"
    root.mkdir()
    monkeypatch.setattr(pf, "_writable", lambda p: False)
    monkeypatch.setattr(pf, "_owner", lambda p: (1001, 1001))
    monkeypatch.setattr(pf.os, "getuid", lambda: 1000)
    pf.check_mc_home(root, create=False)
    monkeypatch.setattr(pf, "RECHECK_SECONDS", 3600.0)

    response = await client.get("/health")

    assert response.status_code == 200  # stack keeps starting, reason is readable
    body = response.json()
    assert body["status"] == "degraded"
    assert body["mc_home"]["ok"] is False
    assert "PUID=1001" in body["mc_home"]["hint"]


def test_boot_step_runs_in_both_processes():
    """prepare_process() is the shared boot path of API and worker."""
    from app.background import prepare_process

    src = inspect.getsource(prepare_process)
    assert "ensure_mc_home()" in src
    # before the seeds, so the reason is logged before anything else fails
    assert src.index("ensure_mc_home()") < src.index("await _seed_templates()")
