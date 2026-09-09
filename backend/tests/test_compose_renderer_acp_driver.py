"""Verify the per-agent OMP_DRIVER=acp env injection (ADR-081 "Protokoll vor Pane").

The omp bridge defaults to the native TUI driver (``_omp_driver()`` reads
OMP_DRIVER, default ``native``); the ACP protocol path (``omp acp``,
docker/omp-bridge/acp_client.py) is selected per container via OMP_DRIVER.
The renderer must inject ``OMP_DRIVER=acp`` ONLY into the service whose slug
is listed in ``_AGENT_ENV_OVERRIDES()`` (currently ``sparky``) — every other
agent (omp fleet included) must stay untouched so the live gate is provably
per-agent. A manually-set OMP_DRIVER entry (the documented rollback to
``native``) survives re-rendering untouched.
"""
from __future__ import annotations

import re
import textwrap

from app.services.compose_renderer import (
    OPENCLAUDE_IMAGE,
    _AGENT_ENV_OVERRIDES,
    _build_new_agent_block,
    _ensure_agent_env_overrides,
    _rewrite_compose,
)


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
    """\
    x-claude-agent-base: &claude-agent-base
      image: mc-claude-agent:latest
      restart: unless-stopped

    x-omp-agent-base: &omp-agent-base
      image: mc-omp-agent:latest
      restart: unless-stopped

    services:
      mc-agent-sparky:
        <<: *omp-agent-base
        container_name: mc-agent-sparky
        environment:
          - AGENT_NAME=sparky

      mc-agent-alpha:
        <<: *omp-agent-base
        container_name: mc-agent-alpha
        environment:
          - AGENT_NAME=alpha

      mc-agent-beta:
        <<: *claude-agent-base
        container_name: mc-agent-beta
        environment:
          - AGENT_NAME=beta
    """
)


# ── sabotage-probe guarantee: per-agent isolation ─────────────────────────────


def test_override_slug_service_gets_omp_driver_acp():
    """The slug listed in _AGENT_ENV_OVERRIDES must get OMP_DRIVER=acp."""
    result = _rewrite_compose(_COMPOSE, image_overrides={})
    slug = next(iter(_AGENT_ENV_OVERRIDES()))
    block = _extract_service_block(result, slug)
    assert "- OMP_DRIVER=acp" in block


def test_other_omp_agent_stays_without_omp_driver():
    """Sabotage-probe: a second omp agent must NOT get any OMP_DRIVER entry —
    the bridge default (native) must stay intact for the whole non-sparky
    fleet."""
    result = _rewrite_compose(_COMPOSE, image_overrides={})
    block = _extract_service_block(result, "alpha")
    assert "OMP_DRIVER" not in block


def test_non_omp_agent_gets_no_omp_driver():
    """Beta (claude image) must not get an OMP_DRIVER entry."""
    result = _rewrite_compose(_COMPOSE, image_overrides={})
    block = _extract_service_block(result, "beta")
    assert "OMP_DRIVER" not in block


# ── idempotency + rollback survival ───────────────────────────────────────────


def test_rewrite_compose_is_idempotent_for_omp_driver():
    """Running _rewrite_compose twice must not duplicate the env entry."""
    once = _rewrite_compose(_COMPOSE, image_overrides={})
    twice = _rewrite_compose(once, image_overrides={})
    assert once == twice
    assert twice.count("OMP_DRIVER=acp") == 1


def test_existing_native_rollback_survives_re_rendering():
    """A deliberate per-service OMP_DRIVER=native (ADR-081 Rueckweg) must
    survive re-rendering — no second entry, value unchanged."""
    with_native = _COMPOSE.replace(
        "      - AGENT_NAME=sparky",
        "      - AGENT_NAME=sparky\n      - OMP_DRIVER=native",
    )
    result = _rewrite_compose(with_native, image_overrides={})
    block = _extract_service_block(result, "sparky")
    assert block.count("OMP_DRIVER=") == 1
    assert "OMP_DRIVER=native" in block


def test_service_without_environment_block_gets_one():
    """An override-slug service body without an environment block must get a
    fresh one containing the entry."""
    slug = next(iter(_AGENT_ENV_OVERRIDES()))
    bare = textwrap.dedent(
        f"""\
        x-omp-agent-base: &omp-agent-base
          image: mc-omp-agent:latest

        services:
          mc-agent-{slug}:
            <<: *omp-agent-base
            container_name: mc-agent-{slug}
        """
    )
    result = _rewrite_compose(bare, image_overrides={})
    block = _extract_service_block(result, slug)
    assert "    environment:" in block
    assert "- OMP_DRIVER=acp" in block


# ── new-agent append path ─────────────────────────────────────────────────────


def test_new_override_slug_agent_block_gets_driver():
    """A brand-new cli-bridge agent with an override slug (not present in the
    static file) must get the driver via the append path too."""
    slug = next(iter(_AGENT_ENV_OVERRIDES()))
    block = _build_new_agent_block(slug, "mc-omp-agent:latest", is_vault_writer=False)
    assert "- OMP_DRIVER=acp" in block


def test_new_non_override_omp_agent_block_gets_no_driver():
    block = _build_new_agent_block("other-omp", "mc-omp-agent:latest", is_vault_writer=False)
    assert "OMP_DRIVER" not in block


def test_openclaude_new_agent_block_gets_no_driver():
    block = _build_new_agent_block("newagent", OPENCLAUDE_IMAGE, is_vault_writer=False)
    assert "OMP_DRIVER" not in block


# ── _ensure_agent_env_overrides unit cases ────────────────────────────────────


class TestEnsureAgentEnvOverrides:
    def test_inserts_before_next_block(self):
        body = [
            "    environment:",
            "      - AGENT_NAME=foo",
            "    volumes:",
            "      - x:/workspace",
        ]
        out = _ensure_agent_env_overrides(body, "sparky")
        assert out.index("      - OMP_DRIVER=acp") < out.index("    volumes:")

    def test_idempotent_keeps_existing_value(self):
        body = ["    environment:", "      - OMP_DRIVER=native"]
        assert _ensure_agent_env_overrides(body, "sparky") == body

    def test_unknown_slug_is_noop(self):
        body = ["    environment:", "      - AGENT_NAME=foo"]
        assert _ensure_agent_env_overrides(body, "someone-else") == body

    def test_creates_environment_block_when_missing(self):
        body = ["    restart: unless-stopped"]
        out = _ensure_agent_env_overrides(body, "sparky")
        assert "    environment:" in out
        assert "      - OMP_DRIVER=acp" in out
