"""Verify the config-driven per-agent OMP_DRIVER=acp env injection (ADR-081
"Protokoll vor Pane").

The omp bridge defaults to the native TUI driver (``_omp_driver()`` reads
OMP_DRIVER, default ``native``); the ACP protocol path (``omp acp``,
docker/omp-bridge/acp_client.py) is selected per container via OMP_DRIVER.
The renderer must inject ``OMP_DRIVER=acp`` ONLY into services whose slug is
listed in the deployment config (``OMP_ACP_AGENT_SLUGS`` in .env, exposed as
``settings.omp_acp_agent_slugs``) — every other agent (omp fleet included)
must stay untouched so the live gate is provably per-agent. An unlisted slug
gets no entry; an empty list means the whole fleet stays native. A manually
set OMP_DRIVER entry (the documented rollback to ``native``) survives
re-rendering untouched.
"""
from __future__ import annotations

import re
import textwrap

from app.config import settings
from app.services.compose_renderer import (
    OPENCLAUDE_IMAGE,
    _build_new_agent_block,
    _ensure_agent_env_overrides,
    _rewrite_compose,
)

# Neutral slugs: the fleet's own agent names never belong in code or tests.
ACP_SLUG = "alpha"  # configured to receive OMP_DRIVER=acp
OTHER_OMP_SLUG = "beta"  # second omp agent — the sabotage probe
NON_OMP_SLUG = "gamma"  # claude-image agent


def _configure_acp(monkeypatch, slugs: str = ACP_SLUG) -> None:
    """Point the deployment config at the neutral test slugs."""
    monkeypatch.setattr(settings, "omp_acp_agent_slugs", slugs, raising=False)


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
      mc-agent-{ACP_SLUG}:
        <<: *omp-agent-base
        container_name: mc-agent-{ACP_SLUG}
        environment:
          - AGENT_NAME={ACP_SLUG}

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


# ── sabotage-probe guarantee: per-agent isolation ─────────────────────────────


def test_configured_slug_service_gets_omp_driver_acp(monkeypatch):
    """A slug listed in the deployment config must get OMP_DRIVER=acp."""
    _configure_acp(monkeypatch)
    result = _rewrite_compose(_COMPOSE, image_overrides={})
    block = _extract_service_block(result, ACP_SLUG)
    assert "- OMP_DRIVER=acp" in block


def test_other_omp_agent_stays_without_omp_driver(monkeypatch):
    """Sabotage-probe: a second omp agent must NOT get any OMP_DRIVER entry —
    the bridge default (native) must stay intact for the whole unlisted
    fleet."""
    _configure_acp(monkeypatch)
    result = _rewrite_compose(_COMPOSE, image_overrides={})
    block = _extract_service_block(result, OTHER_OMP_SLUG)
    assert "OMP_DRIVER" not in block


def test_non_omp_agent_gets_no_omp_driver(monkeypatch):
    """A claude-image agent must not get an OMP_DRIVER entry."""
    _configure_acp(monkeypatch)
    result = _rewrite_compose(_COMPOSE, image_overrides={})
    block = _extract_service_block(result, NON_OMP_SLUG)
    assert "OMP_DRIVER" not in block


def test_empty_config_means_no_override_anywhere(monkeypatch):
    """Default deployment config (empty list) → the whole fleet stays native;
    not a single service gets an OMP_DRIVER entry."""
    _configure_acp(monkeypatch, "")
    result = _rewrite_compose(_COMPOSE, image_overrides={})
    for slug in (ACP_SLUG, OTHER_OMP_SLUG, NON_OMP_SLUG):
        assert "OMP_DRIVER" not in _extract_service_block(result, slug)


def test_multiple_slugs_config(monkeypatch):
    """A comma-separated list covers every listed slug and nobody else."""
    _configure_acp(monkeypatch, f"{ACP_SLUG}, {OTHER_OMP_SLUG}")
    result = _rewrite_compose(_COMPOSE, image_overrides={})
    assert "- OMP_DRIVER=acp" in _extract_service_block(result, ACP_SLUG)
    assert "- OMP_DRIVER=acp" in _extract_service_block(result, OTHER_OMP_SLUG)
    assert "OMP_DRIVER" not in _extract_service_block(result, NON_OMP_SLUG)


# ── idempotency + rollback survival ───────────────────────────────────────────


def test_rewrite_compose_is_idempotent_for_omp_driver(monkeypatch):
    """Running _rewrite_compose twice must not duplicate the env entry."""
    _configure_acp(monkeypatch)
    once = _rewrite_compose(_COMPOSE, image_overrides={})
    twice = _rewrite_compose(once, image_overrides={})
    assert once == twice
    assert twice.count("OMP_DRIVER=acp") == 1


def test_existing_native_rollback_survives_re_rendering(monkeypatch):
    """A deliberate per-service OMP_DRIVER=native (ADR-081 Rueckweg) must
    survive re-rendering — no second entry, value unchanged."""
    _configure_acp(monkeypatch)
    with_native = _COMPOSE.replace(
        f"      - AGENT_NAME={ACP_SLUG}",
        f"      - AGENT_NAME={ACP_SLUG}\n      - OMP_DRIVER=native",
    )
    result = _rewrite_compose(with_native, image_overrides={})
    block = _extract_service_block(result, ACP_SLUG)
    assert block.count("OMP_DRIVER=") == 1
    assert "OMP_DRIVER=native" in block


def test_service_without_environment_block_gets_one(monkeypatch):
    """A configured service body without an environment block must get a
    fresh one containing the entry."""
    _configure_acp(monkeypatch)
    bare = textwrap.dedent(
        f"""\
        x-omp-agent-base: &omp-agent-base
          image: mc-omp-agent:latest

        services:
          mc-agent-{ACP_SLUG}:
            <<: *omp-agent-base
            container_name: mc-agent-{ACP_SLUG}
        """
    )
    result = _rewrite_compose(bare, image_overrides={})
    block = _extract_service_block(result, ACP_SLUG)
    assert "    environment:" in block
    assert "- OMP_DRIVER=acp" in block


# ── new-agent append path ─────────────────────────────────────────────────────


def test_new_configured_agent_block_gets_driver(monkeypatch):
    """A brand-new cli-bridge agent with a configured slug (not present in
    the static file) must get the driver via the append path too."""
    _configure_acp(monkeypatch)
    block = _build_new_agent_block(
        ACP_SLUG, "mc-omp-agent:latest", is_vault_writer=False
    )
    assert "- OMP_DRIVER=acp" in block


def test_new_unlisted_omp_agent_block_gets_no_driver(monkeypatch):
    _configure_acp(monkeypatch)
    block = _build_new_agent_block(
        "other-omp", "mc-omp-agent:latest", is_vault_writer=False
    )
    assert "OMP_DRIVER" not in block


def test_openclaude_new_agent_block_gets_no_driver(monkeypatch):
    _configure_acp(monkeypatch)
    block = _build_new_agent_block("newagent", OPENCLAUDE_IMAGE, is_vault_writer=False)
    assert "OMP_DRIVER" not in block


# ── _ensure_agent_env_overrides unit cases ────────────────────────────────────


class TestEnsureAgentEnvOverrides:
    def test_inserts_before_next_block(self, monkeypatch):
        _configure_acp(monkeypatch)
        body = [
            "    environment:",
            "      - AGENT_NAME=foo",
            "    volumes:",
            "      - x:/workspace",
        ]
        out = _ensure_agent_env_overrides(body, ACP_SLUG)
        assert out.index("      - OMP_DRIVER=acp") < out.index("    volumes:")

    def test_idempotent_keeps_existing_value(self, monkeypatch):
        _configure_acp(monkeypatch)
        body = ["    environment:", "      - OMP_DRIVER=native"]
        assert _ensure_agent_env_overrides(body, ACP_SLUG) == body

    def test_unlisted_slug_is_noop(self, monkeypatch):
        _configure_acp(monkeypatch)
        body = ["    environment:", "      - AGENT_NAME=foo"]
        assert _ensure_agent_env_overrides(body, "someone-else") == body

    def test_creates_environment_block_when_missing(self, monkeypatch):
        _configure_acp(monkeypatch)
        body = ["    restart: unless-stopped"]
        out = _ensure_agent_env_overrides(body, ACP_SLUG)
        assert "    environment:" in out
        assert "      - OMP_DRIVER=acp" in out
