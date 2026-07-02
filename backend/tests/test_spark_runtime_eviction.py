"""Tests for robust Spark runtime eviction + start-verification.

These pin the P0-P4 hardening of the recipe-switch flow:

  P0 — eviction stops ALL running Spark model containers before a fresh start,
       not just the (often-empty) ``container_name``. Two layers:
         (a) by label ``mc.runtime.slug=<slug>``
         (b) a sweep of every ``sparkrun_*_solo`` container (catches CLI- or
             externally-started models MC never labelled).
  P1 — after the stop, poll until no Spark model container is left running
       (bounded timeout, then an honest error) so the new launch doesn't race
       against a still-occupied GPU/RAM.
  P2 — after the nohup launch, poll for a container carrying the slug label;
       if none appears, return ok=False with the launch-log path.
  P4 — error messages carry the ``~/.cache/mc/runtime-launch-<slug>.log`` path.

All SSH is mocked — nothing touches the real Spark (192.0.2.10).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, call, patch

import pytest

from app.services import runtime_manager


SPARK_RT = {
    "id": "qwen-general",
    "slug": "qwen-general",
    "display_name": "Spark Qwen vLLM",
    "runtime_type": "vllm_docker",
    "endpoint": "http://192.0.2.10:8000/v1",
    "container_name": None,  # cleared after every switch — the RC-1 bug surface
    "launch_command": (
        "uvx sparkrun run @official/qwen3.6-27b-fp8-mtp-vllm "
        "--solo --no-rm --ensure --no-follow --label mc.runtime.slug=qwen-general"
    ),
}


# ── P0: eviction stops label + solo-sweep containers ─────────────────────────


@pytest.mark.asyncio
async def test_evict_stops_label_and_solo_containers():
    """Eviction must issue BOTH a label-filtered stop AND a solo-sweep, and
    must NOT depend on container_name (which is None after a switch)."""
    # First call: label+sweep stop command (returns the ids it stopped).
    # Second call onward: the readiness poll reports nothing left running.
    ssh = AsyncMock(side_effect=[
        ("sparkrun_oldid_solo", "", 0),   # stop command output
        ("", "", 0),                       # poll: no solo containers left
    ])
    with patch.object(runtime_manager, "_ssh_run", ssh), \
         patch.object(runtime_manager, "_evict_poll_interval", 0):
        result = await runtime_manager.evict_spark_runtime_containers("qwen-general")

    assert result["ok"] is True
    # The stop command must reference BOTH the label and the solo glob.
    stop_cmd = ssh.call_args_list[0].args[0]
    assert "mc.runtime.slug=qwen-general" in stop_cmd
    assert "sparkrun_" in stop_cmd and "_solo" in stop_cmd
    # `docker stop` must be fed by `xargs -r` so an empty match list runs
    # nothing — the RC-1 fix. A bare `docker stop` with a possibly-empty arg
    # (the old bug) must NOT appear.
    assert "xargs -r docker stop" in stop_cmd
    assert "docker stop \n" not in stop_cmd
    assert "docker stop ''" not in stop_cmd
    assert "; docker stop" not in stop_cmd  # never an unguarded stop


@pytest.mark.asyncio
async def test_evict_slug_is_shell_quoted():
    """A slug must be shell-safe — eviction quotes it to prevent injection."""
    ssh = AsyncMock(return_value=("", "", 0))
    with patch.object(runtime_manager, "_ssh_run", ssh):
        # An attacker-ish slug should not break out of the docker filter.
        await runtime_manager.evict_spark_runtime_containers("evil; rm -rf /")
    stop_cmd = ssh.call_args_list[0].args[0]
    # The dangerous payload must be quoted, not interpolated raw as a command.
    assert "; rm -rf /" not in stop_cmd.replace("'evil; rm -rf /'", "")


# ── P1: poll until free, honest timeout ──────────────────────────────────────


@pytest.mark.asyncio
async def test_evict_waits_until_no_solo_container_running():
    """Eviction polls until the solo-container list is empty before returning."""
    ssh = AsyncMock(side_effect=[
        ("sparkrun_a_solo", "", 0),        # stop command
        ("sparkrun_a_solo", "", 0),        # poll #1: still running
        ("", "", 0),                        # poll #2: gone
    ])
    with patch.object(runtime_manager, "_ssh_run", ssh), \
         patch.object(runtime_manager, "_evict_poll_interval", 0):
        result = await runtime_manager.evict_spark_runtime_containers("qwen-general")
    assert result["ok"] is True
    # At least one poll observed a still-running container before success.
    assert ssh.call_count >= 3


@pytest.mark.asyncio
async def test_evict_times_out_with_honest_error():
    """If containers never free up, eviction returns ok=False — not a silent pass."""
    # Stop command, then every poll keeps reporting a running solo container.
    ssh = AsyncMock(return_value=("sparkrun_stuck_solo", "", 0))
    with patch.object(runtime_manager, "_ssh_run", ssh), \
         patch.object(runtime_manager, "_evict_poll_interval", 0):
        result = await runtime_manager.evict_spark_runtime_containers(
            "qwen-general", timeout=0.05
        )
    assert result["ok"] is False
    assert "sparkrun_stuck_solo" in result["message"] or "still running" in result["message"].lower()


# ── Fix #1+#2: docker query error must not be mistaken for "box free" ────────


@pytest.mark.asyncio
async def test_evict_docker_query_error_is_treated_as_busy_not_free():
    """When the docker ps query exits non-zero (docker daemon unreachable,
    pipefail from a failed docker ps) the eviction poll must NOT return
    ok=True.  A query error means the box state is *unknown* — which is as
    dangerous as a live container.  The poll must keep treating it as busy
    until the timeout expires, then return ok=False."""
    # The stop command contains "xargs" — distinguishes it from poll calls.
    def _side_effect(cmd: str, **kwargs):
        if "xargs" in cmd:
            # stop command — docker daemon is broken but xargs -r with empty
            # input still exits 0; we model the same behaviour here.
            return ("", "", 0)
        # poll query (bash -o pipefail -c '...') — docker daemon error
        return ("", "Cannot connect to the Docker daemon at unix:///var/run/docker.sock", 1)

    ssh = AsyncMock(side_effect=_side_effect)
    with patch.object(runtime_manager, "_ssh_run", ssh), \
         patch.object(runtime_manager, "_evict_poll_interval", 0):
        result = await runtime_manager.evict_spark_runtime_containers(
            "qwen-general", timeout=0.05
        )

    # A query error is not "free" — the eviction must not declare ok=True.
    assert result["ok"] is False
    # At least one poll call must have been attempted after the stop.
    assert ssh.call_count >= 2


# ── P2: start-verification ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_returns_true_when_label_container_appears():
    ssh = AsyncMock(side_effect=[
        ("", "", 0),                        # poll #1: nothing yet
        ("sparkrun_new_solo", "", 0),       # poll #2: container appeared
    ])
    with patch.object(runtime_manager, "_ssh_run", ssh), \
         patch.object(runtime_manager, "_verify_poll_interval", 0):
        ok = await runtime_manager.verify_spark_container_started(
            "qwen-general", timeout=1.0
        )
    assert ok is True


@pytest.mark.asyncio
async def test_verify_returns_false_when_no_container_appears():
    ssh = AsyncMock(return_value=("", "", 0))  # nothing ever appears
    with patch.object(runtime_manager, "_ssh_run", ssh), \
         patch.object(runtime_manager, "_verify_poll_interval", 0):
        ok = await runtime_manager.verify_spark_container_started(
            "qwen-general", timeout=0.05
        )
    assert ok is False


# ── start_runtime: launch then verify ────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_runtime_verifies_launch_and_reports_log_on_failure():
    """When the nohup launch succeeds but no labelled container appears,
    start_runtime must return ok=False and surface the launch-log path (P2+P4)."""
    ssh = AsyncMock(return_value=("", "", 0))  # nohup launch returns exit 0
    with patch.object(runtime_manager, "_ssh_run", ssh), \
         patch.object(
             runtime_manager,
             "verify_spark_container_started",
             AsyncMock(return_value=False),
         ):
        result = await runtime_manager.start_runtime(SPARK_RT)
    assert result["ok"] is False
    assert "runtime-launch-qwen-general.log" in result["message"]


@pytest.mark.asyncio
async def test_start_runtime_ok_when_container_appears():
    ssh = AsyncMock(return_value=("", "", 0))
    with patch.object(runtime_manager, "_ssh_run", ssh), \
         patch.object(
             runtime_manager,
             "verify_spark_container_started",
             AsyncMock(return_value=True),
         ):
        result = await runtime_manager.start_runtime(SPARK_RT)
    assert result["ok"] is True


# ── stop_runtime RC-1 hardening: empty container_name → eviction ─────────────


@pytest.mark.asyncio
async def test_stop_runtime_empty_container_name_evicts_not_bare_stop():
    """RC-1: with container_name=None, stop_runtime must NOT run `docker stop `
    (empty arg) — it falls back to label/solo eviction instead."""
    rt = {**SPARK_RT, "container_name": None}
    evict = AsyncMock(return_value={"ok": True, "message": "evicted", "stopped": []})
    ssh = AsyncMock(return_value=("", "", 0))
    with patch.object(runtime_manager, "evict_spark_runtime_containers", evict), \
         patch.object(runtime_manager, "_ssh_run", ssh):
        result = await runtime_manager.stop_runtime(rt)
    assert result["ok"] is True
    evict.assert_awaited_once()
    # No bare `docker stop` with an empty arg was ever issued directly.
    for c in ssh.call_args_list:
        assert not c.args or "docker stop " not in c.args[0] or "xargs" in c.args[0]


@pytest.mark.asyncio
async def test_stop_runtime_quotes_container_name_with_timeout():
    """With a real container_name, stop_runtime quotes it and passes a timeout."""
    rt = {**SPARK_RT, "container_name": "sparkrun_abc_solo"}
    ssh = AsyncMock(return_value=("", "", 0))
    with patch.object(runtime_manager, "_ssh_run", ssh):
        result = await runtime_manager.stop_runtime(rt)
    assert result["ok"] is True
    stop_call = ssh.call_args_list[0]
    assert "docker stop" in stop_call.args[0]
    assert "sparkrun_abc_solo" in stop_call.args[0]
    assert stop_call.kwargs.get("timeout") is not None


@pytest.mark.asyncio
async def test_start_runtime_no_verify_without_slug():
    """A launch_command-only runtime with no resolvable slug can't be verified;
    start_runtime falls back to the old optimistic ok=True (no regression)."""
    rt = {**SPARK_RT, "id": None, "slug": None}
    ssh = AsyncMock(return_value=("", "", 0))
    with patch.object(runtime_manager, "_ssh_run", ssh):
        result = await runtime_manager.start_runtime(rt)
    # No slug → can't poll for a label, so we keep the previous behaviour.
    assert result["ok"] is True
