"""Engine Control v0 (ADR-057): autostart flag file over SSH.

SSH is fully mocked (asyncssh.connect never touches the network) — mirrors
the pattern in test_ssh_run_timeout.py.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import asyncssh
import pytest

from app.services import runtime_autostart
from app.services.host_resolver import ResolvedHost

_TEST_HOST = ResolvedHost(ssh_host="192.0.2.10", ssh_user="test", source="settings")
_FLAG_PATH = "/home/testuser/scripts/vllm-autostart.enabled"


def _ssh_ctx(exit_code: int):
    fake_result = MagicMock()
    fake_result.stdout = ""
    fake_result.stderr = ""
    fake_result.exit_status = exit_code
    fake_conn = MagicMock()
    fake_conn.run = AsyncMock(return_value=fake_result)

    class _Ctx:
        async def __aenter__(self):
            return fake_conn

        async def __aexit__(self, *a):
            return False

    return _Ctx(), fake_conn


@pytest.mark.asyncio
async def test_get_autostart_status_enabled():
    ctx, conn = _ssh_ctx(0)  # test -f exits 0 → file exists
    with patch("app.services.runtime_manager.asyncssh.connect", return_value=ctx):
        status = await runtime_autostart.get_autostart_status(_FLAG_PATH, host=_TEST_HOST)
    assert status.enabled is True
    assert status.reachable is True
    assert "test -f" in conn.run.call_args.args[0]
    assert _FLAG_PATH in conn.run.call_args.args[0]


@pytest.mark.asyncio
async def test_get_autostart_status_disabled():
    ctx, _ = _ssh_ctx(1)  # test -f exits 1 → file missing
    with patch("app.services.runtime_manager.asyncssh.connect", return_value=ctx):
        status = await runtime_autostart.get_autostart_status(_FLAG_PATH, host=_TEST_HOST)
    assert status.enabled is False
    assert status.reachable is True


@pytest.mark.asyncio
async def test_get_autostart_status_unreachable_host_returns_unknown():
    """A dead SSH connection must surface as enabled=None, reachable=False —
    never raise out of a status probe (the /runtimes page must not 500)."""
    with patch(
        "app.services.runtime_manager.asyncssh.connect",
        side_effect=asyncssh.Error(1, "connection refused"),
    ):
        status = await runtime_autostart.get_autostart_status(_FLAG_PATH, host=_TEST_HOST)
    assert status.enabled is None
    assert status.reachable is False


@pytest.mark.asyncio
async def test_set_autostart_true_touches_then_verifies():
    """set_autostart(True) must touch the file, then read it back — two SSH
    round-trips, both against the same path."""
    fake_result = MagicMock()
    fake_result.stdout = ""
    fake_result.stderr = ""
    fake_result.exit_status = 0  # both touch and the verify test -f succeed
    fake_conn = MagicMock()
    fake_conn.run = AsyncMock(return_value=fake_result)

    class _Ctx:
        async def __aenter__(self):
            return fake_conn

        async def __aexit__(self, *a):
            return False

    with patch("app.services.runtime_manager.asyncssh.connect", return_value=_Ctx()):
        status = await runtime_autostart.set_autostart(_FLAG_PATH, True, host=_TEST_HOST)

    assert status.enabled is True
    assert status.reachable is True
    commands = [c.args[0] for c in fake_conn.run.call_args_list]
    assert any(cmd.startswith("touch ") for cmd in commands)
    assert any(cmd.startswith("test -f ") for cmd in commands)


@pytest.mark.asyncio
async def test_set_autostart_false_removes_then_verifies():
    fake_result = MagicMock()
    fake_result.stdout = ""
    fake_result.stderr = ""
    fake_result.exit_status = 1  # rm succeeded, verify test -f now fails (file gone)
    fake_conn = MagicMock()
    fake_conn.run = AsyncMock(return_value=fake_result)

    class _Ctx:
        async def __aenter__(self):
            return fake_conn

        async def __aexit__(self, *a):
            return False

    with patch("app.services.runtime_manager.asyncssh.connect", return_value=_Ctx()):
        status = await runtime_autostart.set_autostart(_FLAG_PATH, False, host=_TEST_HOST)

    assert status.enabled is False
    commands = [c.args[0] for c in fake_conn.run.call_args_list]
    assert any(cmd.startswith("rm -f ") for cmd in commands)


@pytest.mark.asyncio
async def test_set_autostart_raises_on_unreachable_host():
    """The toggle endpoint needs to distinguish 'host down' from 'flag now
    false' — set_autostart must raise, not silently report enabled=None."""
    with patch(
        "app.services.runtime_manager.asyncssh.connect",
        side_effect=asyncssh.Error(1, "connection refused"),
    ):
        with pytest.raises(runtime_autostart.AutostartHostUnreachable):
            await runtime_autostart.set_autostart(_FLAG_PATH, True, host=_TEST_HOST)


@pytest.mark.asyncio
async def test_flag_path_is_shell_quoted():
    """A flag_path containing shell metacharacters must not break out of the
    quoted argument — defense in depth even though the router regex already
    restricts the charset."""
    fake_result = MagicMock()
    fake_result.stdout = ""
    fake_result.stderr = ""
    fake_result.exit_status = 0
    fake_conn = MagicMock()
    fake_conn.run = AsyncMock(return_value=fake_result)

    class _Ctx:
        async def __aenter__(self):
            return fake_conn

        async def __aexit__(self, *a):
            return False

    dangerous_path = "/tmp/x; rm -rf /"
    with patch("app.services.runtime_manager.asyncssh.connect", return_value=_Ctx()):
        await runtime_autostart.get_autostart_status(dangerous_path, host=_TEST_HOST)

    cmd = fake_conn.run.call_args.args[0]
    # shlex.quote wraps the whole path in single quotes — the semicolon never
    # becomes a second shell command.
    assert cmd == "test -f '/tmp/x; rm -rf /'"
