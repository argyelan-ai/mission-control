"""Verify vault volume mount + env vars survive a compose render.

Post-M.3: cli-bridge agents with the vault:write scope get a vault mount plus
AGENT_VAULT_* env vars. These tests cover preservation, idempotency and
slug-anchoring.

Fixture, not the real file: docker/docker-compose.agents.yml describes the
operator's own fleet and is no longer in version control. On a fresh clone it
is absent — these tests then SKIPPED and the lost coverage was invisible.
After start-all.sh it exists but is EMPTY, and the old helpers died on it
(_load_compose turned `services: {}` into `services: {__`; the block
extractor raised AssertionError). A fixture right here is both honest and
independent of what happens to exist on disk.
"""
from __future__ import annotations

import re
import textwrap

import yaml

from app.services.compose_renderer import _rewrite_compose


# ── Fixture: two agents, one with hand-edited vault entries ──────────────────

HAND_EDITED_COMPOSE = textwrap.dedent("""\
    x-claude-agent-base: &claude-agent-base
      image: mc-claude-agent:latest
      restart: unless-stopped
      networks:
        - mission-control_default

    x-openclaude-agent-base: &openclaude-agent-base
      image: mc-agent-base:latest
      restart: unless-stopped
      networks:
        - mission-control_default

    services:
      mc-agent-alpha:
        <<: *openclaude-agent-base
        container_name: mc-agent-alpha
        environment:
          - AGENT_NAME=alpha
          - MC_TOKEN=${MC_TOKEN_ALPHA}
          - AGENT_VAULT_PATH=/vault/agents/alpha
          - AGENT_VAULT_INBOX=/vault/_inbox
          - AGENT_SLUG=alpha
        volumes:
          - ${HOME}/.mc/agents/alpha/claude-config:/home/agent/.claude
          - ${HOME}/.mc/vault:/vault:rw

      mc-agent-beta:
        <<: *claude-agent-base
        container_name: mc-agent-beta
        environment:
          - AGENT_NAME=beta
        volumes:
          - ${HOME}/.mc/agents/beta/claude-config:/home/agent/.claude

    networks:
      mission-control_default:
        external: true
    """)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _load_compose(raw: str) -> dict:
    """Parse compose YAML, expanding ${HOME} so yaml.safe_load doesn't choke
    on unresolved variables."""
    raw_clean = (
        raw.replace("${HOME}", "/FAKE_HOME")
        .replace("${", "__ENV_")
        .replace("}", "__")
    )
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

def test_hand_edited_vault_mount_survives_a_render():
    """A render with no overrides and no vault_writers must leave an existing
    hand-edited ``/vault:rw`` mount exactly where it is."""
    result = _rewrite_compose(HAND_EDITED_COMPOSE, image_overrides={})

    alpha = _load_compose(result)["services"]["mc-agent-alpha"]
    vault_mounts = [v for v in alpha.get("volumes", []) if "/vault" in str(v)]
    assert vault_mounts, "the /vault mount was dropped by the render"
    assert any(":rw" in str(v) for v in vault_mounts), (
        f"the vault mount must stay :rw, got: {vault_mounts}"
    )


def test_hand_edited_vault_env_vars_survive_a_render():
    """Same for AGENT_VAULT_PATH / AGENT_VAULT_INBOX / AGENT_SLUG."""
    result = _rewrite_compose(HAND_EDITED_COMPOSE, image_overrides={})
    block = _extract_service_block(result, "alpha")

    assert "AGENT_VAULT_PATH=/vault/agents/alpha" in block
    assert "AGENT_VAULT_INBOX=/vault/_inbox" in block
    assert "AGENT_SLUG=alpha" in block


def test_agent_without_the_scope_gets_no_vault_entries():
    """The counterpart: an agent NOT in ``vault_writers`` must not gain vault
    entries out of nowhere — injection is scope-driven, not blanket."""
    result = _rewrite_compose(HAND_EDITED_COMPOSE, image_overrides={})
    block = _extract_service_block(result, "beta")

    assert "/vault:rw" not in block
    assert "AGENT_VAULT_PATH" not in block


# ── New M.3 contract tests ───────────────────────────────────────────────────



def test_renderer_idempotent_when_vault_entries_already_present():
    """Running the renderer twice over the same set of vault_writers produces
    byte-identical output (idempotency)."""
    writers = {"alpha", "beta"}

    pass1 = _rewrite_compose(HAND_EDITED_COMPOSE, image_overrides={}, vault_writers=writers)
    pass2 = _rewrite_compose(pass1, image_overrides={}, vault_writers=writers)
    assert pass1 == pass2, "renderer is not idempotent across two passes"


def test_renderer_does_not_duplicate_entries_it_already_finds():
    """Alpha already carries the entries (hand-edited). With alpha in
    ``vault_writers``, the renderer must NOT add a second copy."""
    out = _rewrite_compose(
        HAND_EDITED_COMPOSE, image_overrides={}, vault_writers={"alpha"}
    )
    block = _extract_service_block(out, "alpha")

    # Exactly one occurrence of each entry — no duplicates.
    assert block.count("AGENT_VAULT_PATH=/vault/agents/alpha") == 1
    assert block.count("AGENT_VAULT_INBOX=/vault/_inbox") == 1
    assert block.count("AGENT_SLUG=alpha") == 1
    assert block.count("/vault:rw") == 1


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


