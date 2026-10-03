"""Archiving agents must never leave an invalid agents compose file behind.

Incident (2026-10-03): several cli-bridge agents were archived one after the
other. Each archive prunes the agent's ``mc-agent-<slug>:`` block from the
private docker-compose.agents.yml. Afterwards the file was invalid YAML: five
lines of one removed block (volume entries plus ``env_file:``) were left
directly under a section comment, in front of the next service, and every
``docker compose up`` failed with "did not find expected key".

Root cause, two parts:

1. ``_rewrite_compose`` collects a service body up to the next
   ``mc-agent-*`` line, so the section comment of the NEXT service
   (``  # ── Section ──``, 2-space indent) ended up inside the body, and
   ``_find_block_range`` did not stop at it. Entries the renderer appended to
   ``volumes:`` landed BELOW that comment. Still valid YAML (comments are
   invisible to the parser), so nobody noticed.
2. ``prune_compose_agent_block`` ended a block at the first line with an
   indent of two or less — including that comment. Everything after the
   comment survived the prune as orphan lines.

Fixtures use the real file's shape (header comments, ``x-*`` anchors with
comments inside, ``<<:`` merges, section comments between services, a block
whose last lines are volumes and ``env_file``) with neutral names.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from app.models.agent import Agent
from app.services import agent_lifecycle, compose_renderer
from app.services.compose_renderer import (
    ComposeValidationError,
    _find_block_range,
    _rewrite_compose,
    prune_compose_agent,
    prune_compose_agent_block,
    render_compose_agents,
    write_compose_agents,
)


@pytest.fixture(autouse=True)
def _patch_compose_redis(fake_redis):
    async def _get_redis():
        return fake_redis
    with patch("app.services.compose_renderer.get_redis", _get_redis):
        yield


HEADER = """\
# docker/docker-compose.agents.yml
# Fleet overview — comment block at the top, as in the real file.
#   /workspace   rw  -> per-agent work zone

# Claude base
x-claude-agent-base: &claude-agent-base
  image: mc-claude-agent:latest
  restart: unless-stopped
  stop_grace_period: 20s
  networks:
    - mission-control_default
  # env_file always loads docker/.env.shared — comment INSIDE an anchor
  # at 2-space indent, followed by another key of the anchor.
  env_file:
    - docker/.env.shared

# OpenClaude base
x-openclaude-agent-base: &openclaude-agent-base
  image: mc-agent-base:latest
  restart: unless-stopped
  stop_grace_period: 20s
  networks:
    - mission-control_default
  env_file:
    - docker/.env.shared
  environment:
    - CLAUDE_CODE_USE_OPENAI=1

services:
  # ── Claude fleet ─────────
"""


def _claude_block(slug: str, extra_volume_comment: bool = False) -> str:
    extra = ""
    if extra_volume_comment:
        extra = (
            "      # Tool credentials — handed through read-only.\n"
            "      # Second comment line inside the volume list.\n"
            "      - ${HOME}/.config/tool:/home/agent/.config/tool:ro\n"
        )
    return (
        f"  mc-agent-{slug}:\n"
        "    <<: *claude-agent-base\n"
        "    image: mc-claude-agent:latest\n"
        f"    container_name: mc-agent-{slug}\n"
        "    environment:\n"
        f"      - AGENT_NAME={slug}\n"
        "      - MC_API_URL=${MC_API_URL:-http://backend:8000}\n"
        f"    # No /workspace-ref for {slug} — comment between list items.\n"
        f"      - AGENT_VAULT_PATH=/vault/agents/{slug}\n"
        "      - AGENT_VAULT_INBOX=/vault/_inbox\n"
        f"      - AGENT_SLUG={slug}\n"
        "      - MSG_DELIVERY_MODE=${MSG_DELIVERY_MODE:-nudge}\n"
        "    volumes:\n"
        f"      - ${{HOME}}/.mc/agents/{slug}/claude-config:/home/agent/.claude\n"
        f"      - ${{HOME}}/.mc/workspaces/{slug}:/workspace\n"
        f"{extra}"
    )


OMEGA_BLOCK = """\
  mc-agent-omega:
    <<: *openclaude-agent-base
    container_name: mc-agent-omega
    environment:
      - AGENT_NAME=omega
      - AGENT_SLUG=omega
      - MSG_DELIVERY_MODE=${MSG_DELIVERY_MODE:-nudge}
    volumes:
      - ${HOME}/.mc/workspaces/omega:/workspace
      - ${HOME}/.mc/vault:/vault:rw
      - ${HOME}/.mc/references:${HOME}/.mc/references:ro
"""

FOOTER = """\

networks:
  mission-control_default:
    external: true

volumes:
  # Shared volume — comment under a top-level key.
  mc_shared_deliverables:
    external: true
"""

_TAIL = (
    "      - ${HOME}/.mc/vault:/vault:rw\n"
    "      - ${HOME}/.mc/references:${HOME}/.mc/references:ro\n"
    "\n"
    "    env_file:\n"
    "      - docker/.env.shared\n"
)

# The state the live file was in BEFORE the archives: the renderer had
# appended gamma's vault/references mounts and env_file BELOW the section
# comment that introduces omega. Valid YAML — comments are invisible.
MISPLACED = (
    HEADER
    + _claude_block("alpha") + _TAIL
    + _claude_block("beta") + _TAIL
    + _claude_block("gamma", extra_volume_comment=True)
    + "\n"
    + "  # ── Omega section (openclaude) ──\n"
    + "      - ${HOME}/.mc/vault:/vault:rw\n"
    + "      - ${HOME}/.mc/references:${HOME}/.mc/references:ro\n"
    + "    env_file:\n"
    + "      - docker/.env.shared\n"
    + OMEGA_BLOCK
    + FOOTER
)

# The same file as the renderer finds it BEFORE it adds anything: gamma
# ends, then a blank line and omega's section comment.
CLEAN = (
    HEADER
    + _claude_block("alpha") + _TAIL
    + _claude_block("beta") + _TAIL
    + _claude_block("gamma", extra_volume_comment=True)
    + "\n"
    + "  # ── Omega section (openclaude) ──\n"
    + OMEGA_BLOCK
    + FOOTER
)


def _services(text: str) -> dict:
    doc = yaml.safe_load(text)
    assert isinstance(doc, dict)
    services = doc["services"]
    assert isinstance(services, dict)
    for name, body in services.items():
        assert isinstance(body, dict), f"{name} is not a mapping"
    return services


def test_fixtures_are_valid_yaml():
    assert set(_services(MISPLACED)) == {
        "mc-agent-alpha", "mc-agent-beta", "mc-agent-gamma", "mc-agent-omega",
    }
    assert set(_services(CLEAN)) == set(_services(MISPLACED))


# ── Part 2: prune stops at the section comment ─────────────────────────────

def test_prune_block_with_section_comment_inside_leaves_no_orphans():
    out, removed = prune_compose_agent_block(MISPLACED, "gamma")
    assert removed is True
    services = _services(out)  # the live failure: this raised a ScannerError
    assert set(services) == {"mc-agent-alpha", "mc-agent-beta", "mc-agent-omega"}
    assert "gamma" not in out
    # Omega keeps its own data untouched.
    assert services["mc-agent-omega"]["environment"][0] == "AGENT_NAME=omega"
    # Old code: the orphans silently extended beta's env_file list (valid
    # YAML, wrong content) — the neighbour must keep exactly its own list.
    assert services["mc-agent-beta"]["env_file"] == ["docker/.env.shared"]
    # The section comment introduces omega and survives the prune.
    assert "  # ── Omega section (openclaude) ──\n  mc-agent-omega:" in out


def test_prune_several_adjacent_blocks_in_sequence():
    """The live run archived several agents in a row — every intermediate
    file must stay valid, and each step removes exactly one service."""
    text = MISPLACED
    expected = {"mc-agent-alpha", "mc-agent-beta", "mc-agent-gamma", "mc-agent-omega"}
    for slug in ("beta", "gamma", "alpha", "omega"):
        text, removed = prune_compose_agent_block(text, slug)
        assert removed is True
        expected.discard(f"mc-agent-{slug}")
        doc = yaml.safe_load(text)
        assert set(doc["services"] or {}) == expected
        assert slug not in text.split("\nnetworks:")[0].split("services:")[1]
    # Last agent gone → empty mapping, anchors and footer intact.
    doc = yaml.safe_load(text)
    assert doc["services"] == {}
    assert "x-claude-agent-base" in doc and "networks" in doc and "volumes" in doc


def test_prune_last_service_keeps_top_level_comment_and_keys():
    out, removed = prune_compose_agent_block(MISPLACED, "omega")
    assert removed is True
    doc = yaml.safe_load(out)
    assert set(doc["services"]) == {"mc-agent-alpha", "mc-agent-beta", "mc-agent-gamma"}
    assert doc["volumes"] == {"mc_shared_deliverables": {"external": True}}


# ── Part 1: the renderer never appends below the next section comment ─────

def test_rewrite_keeps_appended_entries_above_next_section_comment():
    rendered = _rewrite_compose(CLEAN, {}, vault_writers={"gamma"})
    gamma_start = rendered.index("  mc-agent-gamma:")
    comment = rendered.index("  # ── Omega section (openclaude) ──")
    omega_start = rendered.index("  mc-agent-omega:")
    between = rendered[comment:omega_start]
    # Nothing but the comment line between the comment and omega.
    assert between == "  # ── Omega section (openclaude) ──\n"
    gamma_body = rendered[gamma_start:comment]
    assert "/.mc/vault:/vault:rw" in gamma_body
    assert "/.mc/references:" in gamma_body
    services = _services(rendered)
    assert "${HOME}/.mc/vault:/vault:rw" in services["mc-agent-gamma"]["volumes"]


def test_rewrite_new_key_block_lands_above_next_section_comment():
    """A service WITHOUT a volumes block gets a fresh one appended — that
    append must land inside the service, not below the next section comment."""
    no_volumes = CLEAN.replace(
        "    volumes:\n"
        "      - ${HOME}/.mc/agents/gamma/claude-config:/home/agent/.claude\n"
        "      - ${HOME}/.mc/workspaces/gamma:/workspace\n"
        "      # Tool credentials — handed through read-only.\n"
        "      # Second comment line inside the volume list.\n"
        "      - ${HOME}/.config/tool:/home/agent/.config/tool:ro\n",
        "",
    )
    assert "claude-config:/home/agent/.claude\n" not in no_volumes.split("mc-agent-gamma:")[1].split("mc-agent-omega")[0]
    rendered = _rewrite_compose(no_volumes, {}, vault_writers=set())
    comment = rendered.index("  # ── Omega section (openclaude) ──")
    omega_start = rendered.index("  mc-agent-omega:")
    assert rendered[comment:omega_start] == "  # ── Omega section (openclaude) ──\n"
    services = _services(rendered)
    assert services["mc-agent-gamma"]["volumes"] == [
        "${HOME}/.mc/references:${HOME}/.mc/references:ro"
    ]


def test_find_block_range_stops_at_section_comment():
    body = [
        "    volumes:",
        "      - a:/a",
        "",
        "  # ── Next section ──",
        "      - b:/b",
    ]
    assert _find_block_range(body, "volumes") == (0, 2)


def test_rewrite_is_idempotent_on_real_shape():
    once = _rewrite_compose(CLEAN, {}, vault_writers={"alpha", "beta", "gamma"})
    twice = _rewrite_compose(once, {}, vault_writers={"alpha", "beta", "gamma"})
    assert once == twice


def test_rewrite_then_prune_round_trip_stays_valid():
    """The live sequence: renders add mounts, later the agent is archived."""
    rendered = _rewrite_compose(CLEAN, {}, vault_writers={"alpha", "beta", "gamma"})
    for slug in ("gamma", "beta"):
        rendered, removed = prune_compose_agent_block(rendered, slug)
        assert removed is True
    assert set(_services(rendered)) == {"mc-agent-alpha", "mc-agent-omega"}


# ── Guard: never write an invalid file ─────────────────────────────────────

@pytest.fixture
def compose_path(tmp_path: Path) -> Path:
    p = tmp_path / "docker-compose.agents.yml"
    p.write_text(MISPLACED, encoding="utf-8")
    return p


@pytest.mark.asyncio
async def test_prune_file_real_shape_writes_valid_yaml(compose_path: Path):
    result = await prune_compose_agent("gamma", compose_path=compose_path)
    assert result["changed"] == "true"
    written = compose_path.read_text(encoding="utf-8")
    assert set(_services(written)) == {"mc-agent-alpha", "mc-agent-beta", "mc-agent-omega"}


@pytest.mark.asyncio
async def test_prune_file_refuses_to_write_invalid_yaml(compose_path: Path):
    before = compose_path.read_text(encoding="utf-8")
    bak = compose_path.with_suffix(compose_path.suffix + ".bak")
    bak.write_text("previous backup\n", encoding="utf-8")
    # The live shape: orphan list items + env_file right under a section
    # comment, in front of the next service.
    orphaned = MISPLACED.replace(
        "services:\n  # ── Claude fleet ─────────\n",
        "services:\n  # ── Claude fleet ─────────\n"
        "      - ${HOME}/.mc/vault:/vault:rw\n"
        "    env_file:\n"
        "      - docker/.env.shared\n",
    )
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load(orphaned)

    with patch.object(
        compose_renderer, "prune_compose_agent_block",
        return_value=(orphaned, True),
    ):
        with pytest.raises(ComposeValidationError):
            await prune_compose_agent("gamma", compose_path=compose_path)

    assert compose_path.read_text(encoding="utf-8") == before
    assert bak.read_text(encoding="utf-8") == "previous backup\n"
    rejected = compose_path.with_suffix(compose_path.suffix + ".rejected")
    assert rejected.read_text(encoding="utf-8") == orphaned


@pytest.mark.asyncio
async def test_prune_file_refuses_when_a_sibling_service_disappears(compose_path: Path):
    """Valid YAML is not enough — a prune that swallows a neighbour is refused."""
    before = compose_path.read_text(encoding="utf-8")
    swallowed, _ = prune_compose_agent_block(MISPLACED, "omega")
    swallowed, _ = prune_compose_agent_block(swallowed, "gamma")
    with patch.object(
        compose_renderer, "prune_compose_agent_block",
        return_value=(swallowed, True),
    ):
        with pytest.raises(ComposeValidationError):
            await prune_compose_agent("gamma", compose_path=compose_path)
    assert compose_path.read_text(encoding="utf-8") == before


@pytest.mark.asyncio
async def test_write_compose_refuses_invalid_render(session, compose_path: Path):
    before = compose_path.read_text(encoding="utf-8")

    async def _bad_render(_session, compose_path=None):
        return "services:\n  mc-agent-x:\n    image: a\n      - orphan\n"

    with patch.object(compose_renderer, "render_compose_agents", _bad_render):
        with pytest.raises(ComposeValidationError):
            await write_compose_agents(session, compose_path=compose_path)
    assert compose_path.read_text(encoding="utf-8") == before
    assert not compose_path.with_suffix(compose_path.suffix + ".bak").exists()


# ── Archive / restore cycle ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_render_does_not_resurrect_archived_agent(session, compose_path: Path):
    """After archive pruned the block, the next render (any runtime switch)
    must not append it again — otherwise start-all.sh brings it back up."""
    from app.utils import utcnow

    pruned, _ = prune_compose_agent_block(MISPLACED, "gamma")
    compose_path.write_text(pruned, encoding="utf-8")
    session.add(Agent(name="gamma", slug="gamma", agent_runtime="cli-bridge",
                      archived_at=utcnow()))
    await session.commit()

    rendered = await render_compose_agents(session, compose_path=compose_path)
    assert "mc-agent-gamma" not in rendered
    _services(rendered)


@pytest.mark.asyncio
async def test_archive_then_restore_re_adds_a_valid_block(session, compose_path: Path):
    agent = Agent(name="gamma", slug="gamma", agent_runtime="cli-bridge")
    session.add(agent)
    await session.commit()

    with patch.object(compose_renderer, "DEFAULT_COMPOSE_PATH", compose_path), \
         patch.object(agent_lifecycle.docker_agent_sync, "stop_docker_agent_container",
                      return_value={"ok": "true"}), \
         patch.object(agent_lifecycle.docker_agent_sync, "ensure_agent_container_started",
                      return_value={"ok": "true"}) as start:
        await agent_lifecycle.archive_agent(session, agent)
        after_archive = compose_path.read_text(encoding="utf-8")
        assert "mc-agent-gamma" not in _services(after_archive)

        await agent_lifecycle.restore_agent(session, agent)

    start.assert_called_once()
    restored = compose_path.read_text(encoding="utf-8")
    services = _services(restored)
    assert "mc-agent-gamma" in services
    assert services["mc-agent-gamma"]["container_name"] == "mc-agent-gamma"
    assert {"mc-agent-alpha", "mc-agent-beta", "mc-agent-omega"} <= set(services)
    await session.refresh(agent)
    assert agent.archived_at is None
