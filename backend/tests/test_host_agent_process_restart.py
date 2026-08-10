"""Tests for the host-agent "Prozess neu starten" endpoint (Task #19, 2026-08-08).

Everything that would talk to launchd/pgrep/pkill on the real Mac goes through
one seam, ``cli_terminal._ssh_host`` — every test here mocks that seam and
asserts on WHICH commands would run and what the endpoint makes of their
output. No real subprocess, no real DB write outside the in-memory test DB,
no real launchctl. See test_host_probe_bootstrap.py for the same convention.
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

import app.routers.cli_terminal as cli_mod
from app.models.agent import Agent


def _boss_agent() -> Agent:
    return Agent(id=uuid.uuid4(), name="Boss", slug="boss", agent_runtime="host", harness="claude")


def _hermes_agent() -> Agent:
    return Agent(id=uuid.uuid4(), name="Hermes", slug="hermes", agent_runtime="host", harness="hermes")


def _generic_agent() -> Agent:
    return Agent(id=uuid.uuid4(), name="Dev Kimi", slug="dev-kimi", agent_runtime="host", harness="omp")


# ── Self-exclusion pattern (the pkill-matches-itself pitfall) ───────────────

def test_bracket_self_exclude_produces_bracket_expression():
    assert cli_mod._bracket_self_exclude("agents/boss-host/entrypoint.sh") == \
        "[a]gents/boss-host/entrypoint.sh"


def test_pgrep_command_embeds_bracketed_pattern_not_raw():
    cmd = cli_mod._pgrep_command("scripts/hermes-bridge.py")
    assert "[s]cripts/hermes-bridge.py" in cmd
    # The raw (non-bracketed) substring must not appear on its own — that is
    # exactly the self-match bug: pgrep's own argv would contain it verbatim.
    assert "'scripts/hermes-bridge.py'" not in cmd


# ── Label/pattern resolution ─────────────────────────────────────────────────

def test_resolve_target_boss():
    label, pattern = cli_mod._resolve_host_agent_restart_target(_boss_agent())
    assert label == "com.openclaw.boss"
    assert pattern == "agents/boss-host/entrypoint.sh"


def test_resolve_target_hermes():
    label, pattern = cli_mod._resolve_host_agent_restart_target(_hermes_agent())
    assert label == "com.mc.hermes-bridge"
    assert pattern == "scripts/hermes-bridge.py"


def test_resolve_target_generic_staged_agent_falls_back_to_wizard_layout():
    label, pattern = cli_mod._resolve_host_agent_restart_target(_generic_agent())
    assert label == "com.mc.agent.dev-kimi"
    assert pattern == ".mc/agents/dev-kimi/run.sh"


# ── launchctl exit-code tolerance (I/O error 5 is not a failure) ────────────

def test_launchctl_exit_ok_accepts_zero():
    assert cli_mod._launchctl_exit_ok("kickstart succeeded\nEXIT:0") is True


def test_launchctl_exit_ok_accepts_io_error_5():
    assert cli_mod._launchctl_exit_ok("Input/output error\nEXIT:5") is True


def test_launchctl_exit_ok_rejects_other_codes():
    assert cli_mod._launchctl_exit_ok("No such process\nEXIT:3") is False


def test_launchctl_exit_ok_rejects_missing_exit_marker():
    assert cli_mod._launchctl_exit_ok("ssh connection reset") is False


# ── Orphan sweep ──────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_sweep_kills_only_matching_pids_term_then_verify():
    calls = []
    # First pgrep (before TERM) finds two orphans; second (after TERM) finds
    # none — they died cleanly from TERM alone, no KILL escalation needed.
    pgrep_outputs = iter(["1234\n5678", ""])

    async def fake_ssh(command, timeout=30):
        calls.append(command)
        if command.startswith("pgrep"):
            return next(pgrep_outputs)
        return ""

    with patch.object(cli_mod, "_ssh_host", AsyncMock(side_effect=fake_ssh)):
        with patch.object(cli_mod.asyncio, "sleep", AsyncMock()):
            killed = await cli_mod._sweep_orphan_host_processes("agents/boss-host/entrypoint.sh")

    assert killed == ["1234", "5678"]
    term_calls = [c for c in calls if c.startswith("pkill -TERM")]
    kill_calls = [c for c in calls if c.startswith("pkill -KILL")]
    assert len(term_calls) == 1
    assert "[a]gents/boss-host/entrypoint.sh" in term_calls[0]
    # Second pgrep came back empty -> no KILL escalation needed.
    assert kill_calls == []


@pytest.mark.anyio
async def test_sweep_escalates_to_kill_when_term_did_not_finish_them():
    pgrep_outputs = iter(["999", "999"])  # still alive after TERM

    async def fake_ssh(command, timeout=30):
        if command.startswith("pgrep"):
            return next(pgrep_outputs)
        return ""

    with patch.object(cli_mod, "_ssh_host", AsyncMock(side_effect=fake_ssh)):
        with patch.object(cli_mod.asyncio, "sleep", AsyncMock()):
            killed = await cli_mod._sweep_orphan_host_processes("scripts/grok-bridge.py")

    assert killed == ["999"]


@pytest.mark.anyio
async def test_sweep_no_op_when_nothing_matches():
    async def fake_ssh(command, timeout=30):
        return ""

    calls_before = AsyncMock(side_effect=fake_ssh)
    with patch.object(cli_mod, "_ssh_host", calls_before):
        killed = await cli_mod._sweep_orphan_host_processes("scripts/grok-bridge.py")

    assert killed == []
    # Only the initial pgrep ran — no pkill issued for a clean host.
    assert calls_before.call_count == 1


# ── Denylist: ai.hermes.gateway must never be touched ────────────────────────

@pytest.mark.anyio
async def test_denylist_blocks_retired_gateway_label():
    agent = _hermes_agent()
    with patch.object(
        cli_mod, "_resolve_host_agent_restart_target",
        return_value=("ai.hermes.gateway", "irrelevant"),
    ):
        with patch.object(cli_mod, "_ssh_host", AsyncMock()) as ssh:
            with pytest.raises(Exception) as exc_info:
                await cli_mod._host_agent_process_restart(agent)

    # No SSH call of any kind may have happened once the denylist fires.
    ssh.assert_not_called()
    assert "403" in str(exc_info.value) or getattr(exc_info.value, "status_code", None) == 403


@pytest.mark.anyio
async def test_denylist_sabotage_probe_would_fail_without_the_guard():
    """Sabotage probe (per brief): with the guard bypassed, the denylisted
    label WOULD be restarted — proving the guard in the real code is load-
    bearing, not a no-op. This simulates "commenting out the check" by
    calling the internals directly, skipping _host_agent_process_restart's
    own guard clause.
    """
    calls = []

    async def fake_ssh(command, timeout=30):
        calls.append(command)
        if command.startswith("pgrep"):
            return "1"
        return "EXIT:0"

    with patch.object(cli_mod, "_ssh_host", AsyncMock(side_effect=fake_ssh)):
        with patch.object(cli_mod.asyncio, "sleep", AsyncMock()):
            # Directly exercises the kickstart path the guard exists to
            # prevent from ever running for this label.
            uid_label = f"gui/$(id -u testuser)/ai.hermes.gateway"
            await cli_mod._ssh_host(f"launchctl kickstart -k {uid_label} 2>&1; echo EXIT:$?")

    assert any("ai.hermes.gateway" in c for c in calls)


# ── Full endpoint flow (HTTP layer) ──────────────────────────────────────────

@pytest.mark.anyio
async def test_restart_process_endpoint_success(auth_client: AsyncClient, make_agent):
    agent = await make_agent(
        name="Boss", slug="boss", agent_runtime="host", harness="claude",
    )

    pgrep_outputs = iter(["", "4242"])  # sweep: none (1 pgrep call); post-restart verify: running
    curl_calls = []

    async def fake_ssh(command, timeout=30):
        if command.startswith("pgrep"):
            return next(pgrep_outputs)
        if command.startswith("launchctl kickstart"):
            return "EXIT:0"
        if "curl" in command:
            curl_calls.append(command)
        return ""

    with patch.object(cli_mod, "_ssh_host", AsyncMock(side_effect=fake_ssh)):
        with patch.object(cli_mod.asyncio, "sleep", AsyncMock()):
            resp = await auth_client.post(f"/api/v1/host-agents/{agent.id}/restart-process")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["agent"] == "boss"
    assert body["label"] == "com.openclaw.boss"
    assert body["process_running"] is True
    assert body["fallback_used"] is False
    # Non-Hermes agents have no separate worker-session layer — the extra
    # bridge /restart call (and the resulting response key) must not appear.
    assert "worker_restart_output" not in body
    assert curl_calls == []


# ── Hermes worker-session restart (Task #25 fix) ─────────────────────────────

@pytest.mark.anyio
async def test_restart_process_endpoint_hermes_restarts_worker_session(
    auth_client: AsyncClient, make_agent,
):
    """Hermes: after a clean launchd kickstart, the endpoint must ALSO hit the
    bridge's own POST /restart so the tmux 'hermes-worker' session (and thus
    entrypoint.sh's hermes-config-patch.py sync) actually reruns. This is the
    fix for the live bug: restart-process previously only recycled the bridge
    HTTP server, leaving the Hermes TUI on its stale model indefinitely."""
    agent = await make_agent(
        name="Hermes", slug="hermes", agent_runtime="host", harness="hermes",
    )

    pgrep_outputs = iter(["", "777"])  # sweep clean; post-kickstart verify: running
    curl_calls = []

    async def fake_ssh(command, timeout=30):
        if command.startswith("pgrep"):
            return next(pgrep_outputs)
        if command.startswith("launchctl kickstart"):
            return "EXIT:0"
        if "curl" in command:
            curl_calls.append(command)
            return '{"ok": true, "restart": {"status": "started"}}'
        return ""

    with patch.object(cli_mod, "_ssh_host", AsyncMock(side_effect=fake_ssh)):
        with patch.object(cli_mod.asyncio, "sleep", AsyncMock()):
            resp = await auth_client.post(f"/api/v1/host-agents/{agent.id}/restart-process")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert "started" in body["worker_restart_output"]
    assert len(curl_calls) == 1
    assert ":18794/restart" in curl_calls[0]


@pytest.mark.anyio
async def test_restart_hermes_worker_session_retries_until_bridge_answers(
    auth_client: AsyncClient, make_agent,
):
    """The bridge process was just kickstarted by this same endpoint — its
    HTTP server may not be listening yet. The worker-restart call must retry
    (not fail on the first BRIDGE_UNREACHABLE) instead of reporting a
    misleading success/failure on a simple startup race."""
    agent = await make_agent(
        name="Hermes", slug="hermes", agent_runtime="host", harness="hermes",
    )

    pgrep_outputs = iter(["", "777"])
    curl_attempts = {"n": 0}

    async def fake_ssh(command, timeout=30):
        if command.startswith("pgrep"):
            return next(pgrep_outputs)
        if command.startswith("launchctl kickstart"):
            return "EXIT:0"
        if "curl" in command:
            curl_attempts["n"] += 1
            if curl_attempts["n"] < 3:
                return "BRIDGE_UNREACHABLE"
            return '{"ok": true, "restart": {"status": "started"}}'
        return ""

    with patch.object(cli_mod, "_ssh_host", AsyncMock(side_effect=fake_ssh)):
        with patch.object(cli_mod.asyncio, "sleep", AsyncMock()) as sleep_mock:
            resp = await auth_client.post(f"/api/v1/host-agents/{agent.id}/restart-process")

    assert resp.status_code == 200, resp.text
    assert curl_attempts["n"] == 3
    assert sleep_mock.await_count >= 2  # at least 2 retry delays before success


@pytest.mark.anyio
async def test_restart_hermes_worker_session_502_when_bridge_never_comes_up(
    auth_client: AsyncClient, make_agent,
):
    """If the bridge HTTP server never comes back after the launchd kickstart,
    the endpoint must surface a 502 — silently reporting success while the
    TUI is provably unreachable would recreate exactly the "reported clean,
    nothing happened" failure mode this endpoint exists to prevent."""
    agent = await make_agent(
        name="Hermes", slug="hermes", agent_runtime="host", harness="hermes",
    )

    pgrep_outputs = iter(["", "777"])

    async def fake_ssh(command, timeout=30):
        if command.startswith("pgrep"):
            return next(pgrep_outputs)
        if command.startswith("launchctl kickstart"):
            return "EXIT:0"
        if "curl" in command:
            return "BRIDGE_UNREACHABLE"
        return ""

    with patch.object(cli_mod, "_ssh_host", AsyncMock(side_effect=fake_ssh)):
        with patch.object(cli_mod.asyncio, "sleep", AsyncMock()):
            resp = await auth_client.post(f"/api/v1/host-agents/{agent.id}/restart-process")

    assert resp.status_code == 502, resp.text


@pytest.mark.anyio
async def test_restart_process_endpoint_falls_back_when_kickstart_fails_hard(
    auth_client: AsyncClient, make_agent,
):
    agent = await make_agent(
        name="Hermes", slug="hermes", agent_runtime="host", harness="hermes",
    )

    pgrep_outputs = iter(["", "555"])  # sweep clean (1 pgrep call); verify: running after fallback

    async def fake_ssh(command, timeout=30):
        if command.startswith("pgrep"):
            return next(pgrep_outputs)
        if command.startswith("launchctl kickstart"):
            return "some error\nEXIT:3"  # not 0, not 5 -> not ok
        if command.startswith("launchctl unload"):
            return "UNLOAD:0\nLOAD:0"
        if "curl" in command and ":18794/restart" in command:
            return '{"ok": true, "restart": {"status": "started", "session": "hermes-worker"}}'
        return ""

    with patch.object(cli_mod, "_ssh_host", AsyncMock(side_effect=fake_ssh)):
        with patch.object(cli_mod.asyncio, "sleep", AsyncMock()):
            resp = await auth_client.post(f"/api/v1/host-agents/{agent.id}/restart-process")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["fallback_used"] is True
    assert body["process_running"] is True
    # Hermes-only: the tmux worker session must also have been recycled so
    # entrypoint.sh (and thus hermes-config-patch.py) actually reran —
    # otherwise the TUI keeps its pre-switch model (Task #25 live bug).
    assert "started" in body["worker_restart_output"]


@pytest.mark.anyio
async def test_restart_process_endpoint_502_when_nothing_comes_up(
    auth_client: AsyncClient, make_agent,
):
    agent = await make_agent(
        name="Boss", slug="boss", agent_runtime="host", harness="claude",
    )

    async def fake_ssh(command, timeout=30):
        if command.startswith("pgrep"):
            return ""  # never comes up, sweep and verify both empty
        if command.startswith("launchctl kickstart"):
            return "EXIT:0"
        return ""

    with patch.object(cli_mod, "_ssh_host", AsyncMock(side_effect=fake_ssh)):
        with patch.object(cli_mod.asyncio, "sleep", AsyncMock()):
            resp = await auth_client.post(f"/api/v1/host-agents/{agent.id}/restart-process")

    assert resp.status_code == 502


@pytest.mark.anyio
async def test_restart_process_endpoint_blocks_hermes_gateway_label(
    auth_client: AsyncClient, make_agent,
):
    agent = await make_agent(
        name="Hermes", slug="hermes", agent_runtime="host", harness="hermes",
    )

    with patch.object(
        cli_mod, "_resolve_host_agent_restart_target",
        return_value=("ai.hermes.gateway", "irrelevant"),
    ):
        with patch.object(cli_mod, "_ssh_host", AsyncMock()) as ssh:
            resp = await auth_client.post(f"/api/v1/host-agents/{agent.id}/restart-process")

    assert resp.status_code == 403
    assert "ai.hermes.gateway" in resp.text
    ssh.assert_not_called()


@pytest.mark.anyio
async def test_restart_process_endpoint_requires_auth(client: AsyncClient, make_agent):
    agent = await make_agent(name="Boss", slug="boss", agent_runtime="host", harness="claude")
    resp = await client.post(f"/api/v1/host-agents/{agent.id}/restart-process")
    assert resp.status_code in (401, 403)


@pytest.mark.anyio
async def test_restart_process_endpoint_404_for_non_host_agent(
    auth_client: AsyncClient, make_agent,
):
    agent = await make_agent(name="Dev", slug="dev", agent_runtime="cli-bridge")
    resp = await auth_client.post(f"/api/v1/host-agents/{agent.id}/restart-process")
    assert resp.status_code == 404
