"""Tests for compose_renderer.py — Phase 15 Wave 1.

The renderer must:
1. Pick the right image per runtime_type.
2. Fall back to existing static assignment when runtime_id is None.
3. Atomically write with .bak backup.
4. Emit valid YAML (parsable).
5. Be idempotent (rerunning same DB state == same output).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from app.models.agent import Agent
from app.models.runtime import Runtime
from app.services.compose_renderer import (
    CLAUDE_IMAGE,
    OPENCLAUDE_IMAGE,
    detect_image_change,
    pick_image_for_runtime,
    render_compose_agents,
    write_compose_agents,
)


# ── Redis patch for write_compose_agents ──────────────────────────────────────
# write_compose_agents acquires a global Redis lock. All tests that call it
# must use a fake_redis so they don't try to connect to a real Redis server.
@pytest.fixture(autouse=True)
def _patch_compose_redis(fake_redis):
    """Patch get_redis in compose_renderer for every test in this module."""
    async def _get_redis():
        return fake_redis
    with patch("app.services.compose_renderer.get_redis", _get_redis):
        yield


COMPOSE_FIXTURE = """\
# docker/docker-compose.agents.yml — test fixture (mirror of real layout)

x-claude-agent-base: &claude-agent-base
  image: mc-claude-agent:latest
  restart: unless-stopped

x-openclaude-agent-base: &openclaude-agent-base
  image: mc-agent-base:latest
  restart: unless-stopped

services:
  mc-agent-davinci:
    <<: *claude-agent-base
    container_name: mc-agent-davinci
    environment:
      - AGENT_NAME=davinci

  mc-agent-sparky:
    <<: *openclaude-agent-base
    container_name: mc-agent-sparky
    environment:
      - AGENT_NAME=sparky

  mc-agent-rex:
    <<: *claude-agent-base
    container_name: mc-agent-rex
    environment:
      - AGENT_NAME=rex

networks:
  mission-control_default:
    external: true
"""


@pytest.fixture
def compose_path(tmp_path: Path) -> Path:
    p = tmp_path / "docker-compose.agents.yml"
    p.write_text(COMPOSE_FIXTURE, encoding="utf-8")
    return p


# ── pick_image_for_runtime ─────────────────────────────────────────────────


def test_pick_image_cloud_runtime():
    rt = Runtime(
        slug="anthropic-claude-sonnet",
        display_name="Claude Sonnet",
        runtime_type="cloud",
        endpoint="https://api.anthropic.com",
        enabled=True,
    )
    assert pick_image_for_runtime(rt) == CLAUDE_IMAGE


def test_pick_image_vllm_runtime():
    rt = Runtime(
        slug="qwen-general",
        display_name="Qwen 3.6",
        runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1",
        enabled=True,
    )
    assert pick_image_for_runtime(rt) == OPENCLAUDE_IMAGE


def test_pick_image_lmstudio_runtime():
    rt = Runtime(
        slug="qwen-coder-lms",
        display_name="Qwen Coder",
        runtime_type="lmstudio",
        endpoint="http://192.0.2.10:1234/v1",
        enabled=True,
    )
    assert pick_image_for_runtime(rt) == OPENCLAUDE_IMAGE


def test_pick_image_ollama_cloud_runtime():
    """ollama-cloud (cloud-typed, OpenAI-compatible wire protocol) needs the
    openclaude binary — not the claude binary. Regression guard for the
    Researcher/glm-5.1 incident.
    """
    rt = Runtime(
        slug="ollama-cloud",
        display_name="Ollama Cloud",
        runtime_type="cloud",
        endpoint="https://ollama.com/v1",
        model_identifier="glm-5.1",
        enabled=True,
    )
    assert pick_image_for_runtime(rt) == OPENCLAUDE_IMAGE


def test_pick_image_disabled_returns_none():
    rt = Runtime(
        slug="x",
        display_name="x",
        runtime_type="cloud",
        endpoint="https://x",
        enabled=False,
    )
    assert pick_image_for_runtime(rt) is None


def test_pick_image_none_returns_none():
    assert pick_image_for_runtime(None) is None


# ── detect_image_change ────────────────────────────────────────────────────


def test_detect_image_change_same_type_returns_false():
    a = Runtime(slug="a", display_name="a", runtime_type="vllm_docker", endpoint="x", enabled=True)
    b = Runtime(slug="b", display_name="b", runtime_type="lmstudio", endpoint="y", enabled=True)
    # both → mc-agent-base, no image change
    assert detect_image_change(a, b) is False


def test_detect_image_change_cross_cli_returns_true():
    # Slug-based routing: only `anthropic-claude-*` maps to CLAUDE_IMAGE.
    anthropic = Runtime(slug="anthropic-claude-opus", display_name="a", runtime_type="cloud", endpoint="x", enabled=True)
    vllm = Runtime(slug="b", display_name="b", runtime_type="vllm_docker", endpoint="y", enabled=True)
    assert detect_image_change(anthropic, vllm) is True
    assert detect_image_change(vllm, anthropic) is True


def test_detect_image_change_ollama_to_vllm_same_image():
    """ollama-cloud and vllm both use the openclaude binary, so switching
    between them is a same-image swap (env-only refresh)."""
    ollama = Runtime(slug="ollama-cloud", display_name="a", runtime_type="cloud", endpoint="x", enabled=True)
    vllm = Runtime(slug="b", display_name="b", runtime_type="vllm_docker", endpoint="y", enabled=True)
    assert detect_image_change(ollama, vllm) is False


def test_detect_image_change_with_none_returns_true():
    rt = Runtime(slug="anthropic-claude-opus", display_name="a", runtime_type="cloud", endpoint="x", enabled=True)
    assert detect_image_change(None, rt) is True
    assert detect_image_change(rt, None) is True


# ── render_compose_agents ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_renders_claude_agent_with_anthropic_runtime(async_session, compose_path):
    rt = Runtime(
        slug="anthropic-claude-sonnet",
        display_name="Claude Sonnet",
        runtime_type="cloud",
        endpoint="https://api.anthropic.com",
        enabled=True,
    )
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)

    davinci = Agent(
        name="Davinci",
        agent_runtime="cli-bridge",
        runtime_id=rt.id,
    )
    async_session.add(davinci)
    await async_session.commit()
    await async_session.refresh(davinci)

    rendered = await render_compose_agents(async_session, compose_path=compose_path)

    # Davinci already inherits claude-agent-base → no override needed.
    parsed = yaml.safe_load(rendered)
    services = parsed.get("services", {})
    assert "mc-agent-davinci" in services
    # No explicit image override because anchor already provides correct image.
    assert "image" not in services["mc-agent-davinci"] or services["mc-agent-davinci"]["image"] == CLAUDE_IMAGE


@pytest.mark.asyncio
async def test_renders_openclaude_image_for_cross_cli_switch(async_session, compose_path):
    """Davinci was on claude-agent-base anchor; switch to vllm runtime should
    inject explicit `image: mc-agent-base:latest` override."""
    rt = Runtime(
        slug="qwen-general",
        display_name="Qwen 3.6",
        runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1",
        enabled=True,
    )
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)

    davinci = Agent(
        name="Davinci",
        agent_runtime="cli-bridge",
        runtime_id=rt.id,
    )
    async_session.add(davinci)
    await async_session.commit()
    await async_session.refresh(davinci)

    rendered = await render_compose_agents(async_session, compose_path=compose_path)

    parsed = yaml.safe_load(rendered)
    services = parsed["services"]
    # Override should now be present.
    assert services["mc-agent-davinci"].get("image") == OPENCLAUDE_IMAGE
    # Sparky stays on its base, no extra change.
    assert "image" not in services["mc-agent-sparky"] or services["mc-agent-sparky"]["image"] == OPENCLAUDE_IMAGE


@pytest.mark.asyncio
async def test_fallback_when_runtime_id_null(async_session, compose_path):
    """No runtime → no image override.

    Explicit non-vault scopes are set so M.3 vault injection does not change
    the output. The intent of this test is the runtime fallback path; vault
    behavior is covered by ``test_compose_renderer_vault.py``.
    """
    davinci = Agent(
        name="Davinci",
        agent_runtime="cli-bridge",
        runtime_id=None,
        scopes=["chat:write"],
    )
    async_session.add(davinci)
    await async_session.commit()

    rendered = await render_compose_agents(async_session, compose_path=compose_path)

    # Output should be byte-identical to the source (no overrides, no vault).
    assert rendered == COMPOSE_FIXTURE


@pytest.mark.asyncio
async def test_compose_yaml_is_valid_via_yaml_parse(async_session, compose_path):
    rt = Runtime(
        slug="qwen-general",
        display_name="Qwen 3.6",
        runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1",
        enabled=True,
    )
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)

    davinci = Agent(name="Davinci", agent_runtime="cli-bridge", runtime_id=rt.id)
    sparky = Agent(name="Sparky", agent_runtime="cli-bridge", runtime_id=rt.id)
    async_session.add_all([davinci, sparky])
    await async_session.commit()

    rendered = await render_compose_agents(async_session, compose_path=compose_path)

    parsed = yaml.safe_load(rendered)
    assert parsed is not None
    assert "services" in parsed


@pytest.mark.asyncio
async def test_compose_writer_acquires_global_lock(async_session, compose_path, fake_redis):
    """write_compose_agents must SET the global compose lock key in Redis
    while the write is in progress and DELETE it afterward."""
    from unittest.mock import patch as mpatch

    async def _get_redis():
        return fake_redis

    rt = Runtime(
        slug="lock-test",
        display_name="Lock Test",
        runtime_type="vllm_docker",
        endpoint="http://localhost:8000/v1",
        enabled=True,
    )
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)

    davinci = Agent(name="Davinci", agent_runtime="cli-bridge", runtime_id=rt.id)
    async_session.add(davinci)
    await async_session.commit()

    lock_key = "mc:compose:agents-yml:write"

    # Lock must not exist before the call.
    assert await fake_redis.exists(lock_key) == 0

    with mpatch("app.services.compose_renderer.get_redis", _get_redis):
        result = await write_compose_agents(async_session, compose_path=compose_path)

    assert result["changed"] == "true"
    # Lock must be released after the call completes.
    assert await fake_redis.exists(lock_key) == 0


@pytest.mark.asyncio
async def test_concurrent_compose_writes_serialize(async_session, compose_path, fake_redis):
    """If the global lock is already held when write_compose_agents is called,
    it raises RuntimeError (busy) on the second attempt after the retry delay."""
    import asyncio
    from unittest.mock import patch as mpatch

    async def _get_redis():
        return fake_redis

    lock_key = "mc:compose:agents-yml:write"

    # Pre-take the lock to simulate a concurrent writer holding it.
    await fake_redis.set(lock_key, "1", nx=True, ex=60)
    assert await fake_redis.exists(lock_key) == 1

    rt = Runtime(
        slug="serial-test",
        display_name="Serial Test",
        runtime_type="vllm_docker",
        endpoint="http://localhost:8000/v1",
        enabled=True,
    )
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)

    davinci = Agent(name="Davinci", agent_runtime="cli-bridge", runtime_id=rt.id)
    async_session.add(davinci)
    await async_session.commit()

    # Patch sleep to avoid 2-second delay in test.
    async def _no_sleep(_):
        pass

    with mpatch("app.services.compose_renderer.get_redis", _get_redis), \
         mpatch("asyncio.sleep", _no_sleep):
        with pytest.raises(RuntimeError, match="compose write lock busy"):
            await write_compose_agents(async_session, compose_path=compose_path)

    # Cleanup: release the pre-taken lock.
    await fake_redis.delete(lock_key)


@pytest.mark.asyncio
async def test_atomic_write_creates_backup(async_session, compose_path):
    rt = Runtime(
        slug="qwen-general",
        display_name="Qwen 3.6",
        runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1",
        enabled=True,
    )
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)

    davinci = Agent(name="Davinci", agent_runtime="cli-bridge", runtime_id=rt.id)
    async_session.add(davinci)
    await async_session.commit()

    result = await write_compose_agents(async_session, compose_path=compose_path)

    assert result["changed"] == "true"
    bak = compose_path.with_suffix(compose_path.suffix + ".bak")
    assert bak.exists()
    assert bak.read_text(encoding="utf-8") == COMPOSE_FIXTURE
    # Target now contains the override.
    new_content = compose_path.read_text(encoding="utf-8")
    assert OPENCLAUDE_IMAGE in new_content
    # Idempotent rerun should report changed=false.
    result2 = await write_compose_agents(async_session, compose_path=compose_path)
    assert result2["changed"] == "false"


# ── Backward-compat: scopes=None / scopes=[] → vault:write ───────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("scopes_value", [None, []], ids=["scopes_none", "scopes_empty_list"])
async def test_render_compose_injects_vault_for_null_or_empty_scopes(
    async_session, compose_path, scopes_value
):
    """Agents with scopes=None or scopes=[] must be treated as having ALL scopes
    (backward-compat rule: agents created before the scope system get full
    access).  Concretely, render_compose_agents() must add vault entries for
    such an agent's slug into the rendered compose output.

    This exercises the DB-side path in render_compose_agents():
        scopes = ag.scopes
        if not scopes or Scope.VAULT_WRITE.value in scopes:
            vault_writers.add(slug)

    The compose fixture exposes ``mc-agent-davinci`` at 2-space indent so the
    vault injection logic has a real service block to mutate.
    """
    davinci = Agent(
        name="Davinci",
        agent_runtime="cli-bridge",
        runtime_id=None,
        scopes=scopes_value,
    )
    async_session.add(davinci)
    await async_session.commit()

    rendered = await render_compose_agents(async_session, compose_path=compose_path)

    # Vault entries must appear in Davinci's service block.
    assert "AGENT_VAULT_PATH=/vault/agents/davinci" in rendered, (
        f"scopes={scopes_value!r}: AGENT_VAULT_PATH missing from rendered output"
    )
    assert "AGENT_VAULT_INBOX=/vault/_inbox" in rendered, (
        f"scopes={scopes_value!r}: AGENT_VAULT_INBOX missing"
    )
    assert "AGENT_SLUG=davinci" in rendered, (
        f"scopes={scopes_value!r}: AGENT_SLUG missing"
    )
    assert "/.mc/vault:/vault:rw" in rendered, (
        f"scopes={scopes_value!r}: vault volume mount missing"
    )
