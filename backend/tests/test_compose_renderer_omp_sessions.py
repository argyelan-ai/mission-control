"""Verify the omp session-transcript volume mount (ADR-045) in
docker-compose.agents.yml.

Root cause of the empty model_usage_events for omp/Sparky since 2026-07-05:
omp writes JSONL transcripts inside the container at
/home/agent/.omp/profiles/mc-agent/agent/sessions with NO host mount, so the
transcripts vanished on every container recreate and the token harvester
(which only globbed claude-config/projects) never saw them either way.
"""
from __future__ import annotations

import re
import textwrap

from app.services.compose_renderer import (
    OMP_IMAGE,
    OPENCLAUDE_IMAGE,
    _build_new_agent_block,
    _ensure_omp_sessions_volume,
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


# ── Fixture: an agent with an EXPLICIT omp image line ───────────────────────
#
# Fixture, not the real file: docker/docker-compose.agents.yml describes the
# operator's own fleet and left version control. On a fresh clone it is
# absent — these tests then SKIPPED, and the lost coverage was invisible.
# After start-all.sh it exists but is EMPTY, and _extract_service_block died
# on it with an AssertionError.

_EXPLICIT_IMAGE_COMPOSE = textwrap.dedent(
    """\
    x-claude-agent-base: &claude-agent-base
      image: mc-claude-agent:latest
      restart: unless-stopped

    x-openclaude-agent-base: &openclaude-agent-base
      image: mc-agent-base:latest
      restart: unless-stopped

    services:
      mc-agent-alpha:
        <<: *openclaude-agent-base
        image: mc-omp-agent:latest
        container_name: mc-agent-alpha
        volumes:
          - ${HOME}/.mc/agents/alpha/claude-config:/home/agent/.claude

      mc-agent-beta:
        <<: *claude-agent-base
        container_name: mc-agent-beta
        volumes:
          - ${HOME}/.mc/agents/beta/claude-config:/home/agent/.claude
    """
)


def test_explicit_omp_image_gets_sessions_mount_after_rewrite():
    """Alpha's resolved image is OMP_IMAGE via an explicit `image:` line in
    the static file — _rewrite_compose must add the omp-sessions mount even
    with no DB-sourced image_overrides (idempotent detection from the file)."""
    result = _rewrite_compose(_EXPLICIT_IMAGE_COMPOSE, image_overrides={})
    block = _extract_service_block(result, "alpha")
    assert "/omp-sessions:/home/agent/.omp/profiles/mc-agent/agent/sessions" in block
    assert "${HOME}/.mc/agents/alpha/omp-sessions:" in block


def test_rewrite_compose_is_idempotent_for_omp_mount():
    """Running _rewrite_compose twice must not duplicate the mount line."""
    once = _rewrite_compose(_EXPLICIT_IMAGE_COMPOSE, image_overrides={})
    twice = _rewrite_compose(once, image_overrides={})
    assert once == twice
    assert twice.count("/home/agent/.omp/profiles/mc-agent/agent/sessions") == 1


def test_non_omp_agents_get_no_omp_sessions_mount():
    """Beta (claude image) must not get the omp-sessions mount."""
    result = _rewrite_compose(_EXPLICIT_IMAGE_COMPOSE, image_overrides={})
    block = _extract_service_block(result, "beta")
    assert "omp-sessions" not in block


# ── Synthetic fixtures — override-driven (DB says an agent's image is/was omp) ──

_MINIMAL_COMPOSE = textwrap.dedent(
    """\
    x-claude-agent-base: &claude-agent-base
      image: mc-claude-agent:latest
      restart: unless-stopped

    x-omp-agent-base: &omp-agent-base
      image: mc-omp-agent:latest
      restart: unless-stopped

    services:
      mc-agent-gamma:
        <<: *claude-agent-base
        container_name: mc-agent-gamma
        volumes:
          - ${HOME}/.mc/agents/gamma/claude-config:/home/agent/.claude

      mc-agent-delta:
        <<: *omp-agent-base
        container_name: mc-agent-delta
        volumes:
          - ${HOME}/.mc/agents/delta/claude-config:/home/agent/.claude
    """
)


def test_agent_inheriting_omp_anchor_gets_mount_without_explicit_override():
    """An agent inheriting `*omp-agent-base` (no explicit `image:` line, no
    image_overrides entry) must still get the mount — resolved via the
    anchor's default image, matching the vault/references injection pattern."""
    result = _rewrite_compose(_MINIMAL_COMPOSE, image_overrides={})
    block = _extract_service_block(result, "delta")
    assert "/home/agent/.omp/profiles/mc-agent/agent/sessions" in block
    assert "${HOME}/.mc/agents/delta/omp-sessions:" in block


def test_agent_switched_to_omp_via_override_gets_mount():
    """DB-driven switch to omp (image_overrides) must add the mount even
    though the static anchor was claude-agent-base."""
    result = _rewrite_compose(_MINIMAL_COMPOSE, image_overrides={"gamma": OMP_IMAGE})
    block = _extract_service_block(result, "gamma")
    assert f"image: {OMP_IMAGE}" in block
    assert "/home/agent/.omp/profiles/mc-agent/agent/sessions" in block


def test_agent_switched_away_from_omp_gets_no_mount():
    """DB-driven switch OFF omp to openclaude — the new service block should
    not get the omp mount (mount removal is out of scope, same limitation as
    vault entries, but a freshly-overridden-away agent should never gain one)."""
    result = _rewrite_compose(_MINIMAL_COMPOSE, image_overrides={"delta": OPENCLAUDE_IMAGE})
    block = _extract_service_block(result, "delta")
    assert f"image: {OPENCLAUDE_IMAGE}" in block
    assert "omp-sessions" not in block


# ── _ensure_omp_sessions_volume (unit) ───────────────────────────────────────

class TestEnsureOmpSessionsVolume:
    def test_adds_to_existing_volumes_block(self):
        body = [
            "  mc-agent-foo:",
            "    <<: *omp-agent-base",
            "    volumes:",
            "      - ${HOME}/.mc/workspaces/foo:/workspace",
        ]
        out = _ensure_omp_sessions_volume(body, "foo")
        assert any(
            "${HOME}/.mc/agents/foo/omp-sessions:/home/agent/.omp/profiles/mc-agent/agent/sessions" in l
            for l in out
        )

    def test_creates_volumes_block_when_missing(self):
        body = ["  mc-agent-foo:", "    <<: *omp-agent-base"]
        out = _ensure_omp_sessions_volume(body, "foo")
        assert "    volumes:" in out
        assert any("omp-sessions" in l for l in out)

    def test_idempotent_no_duplicate(self):
        body = [
            "  mc-agent-foo:",
            "    volumes:",
            "      - ${HOME}/.mc/agents/foo/omp-sessions:/home/agent/.omp/profiles/mc-agent/agent/sessions",
        ]
        out = _ensure_omp_sessions_volume(body, "foo")
        assert out == body

    def test_slug_anchored_no_cross_agent_false_positive(self):
        """A body already containing a DIFFERENT agent's omp-sessions marker
        (shouldn't happen in practice, but the marker is slug-anchored) must
        still get its own mount added."""
        body = [
            "  mc-agent-bar:",
            "    volumes:",
            "      - ${HOME}/.mc/agents/other-agent/omp-sessions:/home/agent/.omp/profiles/mc-agent/agent/sessions",
        ]
        out = _ensure_omp_sessions_volume(body, "bar")
        assert any("agents/bar/omp-sessions" in l for l in out)


# ── _build_new_agent_block ────────────────────────────────────────────────────

class TestBuildNewAgentBlockOmp:
    def test_new_omp_agent_gets_mount(self):
        block = _build_new_agent_block("newagent", OMP_IMAGE, is_vault_writer=False)
        assert "/home/agent/.omp/profiles/mc-agent/agent/sessions" in block
        assert "${HOME}/.mc/agents/newagent/omp-sessions:" in block

    def test_new_claude_agent_gets_no_mount(self):
        block = _build_new_agent_block("newagent", "mc-claude-agent:latest", is_vault_writer=False)
        assert "omp-sessions" not in block

    def test_new_openclaude_agent_gets_no_mount(self):
        block = _build_new_agent_block("newagent", OPENCLAUDE_IMAGE, is_vault_writer=False)
        assert "omp-sessions" not in block
