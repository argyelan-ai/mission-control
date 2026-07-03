"""Tests for the generalized host-pty-bridge: query-param parsing + validation."""
import importlib.util
import os
import sys
from pathlib import Path

import pytest


# Load directly from the file (not via package, because docker/host-pty-bridge
# is not a Python package). This gives us access to the validation helpers.
def _load_bridge_module():
    repo_root = Path(__file__).resolve().parents[2]
    server_path = repo_root / "docker" / "host-pty-bridge" / "server.py"
    spec = importlib.util.spec_from_file_location("host_pty_bridge_server", server_path)
    module = importlib.util.module_from_spec(spec)
    # websockets is available in the test env via backend deps
    spec.loader.exec_module(module)
    return module


bridge = _load_bridge_module()


def test_default_session_when_no_params():
    """Without query params -> boss default (boss-host:0 + known socket)."""
    session_name, socket_path = bridge.resolve_target(query_string="")
    assert session_name == "boss-host:0"
    assert socket_path == bridge.DEFAULT_SOCKET


def test_custom_session_via_query_param():
    """?session=hermes-worker&socket=/tmp/tmux-501/default -> both accepted."""
    session_name, socket_path = bridge.resolve_target(
        query_string="session=hermes-worker&socket=/tmp/tmux-501/default"
    )
    assert session_name == "hermes-worker"
    assert socket_path == "/tmp/tmux-501/default"


def test_session_only_uses_default_socket():
    """Only ?session= without socket -> default socket."""
    session_name, socket_path = bridge.resolve_target(query_string="session=hermes-worker")
    assert session_name == "hermes-worker"
    assert socket_path == bridge.DEFAULT_SOCKET


def test_invalid_session_name_rejected():
    """?session=../../etc/passwd -> ValueError (400)."""
    with pytest.raises(ValueError, match="session"):
        bridge.resolve_target(query_string="session=../../etc/passwd")


def test_session_name_shell_injection_rejected():
    """Session name with shell metacharacters -> ValueError."""
    with pytest.raises(ValueError, match="session"):
        bridge.resolve_target(query_string="session=boss;rm+-rf+/")


def test_socket_path_traversal_rejected():
    """?socket=/etc/passwd -> ValueError (400)."""
    with pytest.raises(ValueError, match="socket"):
        bridge.resolve_target(
            query_string="session=hermes-worker&socket=/etc/passwd"
        )


def test_socket_path_outside_tmux_dir_rejected():
    """Socket path outside /tmp/tmux-* -> ValueError."""
    with pytest.raises(ValueError, match="socket"):
        bridge.resolve_target(
            query_string="session=hermes-worker&socket=/Users/testuser/.evil/sock"
        )


def test_socket_under_tmpdir_tmux_accepted(tmp_path, monkeypatch):
    """$TMPDIR/tmux-* path is also accepted (macOS pattern)."""
    # Simulate TMPDIR=/var/folders/xx
    fake_tmpdir = "/var/folders/aa/bb"
    monkeypatch.setenv("TMPDIR", fake_tmpdir)
    socket = f"{fake_tmpdir}/tmux-501/default"
    session_name, socket_path = bridge.resolve_target(
        query_string=f"session=ok&socket={socket}"
    )
    assert socket_path == socket


def test_session_with_window_index_accepted():
    """boss-host:0 (session:window) is valid."""
    session_name, _ = bridge.resolve_target(query_string="session=boss-host:0")
    assert session_name == "boss-host:0"
