"""Tests for Hermes routing in cli_terminal.py.

Verifies:
  - Hermes slug routes to hermes-worker tmux session via query params
  - Boss slug stays unchanged (no query string -> bridge uses default)
  - Unknown host-runtime slug -> 404 (no mount)
"""
import os

import pytest

from app.routers import cli_terminal as cli_mod


def test_hermes_slug_routes_to_hermes_worker():
    """slug='hermes' -> upstream URL contains session=hermes-worker + socket."""
    url = cli_mod._build_host_upstream_url("hermes")
    assert url is not None
    assert "session=hermes-worker" in url
    assert "socket=" in url
    # Socket must be the user default, not Boss custom
    assert "boss-host" not in url


def test_boss_slug_unchanged():
    """slug='boss' -> upstream URL without query params (bridge uses default)."""
    url = cli_mod._build_host_upstream_url("boss")
    assert url is not None
    # Backward-compat: no ?session=, so bridge default-attaches to boss-host:0
    assert "?" not in url or "session=" not in url


def test_unknown_host_slug_returns_none():
    """Unknown host-runtime slug -> None (caller closes WS with 4004)."""
    url = cli_mod._build_host_upstream_url("phantom")
    assert url is None


def test_hermes_plist_entry_present():
    """_HOST_AGENT_PLISTS has a hermes entry for launchctl lifecycle."""
    plists = cli_mod._HOST_AGENT_PLISTS
    assert "hermes" in plists
    hermes_plists = plists["hermes"]
    # Entry points to com.mc.hermes-bridge.plist
    joined = " ".join(hermes_plists) if isinstance(hermes_plists, list) else str(hermes_plists)
    assert "com.mc.hermes-bridge" in joined


def test_hermes_target_uses_user_default_socket():
    """Hermes target points to the user-default tmux socket (not Boss custom)."""
    targets = cli_mod._HOST_AGENT_TMUX_TARGETS
    assert "hermes" in targets
    target = targets["hermes"]
    assert target["session"] == "hermes-worker"
    # Socket must be /tmp/tmux-* or $TMPDIR/tmux-*
    socket = target["socket"]
    assert socket.startswith("/tmp/tmux-") or "/tmux-" in socket


def test_boss_target_preserves_legacy_default():
    """Boss entry (if present) signals: no query params needed."""
    # Boss has NO _HOST_AGENT_TMUX_TARGETS override, because the bridge default
    # already points to Boss. Test ensures no override sneaks in.
    targets = cli_mod._HOST_AGENT_TMUX_TARGETS
    # Either 'boss' is missing entirely, or it explicitly points to the Boss default
    if "boss" in targets:
        assert targets["boss"].get("session") in (None, "boss-host:0")
