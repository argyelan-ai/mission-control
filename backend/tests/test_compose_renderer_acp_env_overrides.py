"""Verify the harness-driven OMP_DRIVER=acp env injection (ADR-084).

The omp bridge defaults to the native TUI driver (``_omp_driver()`` reads
OMP_DRIVER, default ``native``); the ACP protocol path (``omp acp``,
docker/omp-bridge/acp_client.py) is selected per container via OMP_DRIVER.
Under ADR-084 ACP is a PROPERTY OF THE omp HARNESS: the renderer injects
``OMP_DRIVER=acp`` into every service whose resolved image is the omp image
— decided centrally by ``harness_compat.omp_driver_for``, never by an agent
name list (ADR-081's ``OMP_ACP_AGENT_SLUGS`` is gone). The single global
rollback is ``OMP_DRIVER_DEFAULT=native``: one knob, whole fleet. A manually
set OMP_DRIVER entry (the documented per-service rollback to ``native``)
survives re-rendering untouched.
"""
from __future__ import annotations

import re
import textwrap

from app.config import settings
from app.services.compose_renderer import (
    OPENCLAUDE_IMAGE,
    OMP_IMAGE,
    _build_new_agent_block,
    _ensure_agent_env_overrides,
    _rewrite_compose,
)

# Neutral slugs: the fleet's own agent names never belong in code or tests.
OMP_SLUG = "alpha"  # omp-image agent — gets OMP_DRIVER=acp via its harness
OTHER_OMP_SLUG = "beta"  # second omp agent — same harness, same treatment
NON_OMP_SLUG = "gamma"  # claude-image agent


def _configure_driver(monkeypatch, driver: str = "acp") -> None:
    """Point the global fleet knob (OMP_DRIVER_DEFAULT) at ``driver``."""
    monkeypatch.setattr(settings, "omp_driver_default", driver, raising=False)


def _extract_service_block(content: str, slug: str) -> str:
    pattern = re.compile(
        rf"(^  mc-agent-{re.escape(slug)}:\s*$.*?)(?=^  mc-agent-|^[a-zA-Z]|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(content)
    if not m:
        raise AssertionError(f"service block mc-agent-{slug} not found")
    return m.group(1)


_COMPOSE = textwrap.dedent(
    f"""\
    x-claude-agent-base: &claude-agent-base
      image: mc-claude-agent:latest
      restart: unless-stopped

    x-omp-agent-base: &omp-agent-base
      image: mc-omp-agent:latest
      restart: unless-stopped

    services:
      mc-agent-{OMP_SLUG}:
        <<: *omp-agent-base
        container_name: mc-agent-{OMP_SLUG}
        environment:
          - AGENT_NAME={OMP_SLUG}

      mc-agent-{OTHER_OMP_SLUG}:
        <<: *omp-agent-base
        container_name: mc-agent-{OTHER_OMP_SLUG}
        environment:
          - AGENT_NAME={OTHER_OMP_SLUG}

      mc-agent-{NON_OMP_SLUG}:
        <<: *claude-agent-base
        container_name: mc-agent-{NON_OMP_SLUG}
        environment:
          - AGENT_NAME={NON_OMP_SLUG}
    """
)


# ── harness property: every omp service gets ACP, nobody else does ───────────


def test_omp_service_gets_omp_driver_acp(monkeypatch):
    """Every omp-image service must get OMP_DRIVER=acp — no config needed."""
    _configure_driver(monkeypatch)
    result = _rewrite_compose(_COMPOSE, {})
    block = _extract_service_block(result, OMP_SLUG)
    assert "- OMP_DRIVER=acp" in block
    assert "- OMP_ACP_PERMISSIONS=yolo" in block


def test_second_omp_service_gets_the_same_treatment(monkeypatch):
    """Sabotage-probe: the treatment follows the HARNESS, not a per-agent
    exception — a second omp service is identical to the first."""
    _configure_driver(monkeypatch)
    result = _rewrite_compose(_COMPOSE, {})
    block = _extract_service_block(result, OTHER_OMP_SLUG)
    assert "- OMP_DRIVER=acp" in block
    assert "- OMP_ACP_PERMISSIONS=yolo" in block


def test_non_omp_agent_gets_no_omp_driver(monkeypatch):
    """A claude-image agent must not get an OMP_DRIVER entry."""
    _configure_driver(monkeypatch)
    result = _rewrite_compose(_COMPOSE, {})
    assert "OMP_DRIVER" not in _extract_service_block(result, NON_OMP_SLUG)


def test_native_default_rolls_back_the_whole_fleet(monkeypatch):
    """OMP_DRIVER_DEFAULT=native is the ONE global escape hatch: every omp
    service stays untouched, no per-agent exception exists."""
    _configure_driver(monkeypatch, "native")
    result = _rewrite_compose(_COMPOSE, {})
    for slug in (OMP_SLUG, OTHER_OMP_SLUG, NON_OMP_SLUG):
        assert "OMP_DRIVER" not in _extract_service_block(result, slug)


def test_invalid_driver_value_falls_back_to_acp(monkeypatch):
    """A garbage OMP_DRIVER_DEFAULT must fail towards ACP, not native — the
    harness property stays intact unless the operator explicitly opts out."""
    _configure_driver(monkeypatch, "TUI-please")
    result = _rewrite_compose(_COMPOSE, {})
    assert "- OMP_DRIVER=acp" in _extract_service_block(result, OMP_SLUG)


# ── idempotency + rollback survival ───────────────────────────────────────────


def test_rewrite_compose_is_idempotent_for_omp_driver(monkeypatch):
    """Running _rewrite_compose twice must not duplicate the env entry."""
    _configure_driver(monkeypatch)
    once = _rewrite_compose(_COMPOSE, {})
    twice = _rewrite_compose(once, {})
    for slug in (OMP_SLUG, OTHER_OMP_SLUG):
        assert _extract_service_block(twice, slug).count("OMP_DRIVER=acp") == 1


def test_existing_native_rollback_survives_re_rendering(monkeypatch):
    """A deliberate per-service OMP_DRIVER=native (the documented rollback)
    must survive re-rendering — the renderer never upgrades it back to acp."""
    _configure_driver(monkeypatch)
    content = _COMPOSE.replace(
        f"      - AGENT_NAME={OMP_SLUG}",
        f"      - AGENT_NAME={OMP_SLUG}\n      - OMP_DRIVER=native",
    )
    result = _rewrite_compose(content, {})
    block = _extract_service_block(result, OMP_SLUG)
    assert "- OMP_DRIVER=native" in block
    assert "OMP_ACP_PERMISSIONS" not in block

def test_service_without_environment_block_gets_one(monkeypatch):
    """An omp service body without an environment block must get a complete
    one (created), not a silent no-op."""
    _configure_driver(monkeypatch)
    content = _COMPOSE.replace(
        "    environment:\n      - AGENT_NAME=alpha\n", ""
    )
    result = _rewrite_compose(content, {})
    block = _extract_service_block(result, OMP_SLUG)
    assert "    environment:" in block
    assert "- OMP_DRIVER=acp" in block


# ── new-agent append path ─────────────────────────────────────────────────────


def test_new_omp_agent_block_gets_driver(monkeypatch):
    """A brand-new cli-bridge omp agent (not present in the static file) gets
    OMP_DRIVER=acp from its image alone — the fresh-agent guarantee."""
    _configure_driver(monkeypatch)
    block = _build_new_agent_block("fresh-agent", OMP_IMAGE, is_vault_writer=False)
    assert "- OMP_DRIVER=acp" in block
    assert "- OMP_ACP_PERMISSIONS=yolo" in block


def test_new_claude_agent_block_gets_no_driver(monkeypatch):
    _configure_driver(monkeypatch)
    block = _build_new_agent_block("newagent", OPENCLAUDE_IMAGE, is_vault_writer=False)
    assert "OMP_DRIVER" not in block


def test_new_omp_agent_block_stays_native_under_global_rollback(monkeypatch):
    _configure_driver(monkeypatch, "native")
    block = _build_new_agent_block("fresh-agent", OMP_IMAGE, is_vault_writer=False)
    assert "OMP_DRIVER" not in block


# ── _ensure_agent_env_overrides unit cases ────────────────────────────────────


class TestEnsureAgentEnvOverrides:
    def test_inserts_before_next_block(self, monkeypatch):
        _configure_driver(monkeypatch)
        body = [
            "    environment:",
            "      - AGENT_NAME=foo",
            "    volumes:",
            "      - x:/workspace",
        ]
        out = _ensure_agent_env_overrides(body, "omp")
        assert out.index("      - OMP_DRIVER=acp") < out.index("    volumes:")

    def test_idempotent_keeps_existing_value(self, monkeypatch):
        _configure_driver(monkeypatch)
        body = ["    environment:", "      - OMP_DRIVER=native"]
        # native rollback: no companion vars either
        assert _ensure_agent_env_overrides(body, "omp") == body

    def test_existing_acp_driver_gets_permission_policy_once(self, monkeypatch):
        _configure_driver(monkeypatch)
        body = ["    environment:", "      - OMP_DRIVER=acp"]
        once = _ensure_agent_env_overrides(body, "omp")
        assert once.count("      - OMP_ACP_PERMISSIONS=yolo") == 1
        assert _ensure_agent_env_overrides(once, "omp") == once

    def test_non_omp_harness_is_noop(self, monkeypatch):
        _configure_driver(monkeypatch)
        body = ["    environment:", "      - AGENT_NAME=foo"]
        assert _ensure_agent_env_overrides(body, "claude") == body
        assert _ensure_agent_env_overrides(body, None) == body

    def test_creates_environment_block_when_missing(self, monkeypatch):
        _configure_driver(monkeypatch)
        body = ["    restart: unless-stopped"]
        out = _ensure_agent_env_overrides(body, "omp")
        assert "    environment:" in out
        assert "      - OMP_DRIVER=acp" in out
