"""Tests for the fs_roots registry — the single SSoT for browsable ~/.mc roots."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.services import fs_roots


def test_browsable_roots_include_expected_subtrees():
    keys = {r.key for r in fs_roots.browsable_roots()}
    assert {
        "deliverables",
        "workspaces",
        "vault",
        "attachments",
        "mcp-screenshots",
        "media",
        "shared-artifacts",
        "storyboard-images",
    }.issubset(keys)


def test_sensitive_roots_are_never_browsable():
    keys = {r.key for r in fs_roots.browsable_roots()}
    for sensitive in ("secrets", "agents", "logs", "backups", "browser-profiles"):
        assert sensitive not in keys, f"{sensitive} must not be browsable"
        assert sensitive in fs_roots.SENSITIVE_KEYS


def test_get_browsable_root_refuses_sensitive_and_unknown():
    with pytest.raises(KeyError):
        fs_roots.get_browsable_root("secrets")
    with pytest.raises(KeyError):
        fs_roots.get_browsable_root("agents")
    with pytest.raises(KeyError):
        fs_roots.get_browsable_root("does-not-exist")
    # a real browsable root resolves
    assert fs_roots.get_browsable_root("vault").key == "vault"


def test_host_backed_roots_resolve_under_mc_home():
    mc_home = Path(settings.home_host) / ".mc"
    vault = fs_roots.get_browsable_root("vault")
    assert vault.container_path == mc_home / "vault"
    assert vault.host_path == mc_home / "vault"
    assert vault.native_open is True


def test_named_volume_has_no_host_path():
    named = fs_roots.get_browsable_root("shared-deliverables")
    assert named.container_path == Path("/shared-deliverables")
    assert named.host_path is None
    assert named.native_open is False


# ── Soft-delete policy: DELETABLE_KEYS whitelist + typed resolver ──────────


def test_deletable_keys_exact_golden():
    """CRITICAL whitelist pin — a fat-fingered deletable=True must fail here."""
    assert fs_roots.DELETABLE_KEYS == {
        "deliverables",
        "media",
        "shared-artifacts",
        "mcp-screenshots",
        "storyboard-images",
    }


def test_blocked_and_sensitive_not_deletable():
    blocked = {"workspaces", "vault", "attachments", "shared-deliverables"}
    sensitive = {"secrets", "agents", "logs", "backups", "browser-profiles"}
    for key in blocked | sensitive:
        assert key not in fs_roots.DELETABLE_KEYS, f"{key} must not be deletable"


def test_deletable_disjoint_sensitive():
    assert not (fs_roots.SENSITIVE_KEYS & fs_roots.DELETABLE_KEYS)


def test_deletable_all_host_backed():
    for key in fs_roots.DELETABLE_KEYS:
        root = fs_roots.get_deletable_root(key)
        assert root.host_path is not None, key
        assert root.container_override is None, key


def test_get_deletable_root_exceptions():
    # unknown → RootNotFound
    with pytest.raises(fs_roots.RootNotFound):
        fs_roots.get_deletable_root("does-not-exist")
    # each sensitive → RootBlocked (403, NOT 404)
    for sensitive in ("secrets", "agents", "logs", "backups", "browser-profiles"):
        with pytest.raises(fs_roots.RootBlocked) as exc:
            fs_roots.get_deletable_root(sensitive)
        assert exc.value.reason  # a clear reason is attached
    # each blocked → RootBlocked with a reason
    for blocked in ("workspaces", "vault", "attachments", "shared-deliverables"):
        with pytest.raises(fs_roots.RootBlocked) as exc:
            fs_roots.get_deletable_root(blocked)
        assert exc.value.reason
    # a deletable root resolves to an FsRoot
    assert fs_roots.get_deletable_root("deliverables").key == "deliverables"
