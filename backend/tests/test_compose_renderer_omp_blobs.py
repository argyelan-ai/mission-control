"""omp's image folder (``blobs``) is mounted from the host (decision H7).

omp keeps the images of a conversation in ``<agent-dir>/blobs`` — inside an
omp-harness container that is ``/home/agent/.omp/profiles/mc-agent/agent/blobs``,
a sibling of the ``sessions`` folder that is already mounted (see
test_compose_renderer_omp_sessions.py). Without a host mount every container
recreate (image update, harness switch, CLI update) threw the images away while
the conversation that points at them survived on the host.

The mount mirrors the omp-sessions one: same marker check (no duplicate line on
a second render), only for the ``mc-omp-agent`` image, on both the rewrite path
(``_rewrite_compose``) and the append path (``_build_new_agent_block``). Its
source sits below ``${HOME}/.mc/`` — inside the allowed root of ADR-095.

The renderer also creates the host folders (omp-sessions and omp-blobs) for the
live compose file, so the container never finds a folder that Docker had to
create on its own.
"""
from __future__ import annotations

import re
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from app.models.agent import Agent
from app.models.runtime import Runtime
from app.services import compose_renderer
from app.services.compose_renderer import (
    CLAUDE_IMAGE,
    KIMI_IMAGE,
    OMP_IMAGE,
    OPENCLAUDE_IMAGE,
    _build_new_agent_block,
    _ensure_omp_blobs_volume,
    _rewrite_compose,
    write_compose_agents,
)

BLOBS_TARGET = "/home/agent/.omp/profiles/mc-agent/agent/blobs"


def _blobs_line(slug: str) -> str:
    return f"      - ${{HOME}}/.mc/agents/{slug}/omp-blobs:{BLOBS_TARGET}:rw"


def _extract_service_block(content: str, slug: str) -> str:
    pattern = re.compile(
        rf"(^  mc-agent-{re.escape(slug)}:\s*$.*?)(?=^  mc-agent-|^[a-zA-Z]|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(content)
    if not m:
        raise AssertionError(f"service block mc-agent-{slug} not found")
    return m.group(1)


def _blobs_lines(block: str) -> list[str]:
    return [line for line in block.splitlines() if "omp-blobs" in line]


# Neutral fixture (public repo): one agent per harness, plus an omp agent that
# inherits its image from the omp anchor and one with an explicit image line.
_COMPOSE = textwrap.dedent(
    """\
    x-claude-agent-base: &claude-agent-base
      image: mc-claude-agent:latest
      restart: unless-stopped

    x-openclaude-agent-base: &openclaude-agent-base
      image: mc-agent-base:latest
      restart: unless-stopped

    x-omp-agent-base: &omp-agent-base
      image: mc-omp-agent:latest
      restart: unless-stopped

    x-kimi-agent-base: &kimi-agent-base
      image: mc-kimi-agent:latest
      restart: unless-stopped

    services:
      mc-agent-alpha:
        <<: *openclaude-agent-base
        image: mc-omp-agent:latest
        container_name: mc-agent-alpha
        volumes:
          - ${HOME}/.mc/agents/alpha/claude-config:/home/agent/.claude

      mc-agent-delta:
        <<: *omp-agent-base
        container_name: mc-agent-delta
        volumes:
          - ${HOME}/.mc/agents/delta/claude-config:/home/agent/.claude

      mc-agent-beta:
        <<: *claude-agent-base
        container_name: mc-agent-beta
        volumes:
          - ${HOME}/.mc/agents/beta/claude-config:/home/agent/.claude

      mc-agent-gamma:
        <<: *openclaude-agent-base
        container_name: mc-agent-gamma
        volumes:
          - ${HOME}/.mc/agents/gamma/claude-config:/home/agent/.claude

      mc-agent-kappa:
        <<: *kimi-agent-base
        container_name: mc-agent-kappa
        volumes:
          - ${HOME}/.mc/agents/kappa/claude-config:/home/agent/.claude
    """
)


# ── 1 · rewrite path adds exactly one line for mc-omp-agent ──────────────────

@pytest.mark.parametrize("slug", ["alpha", "delta"])
def test_rewrite_adds_exactly_one_blobs_line_for_omp(slug):
    """alpha: explicit ``image: mc-omp-agent`` line; delta: image inherited
    from ``*omp-agent-base``. Both resolve to the omp image without any
    DB-sourced override, and both get the line exactly once."""
    block = _extract_service_block(_rewrite_compose(_COMPOSE, image_overrides={}), slug)
    assert _blobs_lines(block) == [_blobs_line(slug)]


def test_switch_to_omp_via_override_adds_the_line():
    """A DB-driven switch onto omp (claude anchor in the file) gets it too."""
    rendered = _rewrite_compose(_COMPOSE, image_overrides={"beta": OMP_IMAGE})
    block = _extract_service_block(rendered, "beta")
    assert f"image: {OMP_IMAGE}" in block
    assert _blobs_lines(block) == [_blobs_line("beta")]


def test_blobs_line_sits_in_the_volumes_list_next_to_sessions():
    """The line must land INSIDE the service's ``volumes:`` list — the same
    list that carries the omp-sessions mount — not after the block."""
    block = _extract_service_block(_rewrite_compose(_COMPOSE, image_overrides={}), "delta")
    lines = block.splitlines()
    vol_idx = lines.index("    volumes:")
    items = []
    for line in lines[vol_idx + 1:]:
        if not line.startswith("      - "):
            break
        items.append(line)
    assert _blobs_line("delta") in items
    assert any("/omp-sessions:" in item for item in items)


# ── 2 · second render → no duplicate ─────────────────────────────────────────

def test_second_render_adds_no_duplicate():
    once = _rewrite_compose(_COMPOSE, image_overrides={})
    twice = _rewrite_compose(once, image_overrides={})
    assert once == twice
    assert twice.count(BLOBS_TARGET) == 2  # alpha + delta, once each


def test_hand_written_line_without_mode_is_recognised():
    """A line written by hand without ``:rw`` is the same mount (compose's
    default mode is rw) — the marker must see it and add nothing."""
    body = [
        "  mc-agent-delta:",
        "    volumes:",
        f"      - ${{HOME}}/.mc/agents/delta/omp-blobs:{BLOBS_TARGET}",
    ]
    assert _ensure_omp_blobs_volume(body, "delta") == body


def test_marker_is_slug_anchored():
    """Another agent's blobs line in the body does not count as this one's."""
    body = [
        "  mc-agent-delta:",
        "    volumes:",
        f"      - ${{HOME}}/.mc/agents/delta-two/omp-blobs:{BLOBS_TARGET}:rw",
    ]
    out = _ensure_omp_blobs_volume(body, "delta")
    assert _blobs_line("delta") in out


def test_creates_volumes_block_when_missing():
    body = ["  mc-agent-delta:", "    <<: *omp-agent-base"]
    out = _ensure_omp_blobs_volume(body, "delta")
    assert out[-2:] == ["    volumes:", _blobs_line("delta")]


# ── 3 · append path (new agent) has it ───────────────────────────────────────

def test_append_path_new_omp_agent_gets_the_line():
    block = _build_new_agent_block("newagent", OMP_IMAGE, is_vault_writer=False)
    assert _blobs_lines(block) == [_blobs_line("newagent")]


# ── 4 · claude, kimi and openclaude images never get it ──────────────────────

@pytest.mark.parametrize("slug", ["beta", "gamma", "kappa"])
def test_rewrite_path_non_omp_images_get_no_blobs_line(slug):
    block = _extract_service_block(_rewrite_compose(_COMPOSE, image_overrides={}), slug)
    assert "omp-blobs" not in block


@pytest.mark.parametrize(
    "image",
    [CLAUDE_IMAGE, OPENCLAUDE_IMAGE, KIMI_IMAGE],
    ids=["claude", "openclaude", "kimi"],
)
def test_append_path_non_omp_images_get_no_blobs_line(image):
    block = _build_new_agent_block("newagent", image, is_vault_writer=False)
    assert "omp-blobs" not in block


def test_switch_away_from_omp_gains_no_blobs_line():
    rendered = _rewrite_compose(_COMPOSE, image_overrides={"delta": OPENCLAUDE_IMAGE})
    block = _extract_service_block(rendered, "delta")
    assert f"image: {OPENCLAUDE_IMAGE}" in block
    assert "omp-blobs" not in block


# ── 5 · source starts with ${HOME}/.mc/agents/<slug>/ ────────────────────────

def test_source_is_below_the_agents_own_mc_folder():
    """ADR-095 decision 2: a renderer-made source must sit below
    ``HOME_HOST/.mc/``. Both paths use the agent's own folder."""
    rewritten = _extract_service_block(_rewrite_compose(_COMPOSE, image_overrides={}), "delta")
    appended = _build_new_agent_block("newagent", OMP_IMAGE, is_vault_writer=False)
    for slug, block in (("delta", rewritten), ("newagent", appended)):
        (line,) = _blobs_lines(block)
        source = line.strip()[2:].split(":", 1)[0]
        assert source.startswith(f"${{HOME}}/.mc/agents/{slug}/"), source
        assert source == f"${{HOME}}/.mc/agents/{slug}/omp-blobs"
        assert ".." not in source


# ── host folders exist before the container is (re)created ──────────────────

@pytest.fixture
def _redis(fake_redis):
    async def _get_redis():
        return fake_redis
    with patch("app.services.compose_renderer.get_redis", _get_redis):
        yield


@pytest.fixture
def host_home(tmp_path: Path, monkeypatch) -> Path:
    """What the backend container has: MC_AGENTS_ROOT = <home>/.mc/agents."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MC_AGENTS_ROOT", str(home / ".mc" / "agents"))
    return home


@pytest.fixture
def live_compose(tmp_path: Path) -> Path:
    path = tmp_path / "repo" / "docker" / "docker-compose.agents.yml"
    path.parent.mkdir(parents=True)
    path.write_text(_COMPOSE, encoding="utf-8")
    with patch.object(compose_renderer, "DEFAULT_COMPOSE_PATH", path):
        yield path


@pytest.mark.asyncio
async def test_live_write_creates_omp_host_folders(
    _redis, async_session, host_home, live_compose
):
    """The live file gets the mount → the backend creates both host folders
    (it sees ``HOME_HOST/.mc`` under the same path), for omp services only."""
    await write_compose_agents(async_session)

    agents = host_home / ".mc" / "agents"
    for slug in ("alpha", "delta"):
        assert (agents / slug / "omp-blobs").is_dir()
        assert (agents / slug / "omp-sessions").is_dir()
    for slug in ("beta", "gamma", "kappa"):
        assert not (agents / slug).exists()


@pytest.mark.asyncio
async def test_unchanged_live_file_still_ensures_the_folders(
    _redis, async_session, host_home, live_compose
):
    """A re-render that changes nothing (the file is already current) still
    creates a folder that went missing — that render is the step right before
    a recreate."""
    await write_compose_agents(async_session)
    blobs = host_home / ".mc" / "agents" / "delta" / "omp-blobs"
    blobs.rmdir()

    result = await write_compose_agents(async_session)

    assert result["changed"] == "false"
    assert blobs.is_dir()


@pytest.mark.asyncio
async def test_existing_folder_content_is_left_alone(
    _redis, async_session, host_home, live_compose
):
    blobs = host_home / ".mc" / "agents" / "delta" / "omp-blobs"
    blobs.mkdir(parents=True)
    (blobs / "keep.bin").write_bytes(b"\x89PNG")

    await write_compose_agents(async_session)

    assert (blobs / "keep.bin").read_bytes() == b"\x89PNG"


@pytest.mark.asyncio
async def test_other_compose_files_create_no_host_folders(
    _redis, async_session, host_home, tmp_path
):
    """Only the file the stack runs on gets host folders. A render into any
    other path (tests, a scratch copy) must not create folders in the
    operator's real agents directory."""
    other = tmp_path / "scratch" / "docker-compose.agents.yml"
    other.parent.mkdir()
    other.write_text(_COMPOSE, encoding="utf-8")

    await write_compose_agents(async_session, compose_path=other)

    assert "omp-blobs" in other.read_text(encoding="utf-8")
    assert not (host_home / ".mc").exists()


@pytest.mark.asyncio
async def test_without_agents_root_nothing_is_created(
    _redis, async_session, tmp_path, live_compose, monkeypatch
):
    """MC_AGENTS_ROOT is set only in the backend and worker containers. A
    test run or a bare-metal dev run has no such root, and then nothing is
    created, even for the default compose file: tests that render that file
    must never litter the operator's live agents directory. In particular,
    the code must not fall back to ``settings.home_host`` (the real home in
    a test run)."""
    monkeypatch.delenv("MC_AGENTS_ROOT", raising=False)
    home = tmp_path / "home-host"
    home.mkdir()
    monkeypatch.setattr(compose_renderer.settings, "home_host", str(home))

    result = await write_compose_agents(async_session)

    assert result["changed"] == "true"
    assert "omp-blobs" in live_compose.read_text(encoding="utf-8")
    assert list(home.iterdir()) == []


@pytest.mark.asyncio
async def test_folder_failure_never_blocks_the_compose_write(
    _redis, async_session, host_home, live_compose
):
    """Best effort: a folder that cannot be created is logged; the compose
    file is still written (Docker Desktop creates a missing source itself)."""
    blocker = host_home / ".mc"
    blocker.write_text("not a directory", encoding="utf-8")

    result = await write_compose_agents(async_session)

    assert result["changed"] == "true"
    assert "omp-blobs" in live_compose.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_new_omp_agent_from_db_gets_line_and_folders(
    _redis, async_session, host_home, live_compose
):
    """End to end through the DB: a new omp-harness agent is appended with
    the line, and its host folders exist after the write."""
    rt = Runtime(
        slug="omp-local",
        display_name="Local omp",
        runtime_type="omp",
        endpoint="http://localhost:8000/v1",
        enabled=True,
    )
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)
    async_session.add(
        Agent(name="Worker-O", agent_runtime="cli-bridge", runtime_id=rt.id,
              harness="omp", scopes=["tasks:read"])
    )
    await async_session.commit()

    await write_compose_agents(async_session)

    block = _extract_service_block(live_compose.read_text(encoding="utf-8"), "worker-o")
    assert _blobs_lines(block) == [_blobs_line("worker-o")]
    assert (host_home / ".mc" / "agents" / "worker-o" / "omp-blobs").is_dir()
