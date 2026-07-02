"""Pin that _ssh_run passes a command-level timeout to asyncssh, and that
long-running model-load commands receive a generous per-call override.

A hanging ``docker`` invocation on the Spark must not block the whole switch
forever — the 60s default covers that.  But ``lms load`` runs in the
foreground until the model is fully in VRAM (can be >60s for large models on
cold storage), so those calls must pass ``timeout=300`` explicitly.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import runtime_manager
from app.services.host_resolver import ResolvedHost

# ADR-048: _ssh_run verlangt einen aufgelösten Host (klarer Fehler statt
# Connect gegen ""). asyncssh.connect ist gemockt — die Werte sind inert.
_TEST_HOST = ResolvedHost(ssh_host="192.0.2.10", ssh_user="test", source="settings")


@pytest.mark.asyncio
async def test_ssh_run_passes_command_timeout():
    # Fake connection context manager whose .run records its kwargs.
    fake_result = MagicMock()
    fake_result.stdout = "ok"
    fake_result.stderr = ""
    fake_result.exit_status = 0

    fake_conn = MagicMock()
    fake_conn.run = AsyncMock(return_value=fake_result)

    class _Ctx:
        async def __aenter__(self):
            return fake_conn

        async def __aexit__(self, *a):
            return False

    with patch.object(runtime_manager.asyncssh, "connect", return_value=_Ctx()):
        out, err, code = await runtime_manager._ssh_run(
            "docker ps", host=_TEST_HOST, timeout=42
        )

    assert (out, err, code) == ("ok", "", 0)
    # The command-level timeout must be forwarded to asyncssh.run.
    assert fake_conn.run.call_args.kwargs.get("timeout") == 42


@pytest.mark.asyncio
async def test_ssh_run_has_default_command_timeout():
    """Even without an explicit timeout, _ssh_run must bound the command so a
    hung docker call can't wedge the switch forever."""
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

    with patch.object(runtime_manager.asyncssh, "connect", return_value=_Ctx()):
        await runtime_manager._ssh_run("docker ps", host=_TEST_HOST)

    assert fake_conn.run.call_args.kwargs.get("timeout") is not None


# ── Fix #3: lms load must use a generous timeout, not the 60s default ────────


@pytest.mark.asyncio
async def test_lms_load_start_runtime_uses_generous_timeout():
    """start_runtime for lmstudio must call _ssh_run with timeout=300 on the
    lms load step — 60s is not enough for large models on cold storage."""
    rt = {
        "id": "lmstudio-qwen",
        "slug": "lmstudio-qwen",
        "display_name": "Qwen LM Studio",
        "runtime_type": "lmstudio",
        "lms_identifier": "qwen3-8b",
        "lms_cli_path": "~/.lmstudio/bin/lms",
        "context_length": None,
    }
    ssh = AsyncMock(return_value=("", "", 0))
    with patch.object(runtime_manager, "_ssh_run", ssh):
        result = await runtime_manager.start_runtime(rt)

    assert result["ok"] is True
    # Locate the lms load call among all _ssh_run invocations.
    load_calls = [
        c for c in ssh.call_args_list
        if "lms" in c.args[0] and "load" in c.args[0]
    ]
    assert load_calls, "expected at least one lms load call"
    for c in load_calls:
        assert c.kwargs.get("timeout") == 300, (
            f"lms load must use timeout=300, got {c.kwargs.get('timeout')}"
        )


@pytest.mark.asyncio
async def test_lms_load_by_id_uses_generous_timeout():
    """lms_load_by_id is the ad-hoc load path — same timeout requirement."""
    ssh = AsyncMock(return_value=("", "", 0))
    with patch.object(runtime_manager, "_ssh_run", ssh):
        result = await runtime_manager.lms_load_by_id("qwen3-30b", context_length=8192)

    assert result["ok"] is True
    load_call = ssh.call_args_list[0]
    assert load_call.kwargs.get("timeout") == 300, (
        f"lms_load_by_id must use timeout=300, got {load_call.kwargs.get('timeout')}"
    )
