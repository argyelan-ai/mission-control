"""Parity test: agent anchors vs. the shipped example template (Task
18288efd, follow-up to #573).

#573 added ``stop_grace_period: 20s`` to every agent anchor
(``x-claude-agent-base``, ``x-openclaude-agent-base``, ``x-omp-agent-base``,
``x-kimi-agent-base``) in docker-compose.agents.example.yml — but
``_rewrite_compose`` deliberately leaves anchor BLOCKS untouched (see its
docstring: "Anchor blocks themselves are untouched — they remain the static
base"). None of the four call sites that write a private
docker-compose.agents.yml (runtime-switch, agent create/delete, provisioning)
ever patch an anchor body either. A private file written before #573 —
which is every operator's file, since it's gitignored and only ever created
once by setup.sh or bootstrapped from the template — therefore keeps a
stale, key-less anchor forever. Every agent on it inherits Docker Desktop's
1s StopTimeout default and dies by SIGKILL on every stop, measured live
(including containers freshly recreated from the #573 image, which changed
the IMAGE but not the already-written compose FILE).

This test proves ``render_compose_agents`` backfills the missing key into a
stale anchor and — per the task card — compares the anchor KEYS generally,
not just ``stop_grace_period`` in isolation, against the real shipped
template (``docker/docker-compose.agents.example.yml``, not a hand copy —
same reasoning as test_compose_renderer_empty_template.py: a copy in the
test drifts from what people actually download).

What is compared and what deliberately is not:
  - Key NAMES per anchor (image, restart, stop_grace_period, networks,
    env_file, environment) — compared as a set.
  - ``image`` is excluded from the "must match" set: a private fleet's own
    ``MC_AGENT_IMAGE_PREFIX``/tag choice, or a legacy bare image name
    predating the registry prefix, is allowed to diverge from the template's
    value (see ``_needs_explicit_image``) — only the fact that an ``image:``
    key exists at all is asserted, not its value.
  - ``stop_grace_period``'s VALUE is compared exactly (not just the key) —
    that is the actual bug this closes: the private file's value must
    converge to the template's value ("20s"), not just gain some key.
  - Per-service bodies are NOT touched by the new anchor patch — proven
    separately by asserting a hand-maintained service's #524-class mounts
    (/workspace-ref, /shared-deliverables, /shared-mcp) survive a render
    unchanged. This is the guard the task's Nachtrag (c) asks for: the
    anchor patch must not become a second, less careful rewrite path that
    reopens the #524 mount-loss class while patching stop_grace_period.
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.compose_renderer import render_compose_agents

# Geprueft wird die ECHTE ausgelieferte Vorlage, keine Handkopie (siehe
# test_compose_renderer_empty_template.py fuer die Begruendung).
_REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_TEMPLATE_PATH = _REPO_ROOT / "docker" / "docker-compose.agents.example.yml"
EXAMPLE_TEMPLATE = EXAMPLE_TEMPLATE_PATH.read_text(encoding="utf-8")

_ANCHOR_HEADER_RE = re.compile(r"^x-[a-z0-9_-]+:\s*&(?P<anchor>[a-z0-9_-]+)\s*$")
_ANCHOR_TOP_KEY_RE = re.compile(r"^  ([a-zA-Z_][a-zA-Z0-9_-]*):")

ANCHOR_NAMES = (
    "claude-agent-base",
    "openclaude-agent-base",
    "omp-agent-base",
    "kimi-agent-base",
)


def _anchor_key_sets(content: str) -> dict[str, set[str]]:
    """anchor name -> set of top-level keys declared directly in its body."""
    lines = content.splitlines()
    n = len(lines)
    keys: dict[str, set[str]] = {}
    i = 0
    while i < n:
        m = _ANCHOR_HEADER_RE.match(lines[i])
        if not m:
            i += 1
            continue
        anchor = m.group("anchor")
        i += 1
        found: set[str] = set()
        while i < n:
            cur = lines[i]
            if not cur.strip() or not cur.startswith(" "):
                break
            km = _ANCHOR_TOP_KEY_RE.match(cur)
            if km:
                found.add(km.group(1))
            i += 1
        keys[anchor] = found
    return keys


def _anchor_stop_grace_period(content: str, anchor: str) -> str | None:
    lines = content.splitlines()
    n = len(lines)
    i = 0
    while i < n:
        m = _ANCHOR_HEADER_RE.match(lines[i])
        if m and m.group("anchor") == anchor:
            i += 1
            while i < n:
                cur = lines[i]
                if not cur.strip() or not cur.startswith(" "):
                    return None
                sm = re.match(r"^\s*stop_grace_period:\s*(\S+)", cur)
                if sm:
                    return sm.group(1)
                i += 1
            return None
        i += 1
    return None


# A private docker-compose.agents.yml written BEFORE #573: anchors have
# image/restart/networks/env_file(/environment for openclaude) but no
# stop_grace_period anywhere — matching what #573's own diff shows it added
# to docker-compose.agents.example.yml (13 new lines, all stop_grace_period-
# related). rex's service body carries the three #524-class mounts by hand,
# the way a real pre-existing agent does, to prove the new anchor patch
# doesn't touch service bodies.
STALE_PRIVATE_FIXTURE = """\
# docker/docker-compose.agents.yml — pre-#573 private fleet file (stale
# fixture, hand-maintained by the operator via the UI over many renders).

x-claude-agent-base: &claude-agent-base
  image: "${MC_AGENT_IMAGE_PREFIX-ghcr.io/argyelan-ai/}mc-claude-agent:${MC_AGENT_IMAGE_TAG:-latest}"
  restart: unless-stopped
  networks:
    - mission-control_default
  env_file:
    - docker/.env.shared

x-openclaude-agent-base: &openclaude-agent-base
  image: "${MC_AGENT_IMAGE_PREFIX-ghcr.io/argyelan-ai/}mc-agent-base:${MC_AGENT_IMAGE_TAG:-latest}"
  restart: unless-stopped
  networks:
    - mission-control_default
  env_file:
    - docker/.env.shared
  environment:
    - CLAUDE_CODE_USE_OPENAI=1
    - OLLAMA_API_KEY=${OLLAMA_API_KEY:-}

x-omp-agent-base: &omp-agent-base
  image: mc-omp-agent:latest
  restart: unless-stopped
  networks:
    - mission-control_default
  env_file:
    - docker/.env.shared

x-kimi-agent-base: &kimi-agent-base
  image: mc-kimi-agent:latest
  restart: unless-stopped
  networks:
    - mission-control_default
  env_file:
    - docker/.env.shared

services:
  mc-agent-rex:
    <<: *claude-agent-base
    container_name: mc-agent-rex
    environment:
      - AGENT_NAME=rex
      - MC_API_URL=${MC_API_URL:-http://backend:8000}
      - MC_TOKEN=${MC_TOKEN_REX}
      - AGENT_RECYCLER_ENABLED=${AGENT_RECYCLER_ENABLED:-true}
      - AGENT_VAULT_PATH=/vault/agents/rex
      - AGENT_VAULT_INBOX=/vault/_inbox
      - AGENT_SLUG=rex
    volumes:
      - ${HOME}/.mc/agents/rex/claude-config:/home/agent/.claude
      - ${HOME}/.mc/mcp-servers:/mc-servers:ro
      - ${HOME}/.mc/workspaces/rex:/workspace
      - ${HOME}/Workspace/Projects:/workspace-ref:ro
      - ${HOME}/.mc/deliverables/rex:/deliverables
      - mc_shared_deliverables:/shared-deliverables:ro
      - ${HOME}/.mc/mcp-screenshots:/shared-mcp:ro
      - ${HOME}/.mc/vault:/vault:rw

networks:
  mission-control_default:
    external: true

volumes:
  mc_shared_deliverables:
    external: true
    name: mission-control_mc_shared_deliverables
"""


@pytest.fixture(autouse=True)
def _patch_compose_redis(fake_redis):
    async def _get_redis():
        return fake_redis
    with patch("app.services.compose_renderer.get_redis", _get_redis):
        yield


@pytest.fixture
def compose_path(tmp_path: Path) -> Path:
    p = tmp_path / "docker-compose.agents.yml"
    p.write_text(STALE_PRIVATE_FIXTURE, encoding="utf-8")
    return p


def test_example_template_itself_has_stop_grace_period_on_all_four_anchors():
    """Sanity check on the fixture's assumption: if #573 ever regresses (or
    the template gains a 5th anchor without the key), this fails BEFORE the
    more interesting renderer tests below, with an unambiguous message."""
    for anchor in ANCHOR_NAMES:
        value = _anchor_stop_grace_period(EXAMPLE_TEMPLATE, anchor)
        assert value == "20s", (
            f"docker-compose.agents.example.yml anchor {anchor!r}: expected "
            f"stop_grace_period 20s, got {value!r}"
        )


@pytest.mark.asyncio
async def test_renderer_backfills_stop_grace_period_into_stale_anchors(
    async_session, compose_path
):
    """The actual bug: a private file predating #573 has anchors with no
    stop_grace_period at all. render_compose_agents must backfill it so the
    anchor's key set — not just this one key — matches the shipped example
    template, and the VALUE must equal the template's value exactly."""
    rendered = await render_compose_agents(async_session, compose_path=compose_path)

    example_keys = _anchor_key_sets(EXAMPLE_TEMPLATE)
    rendered_keys = _anchor_key_sets(rendered)

    for anchor in ANCHOR_NAMES:
        assert anchor in rendered_keys, f"{anchor} missing from rendered output"
        # image excluded on purpose — see module docstring.
        required = example_keys[anchor] - {"image"}
        missing = required - rendered_keys[anchor]
        assert not missing, (
            f"{anchor}: renderer output is missing anchor keys present in "
            f"the example template: {missing}"
        )

    for anchor in ANCHOR_NAMES:
        example_value = _anchor_stop_grace_period(EXAMPLE_TEMPLATE, anchor)
        rendered_value = _anchor_stop_grace_period(rendered, anchor)
        assert rendered_value == example_value, (
            f"{anchor}: rendered stop_grace_period={rendered_value!r} != "
            f"example template value {example_value!r}"
        )


@pytest.mark.asyncio
async def test_anchor_patch_is_idempotent(async_session, compose_path):
    """Re-rendering an already-patched file must not add a second
    stop_grace_period line or otherwise change the anchor bodies again —
    same idempotency contract every other _ensure_* helper in this module
    already carries."""
    first = await render_compose_agents(async_session, compose_path=compose_path)
    compose_path.write_text(first, encoding="utf-8")
    second = await render_compose_agents(async_session, compose_path=compose_path)
    assert first == second
    for anchor in ANCHOR_NAMES:
        assert first.count(f"&{anchor}") == 1
    assert first.count("stop_grace_period: 20s") == len(ANCHOR_NAMES)


@pytest.mark.asyncio
async def test_anchor_patch_preserves_hand_maintained_service_mounts(
    async_session, compose_path
):
    """Backfilling the anchor must not touch existing per-service bodies —
    the #524-class mounts (/workspace-ref, /shared-deliverables, /shared-mcp)
    rex's block already carries by hand must survive untouched. Guards
    against the anchor patch becoming a second, less careful rewrite path
    (task Nachtrag c: the parity test must cover this BEFORE anyone
    re-renders a real fleet file)."""
    rendered = await render_compose_agents(async_session, compose_path=compose_path)
    for mount in (
        "${HOME}/Workspace/Projects:/workspace-ref:ro",
        "mc_shared_deliverables:/shared-deliverables:ro",
        "${HOME}/.mc/mcp-screenshots:/shared-mcp:ro",
        "${HOME}/.mc/vault:/vault:rw",
    ):
        assert mount in rendered, f"existing hand-maintained mount lost: {mount}"
    assert rendered.count("mc-agent-rex:") == 1


@pytest.mark.asyncio
async def test_anchor_patch_respects_explicit_override(async_session, compose_path):
    """An operator who deliberately set a non-default grace period on an
    anchor keeps it — same 'insert only if absent' contract as
    _ensure_msg_delivery_mode / _ensure_vault_entries / etc."""
    overridden = STALE_PRIVATE_FIXTURE.replace(
        "x-claude-agent-base: &claude-agent-base\n  image:",
        "x-claude-agent-base: &claude-agent-base\n  stop_grace_period: 45s\n  image:",
        1,
    )
    compose_path.write_text(overridden, encoding="utf-8")
    rendered = await render_compose_agents(async_session, compose_path=compose_path)
    assert _anchor_stop_grace_period(rendered, "claude-agent-base") == "45s"
    # The other three anchors still get backfilled normally.
    for anchor in ("openclaude-agent-base", "omp-agent-base", "kimi-agent-base"):
        assert _anchor_stop_grace_period(rendered, anchor) == "20s"
