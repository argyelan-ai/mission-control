"""Tests for robust Spark runtime eviction + start-verification.

These pin the P0-P4 hardening of the recipe-switch flow:

  P0 — eviction stops ALL running Spark model containers before a fresh start,
       not just the (often-empty) ``container_name``. Three layers:
         (a) by label ``mc.runtime.slug=<slug>``
         (b) a sweep of every ``sparkrun_*_solo`` container (catches CLI- or
             externally-started models MC never labelled).
         (c) a ``com.docker.compose.project`` sweep (Task #22 — catches a
             compose service with no ``container_name:``, previously
             invisible to (a) and (b)).
  P1 — after the stop, poll until no Spark model container is left running
       (bounded timeout, then an honest error) so the new launch doesn't race
       against a still-occupied GPU/RAM.
  P2 — after the nohup launch, poll for a container carrying the slug label;
       if none appears, return ok=False with the launch-log path.
  P3 — ownership (Task #22): a container found by (a) is only stopped if its
       ``mc.runtime.nonce`` label matches what MC recorded at launch time.
       Dedicated ownership scenarios (mismatch/missing/matching nonce) live
       in ``test_eviction_ownership.py``; the tests here use containers with
       no recorded nonce expectation (the common case before this feature's
       first switch), which stay stoppable exactly as before.
  P4 — error messages carry the ``~/.cache/mc/runtime-launch-<slug>.log`` path.

All SSH is mocked — nothing touches the real Spark (192.0.2.10).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, call, patch

import pytest

from app.services import runtime_manager


def _discovery_output(
    *, total: int = 0, label=None, solo=None, manual=None, project=None
) -> str:
    """Builds the section-marked output ``_eviction_discovery_script``
    expects to parse back — ``project`` is a list of ``(id, project_name)``
    pairs, everything else a plain list of container ids."""
    lines = [
        "__TOTAL__", str(total),
        "__LABEL__", *(label or []),
        "__SOLO__", *(solo or []),
        "__MANUAL__", *(manual or []),
        "__PROJECT__", *(f"{cid}|{proj}" for cid, proj in (project or [])),
    ]
    return "\n".join(lines)


def _inspect_output(rows) -> str:
    """``rows``: list of ``(container_id, slug_label, nonce_label)``."""
    return "\n".join(f"{cid}\t{slug}\t{nonce}" for cid, slug, nonce in rows)


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


# ── P0: eviction stops label + solo-sweep + compose-project containers ───────


@pytest.mark.asyncio
async def test_evict_stops_label_and_solo_containers():
    """Eviction discovery must reference BOTH the label filter AND the solo
    glob, and must NOT depend on container_name (which is None after a
    switch). No nonce was ever recorded for this slug, so the found
    container is trusted and stopped — same outcome as before Task #22."""
    ssh = AsyncMock(side_effect=[
        (_discovery_output(total=1, label=["sparkrun_oldid_solo"],
                            solo=["sparkrun_oldid_solo"]), "", 0),  # discovery
        (_inspect_output([("sparkrun_oldid_solo", "qwen-general", "")]), "", 0),  # ownership
        ("sparkrun_oldid_solo", "", 0),   # docker stop
        ("", "", 0),                       # poll: gone
    ])
    with patch.object(runtime_manager, "_ssh_run", ssh), \
         patch.object(runtime_manager, "_evict_poll_interval", 0):
        result = await runtime_manager.evict_spark_runtime_containers("qwen-general")

    assert result["ok"] is True
    assert result["stopped"] == ["sparkrun_oldid_solo"]
    discovery_cmd = ssh.call_args_list[0].args[0]
    assert "mc.runtime.slug=qwen-general" in discovery_cmd
    assert "sparkrun_" in discovery_cmd and "_solo" in discovery_cmd
    assert "com.docker.compose.project" in discovery_cmd
    stop_cmd = ssh.call_args_list[2].args[0]
    assert stop_cmd == "docker stop sparkrun_oldid_solo"


@pytest.mark.asyncio
async def test_evict_finds_compose_container_without_container_name():
    """Task #22's live incident: a compose service with no ``container_name:``
    gets a docker-generated name that matches neither the label nor either
    name filter — only the compose-project sweep finds it."""
    ssh = AsyncMock(side_effect=[
        (_discovery_output(
            total=2,
            project=[("generatedname123", "sparkrun_qwenwrap")],
        ), "", 0),                                                    # discovery
        (_inspect_output([("generatedname123", "", "")]), "", 0),      # ownership: no slug label
        ("generatedname123", "", 0),                                   # docker stop
        ("", "", 0),                                                   # poll: gone
    ])
    with patch.object(runtime_manager, "_ssh_run", ssh), \
         patch.object(runtime_manager, "_evict_poll_interval", 0):
        result = await runtime_manager.evict_spark_runtime_containers("deepseek-v4")

    assert result["ok"] is True
    assert result["stopped"] == ["generatedname123"]


@pytest.mark.asyncio
async def test_evict_slug_is_shell_quoted():
    """A slug must be shell-safe — the discovery script quotes it (via
    ``_sanitize_slug`` + ``shlex_quote``) to prevent injection."""
    ssh = AsyncMock(return_value=(_discovery_output(total=0), "", 0))
    with patch.object(runtime_manager, "_ssh_run", ssh):
        # An attacker-ish slug should not break out of the docker filter.
        await runtime_manager.evict_spark_runtime_containers("evil; rm -rf /")
    discovery_cmd = ssh.call_args_list[0].args[0]
    # The dangerous payload must be quoted, not interpolated raw as a command.
    assert "; rm -rf /" not in discovery_cmd.replace("'evil; rm -rf /'", "")


@pytest.mark.asyncio
async def test_evict_nothing_found_reports_diagnostics():
    """P3/visibility: a "box already free" result must carry the box-wide
    container total, not just an empty match list — this is what makes a
    false all-clear (the live incident) diagnosable from the logs alone."""
    ssh = AsyncMock(return_value=(_discovery_output(total=7), "", 0))
    with patch.object(runtime_manager, "_ssh_run", ssh):
        result = await runtime_manager.evict_spark_runtime_containers("qwen-general")
    assert result["ok"] is True
    assert result["stopped"] == []
    assert "7" in result["message"]


@pytest.mark.asyncio
async def test_evict_discovery_error_returns_ok_false():
    """A broken discovery query (docker daemon unreachable) must not be
    mistaken for "nothing running" — that was exactly the shape of the
    original P0 bug, just one layer earlier."""
    ssh = AsyncMock(return_value=("", "Cannot connect to the Docker daemon", 1))
    with patch.object(runtime_manager, "_ssh_run", ssh):
        result = await runtime_manager.evict_spark_runtime_containers("qwen-general")
    assert result["ok"] is False
    assert "discovery" in result["message"].lower() or "Discovery" in result["message"]


# ── P1: poll until free, honest timeout ──────────────────────────────────────


def _happy_discovery_and_ownership(cmd: str, container_id: str):
    """Shared side_effect body for the discovery+ownership prefix of an
    eviction call — used by tests that only care about the stop/poll tail."""
    if "__TOTAL__" in cmd:
        return (_discovery_output(total=1, solo=[container_id]), "", 0)
    if "docker inspect" in cmd:
        return (_inspect_output([(container_id, "", "")]), "", 0)
    return None


@pytest.mark.asyncio
async def test_evict_waits_until_no_solo_container_running():
    """Eviction polls until the stopped container is actually gone."""
    def _side_effect(cmd: str, **kwargs):
        pre = _happy_discovery_and_ownership(cmd, "sparkrun_a_solo")
        if pre is not None:
            return pre
        if cmd.startswith("docker stop"):
            return ("sparkrun_a_solo", "", 0)
        # poll: first call still running, then gone
        _side_effect.polls += 1
        return ("sparkrun_a_solo", "", 0) if _side_effect.polls == 1 else ("", "", 0)

    _side_effect.polls = 0
    ssh = AsyncMock(side_effect=_side_effect)
    with patch.object(runtime_manager, "_ssh_run", ssh), \
         patch.object(runtime_manager, "_evict_poll_interval", 0):
        result = await runtime_manager.evict_spark_runtime_containers("qwen-general")
    assert result["ok"] is True
    # discovery + inspect + stop + at least 2 polls
    assert ssh.call_count >= 5


@pytest.mark.asyncio
async def test_evict_times_out_with_honest_error():
    """If a container never actually stops, eviction returns ok=False — not
    a silent pass."""
    def _side_effect(cmd: str, **kwargs):
        pre = _happy_discovery_and_ownership(cmd, "sparkrun_stuck_solo")
        if pre is not None:
            return pre
        if cmd.startswith("docker stop"):
            return ("sparkrun_stuck_solo", "", 0)
        return ("sparkrun_stuck_solo", "", 0)  # every poll: still running

    ssh = AsyncMock(side_effect=_side_effect)
    with patch.object(runtime_manager, "_ssh_run", ssh), \
         patch.object(runtime_manager, "_evict_poll_interval", 0):
        result = await runtime_manager.evict_spark_runtime_containers(
            "qwen-general", timeout=0.05
        )
    assert result["ok"] is False
    assert "sparkrun_stuck_solo" in result["message"] or "still running" in result["message"].lower()


# ── Fix #1+#2: docker query error must not be mistaken for "box free" ────────


@pytest.mark.asyncio
async def test_evict_poll_query_error_is_treated_as_busy_not_free():
    """When the poll's docker ps query exits non-zero (docker daemon
    unreachable) the eviction poll must NOT return ok=True. A query error
    means the box state is *unknown* — which is as dangerous as a live
    container. The poll must keep treating it as busy until the timeout
    expires, then return ok=False."""
    def _side_effect(cmd: str, **kwargs):
        pre = _happy_discovery_and_ownership(cmd, "sparkrun_x_solo")
        if pre is not None:
            return pre
        if cmd.startswith("docker stop"):
            return ("sparkrun_x_solo", "", 0)
        # poll query — docker daemon error
        return ("", "Cannot connect to the Docker daemon at unix:///var/run/docker.sock", 1)

    ssh = AsyncMock(side_effect=_side_effect)
    with patch.object(runtime_manager, "_ssh_run", ssh), \
         patch.object(runtime_manager, "_evict_poll_interval", 0):
        result = await runtime_manager.evict_spark_runtime_containers(
            "qwen-general", timeout=0.05
        )

    # A query error is not "free" — the eviction must not declare ok=True.
    assert result["ok"] is False
    # discovery + inspect + stop + at least one poll attempt.
    assert ssh.call_count >= 4


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


# ── P6 (ADR-059): process-liveness check — container up ≠ vLLM serving ───────
# A sparkrun/manual container can appear (PID1 running, e.g. a `sleep
# infinity` wrapper) while the actual `vllm serve` process inside never
# started or crashed immediately (wrong tp, OOM, bad recipe args). The P2
# container-existence check alone reports this as a success. This adds a
# second, equally cheap check: is there an actual vllm-serve process in the
# container's process list.


@pytest.mark.asyncio
async def test_verify_process_returns_true_when_vllm_serve_running():
    ssh = AsyncMock(side_effect=[
        ("sparkrun_new_solo", "", 0),                  # container lookup
        ("root  1  vllm serve Qwen/Foo --port 8000", "", 0),  # docker top
    ])
    with patch.object(runtime_manager, "_ssh_run", ssh), \
         patch.object(runtime_manager, "_verify_poll_interval", 0):
        ok = await runtime_manager.verify_spark_vllm_process_started(
            "qwen-general", timeout=1.0
        )
    assert ok is True


@pytest.mark.asyncio
async def test_verify_process_returns_false_when_process_never_appears():
    # Container exists but `docker top` never shows a vllm serve process —
    # e.g. it crashed instantly or the container is a bare sleep-infinity shell.
    ssh = AsyncMock(side_effect=[
        ("sparkrun_new_solo", "", 0),   # container lookup
        ("root  1  sleep infinity", "", 0),  # docker top — no vllm process
    ] * 5)
    with patch.object(runtime_manager, "_ssh_run", ssh), \
         patch.object(runtime_manager, "_verify_poll_interval", 0):
        ok = await runtime_manager.verify_spark_vllm_process_started(
            "qwen-general", timeout=0.05
        )
    assert ok is False


@pytest.mark.asyncio
async def test_verify_process_returns_false_when_container_never_appears():
    ssh = AsyncMock(return_value=("", "", 0))  # container lookup: nothing
    with patch.object(runtime_manager, "_ssh_run", ssh), \
         patch.object(runtime_manager, "_verify_poll_interval", 0):
        ok = await runtime_manager.verify_spark_vllm_process_started(
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
async def test_start_runtime_ok_when_container_appears_and_vllm_serves():
    ssh = AsyncMock(return_value=("", "", 0))
    with patch.object(runtime_manager, "_ssh_run", ssh), \
         patch.object(
             runtime_manager,
             "verify_spark_container_started",
             AsyncMock(return_value=True),
         ), \
         patch.object(
             runtime_manager,
             "verify_spark_vllm_process_started",
             AsyncMock(return_value=True),
         ):
        result = await runtime_manager.start_runtime(SPARK_RT)
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_start_runtime_fails_when_container_appears_but_vllm_process_never_starts():
    """The exact failure mode from the incident: container up (nohup exit 0,
    label appears), but the vllm serve process inside never came up (wrong
    tp/OOM/crash). Must be reported as a failure, not a silent success."""
    ssh = AsyncMock(return_value=("", "", 0))
    with patch.object(runtime_manager, "_ssh_run", ssh), \
         patch.object(
             runtime_manager,
             "verify_spark_container_started",
             AsyncMock(return_value=True),
         ), \
         patch.object(
             runtime_manager,
             "verify_spark_vllm_process_started",
             AsyncMock(return_value=False),
         ):
        result = await runtime_manager.start_runtime(SPARK_RT)
    assert result["ok"] is False
    assert "vllm" in result["message"].lower()
    assert "runtime-launch-qwen-general.log" in result["message"]


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
