"""Verify vault volume mount + env vars in docker-compose.agents.yml.

Post-M.3: all cli-bridge agents have vault:read+vault:write scopes and
vault mounts. Tests verify preservation, idempotency, and slug-anchoring.
"""
from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest
import yaml

from app.services.compose_renderer import _rewrite_compose

# Path to the actual compose file relative to this test file's location.
# Tests run from backend/ so we navigate up one level.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
COMPOSE_PATH = _REPO_ROOT / "docker" / "docker-compose.agents.yml"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _load_compose() -> dict:
    """Parse docker-compose.agents.yml, expanding ${HOME} so yaml.safe_load
    doesn't choke on unresolved variables."""
    raw = COMPOSE_PATH.read_text(encoding="utf-8")
    raw_clean = raw.replace("${HOME}", "/FAKE_HOME").replace("${", "__ENV_").replace("}", "__")
    return yaml.safe_load(raw_clean)


def _extract_service_block(content: str, slug: str) -> str:
    """Return the raw text of a single ``mc-agent-<slug>:`` service block."""
    pattern = re.compile(
        rf"(^  mc-agent-{re.escape(slug)}:\s*$.*?)(?=^  mc-agent-|^[a-zA-Z]|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(content)
    if not m:
        raise AssertionError(f"service block mc-agent-{slug} not found")
    return m.group(1)


# ── Tests preserving M.2 contracts ───────────────────────────────────────────

@pytest.mark.skipif(not COMPOSE_PATH.exists(), reason="docker-compose.agents.yml not found")
def test_sparky_has_vault_volume_mount():
    """mc-agent-sparky must have /FAKE_HOME/.mc/vault:/vault:rw volume."""
    data = _load_compose()
    sparky = data["services"]["mc-agent-sparky"]
    volumes = sparky.get("volumes", [])
    vault_mounts = [v for v in volumes if "/vault" in str(v)]
    assert vault_mounts, "Sparky has no /vault volume mount"
    assert any(":rw" in str(v) for v in vault_mounts), (
        f"Sparky vault mount should be :rw, got: {vault_mounts}"
    )


@pytest.mark.skipif(not COMPOSE_PATH.exists(), reason="docker-compose.agents.yml not found")
def test_sparky_has_vault_env_vars():
    """mc-agent-sparky must have AGENT_VAULT_PATH, AGENT_VAULT_INBOX, AGENT_SLUG."""
    raw = COMPOSE_PATH.read_text(encoding="utf-8")
    block = _extract_service_block(raw, "sparky")

    assert "AGENT_VAULT_PATH=/vault/agents/sparky" in block
    assert "AGENT_VAULT_INBOX=/vault/_inbox" in block
    assert "AGENT_SLUG=sparky" in block


@pytest.mark.skipif(not COMPOSE_PATH.exists(), reason="docker-compose.agents.yml not found")
def test_compose_renderer_preserves_vault_entries():
    """Running ``_rewrite_compose()`` with no overrides + no vault_writers must
    leave Sparky's existing hand-edited vault entries intact."""
    raw = COMPOSE_PATH.read_text(encoding="utf-8")
    result = _rewrite_compose(raw, image_overrides={})

    assert "AGENT_VAULT_PATH=/vault/agents/sparky" in result
    assert "AGENT_VAULT_INBOX=/vault/_inbox" in result
    assert "/vault:rw" in result


# ── New M.3 contract tests ───────────────────────────────────────────────────



@pytest.mark.skipif(not COMPOSE_PATH.exists(), reason="docker-compose.agents.yml not found")
def test_renderer_idempotent_when_vault_entries_already_present():
    """Running the renderer twice over the same set of vault_writers produces
    byte-identical output (idempotency)."""
    raw = COMPOSE_PATH.read_text(encoding="utf-8")
    writers = {"sparky", "rex", "tester"}

    pass1 = _rewrite_compose(raw, image_overrides={}, vault_writers=writers)
    pass2 = _rewrite_compose(pass1, image_overrides={}, vault_writers=writers)
    assert pass1 == pass2, "renderer is not idempotent across two passes"


@pytest.mark.skipif(not COMPOSE_PATH.exists(), reason="docker-compose.agents.yml not found")
def test_renderer_preserves_sparky_when_sparky_in_vault_writers():
    """Sparky already has the entries (hand-edited in M.2). When sparky is in
    ``vault_writers``, the renderer must NOT duplicate them."""
    raw = COMPOSE_PATH.read_text(encoding="utf-8")
    out = _rewrite_compose(raw, image_overrides={}, vault_writers={"sparky"})
    sparky_block = _extract_service_block(out, "sparky")

    # Exactly one occurrence of each entry — no duplicates.
    assert sparky_block.count("AGENT_VAULT_PATH=/vault/agents/sparky") == 1
    assert sparky_block.count("AGENT_VAULT_INBOX=/vault/_inbox") == 1
    assert sparky_block.count("AGENT_SLUG=sparky") == 1
    assert sparky_block.count("/vault:rw") == 1


def test_renderer_anchors_slug_match_no_prefix_shadowing():
    """A service body that already contains AGENT_SLUG=neo-planner must NOT
    suppress injection for slug ``neo`` — the marker ``- AGENT_SLUG=neo`` must
    not be treated as a prefix-match against ``- AGENT_SLUG=neo-planner``.

    Uses a synthetic compose snippet (no real file needed) so the test is
    self-contained and independent of which services exist on disk.
    """
    # Minimal synthetic compose with mc-agent-neo already having a longer slug.
    synthetic = textwrap.dedent("""\
        services:
          mc-agent-neo:
            <<: *claude-agent-base
            environment:
              - AGENT_SLUG=neo-planner
    """)

    out = _rewrite_compose(synthetic, image_overrides={}, vault_writers={"neo"})
    neo_block = _extract_service_block(out, "neo")

    # AGENT_SLUG=neo must be present as its own exact line item.
    lines = [l.strip() for l in neo_block.splitlines()]
    assert "- AGENT_SLUG=neo" in lines, (
        f"AGENT_SLUG=neo was not injected (prefix-shadow bug?); neo block:\n{neo_block}"
    )
    # The longer slug entry must be preserved untouched.
    assert "- AGENT_SLUG=neo-planner" in lines, (
        "AGENT_SLUG=neo-planner was unexpectedly removed"
    )
    assert "- AGENT_VAULT_PATH=/vault/agents/neo" in lines


