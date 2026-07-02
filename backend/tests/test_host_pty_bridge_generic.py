"""Tests fuer den generalisierten host-pty-bridge: query-param parsing + Validierung."""
import importlib.util
import os
import sys
from pathlib import Path

import pytest


# Direkt aus Datei laden (nicht via Package, weil docker/host-pty-bridge kein
# Python-Package ist). Das gibt uns Zugriff auf die Validierungs-Helper.
def _load_bridge_module():
    repo_root = Path(__file__).resolve().parents[2]
    server_path = repo_root / "docker" / "host-pty-bridge" / "server.py"
    spec = importlib.util.spec_from_file_location("host_pty_bridge_server", server_path)
    module = importlib.util.module_from_spec(spec)
    # websockets ist im Test-Env via backend deps verfuegbar
    spec.loader.exec_module(module)
    return module


bridge = _load_bridge_module()


def test_default_session_when_no_params():
    """Ohne query-params -> Boss-Default (boss-host:0 + bekannter Socket)."""
    session_name, socket_path = bridge.resolve_target(query_string="")
    assert session_name == "boss-host:0"
    assert socket_path == bridge.DEFAULT_SOCKET


def test_custom_session_via_query_param():
    """?session=hermes-worker&socket=/tmp/tmux-501/default -> beides angenommen."""
    session_name, socket_path = bridge.resolve_target(
        query_string="session=hermes-worker&socket=/tmp/tmux-501/default"
    )
    assert session_name == "hermes-worker"
    assert socket_path == "/tmp/tmux-501/default"


def test_session_only_uses_default_socket():
    """Nur ?session= ohne socket -> Default-Socket."""
    session_name, socket_path = bridge.resolve_target(query_string="session=hermes-worker")
    assert session_name == "hermes-worker"
    assert socket_path == bridge.DEFAULT_SOCKET


def test_invalid_session_name_rejected():
    """?session=../../etc/passwd -> ValueError (400)."""
    with pytest.raises(ValueError, match="session"):
        bridge.resolve_target(query_string="session=../../etc/passwd")


def test_session_name_shell_injection_rejected():
    """Session-Name mit Shell-Metachars -> ValueError."""
    with pytest.raises(ValueError, match="session"):
        bridge.resolve_target(query_string="session=boss;rm+-rf+/")


def test_socket_path_traversal_rejected():
    """?socket=/etc/passwd -> ValueError (400)."""
    with pytest.raises(ValueError, match="socket"):
        bridge.resolve_target(
            query_string="session=hermes-worker&socket=/etc/passwd"
        )


def test_socket_path_outside_tmux_dir_rejected():
    """Socket-Pfad ausserhalb /tmp/tmux-* -> ValueError."""
    with pytest.raises(ValueError, match="socket"):
        bridge.resolve_target(
            query_string="session=hermes-worker&socket=/Users/testuser/.evil/sock"
        )


def test_socket_under_tmpdir_tmux_accepted(tmp_path, monkeypatch):
    """$TMPDIR/tmux-* Pfad wird auch akzeptiert (macOS-Pattern)."""
    # Simuliere TMPDIR=/var/folders/xx
    fake_tmpdir = "/var/folders/aa/bb"
    monkeypatch.setenv("TMPDIR", fake_tmpdir)
    socket = f"{fake_tmpdir}/tmux-501/default"
    session_name, socket_path = bridge.resolve_target(
        query_string=f"session=ok&socket={socket}"
    )
    assert socket_path == socket


def test_session_with_window_index_accepted():
    """boss-host:0 (Session:Window) ist gueltig."""
    session_name, _ = bridge.resolve_target(query_string="session=boss-host:0")
    assert session_name == "boss-host:0"
