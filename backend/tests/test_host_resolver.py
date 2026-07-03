"""Host Resolver + host-aware runtime_manager (ADR-048, Task B2).

Covers:
- resolve_host_for_runtime(): all 4 stages of the back-compat chain
  (host_id → legacy host field → settings.dgx_ssh_* → None), incl.
  disabled-host warning without silent fallback
- _ssh_run(): clear "runtime has no host" error instead of connecting against ""
- Host-scoped eviction: all SSH calls run on the runtime's host
- get_host_metrics(): ssh host (nvidia-smi via SSH, mocked), flask_wol
  (health instead of GPU), local (empty), error → unreachable
- get_spark_metrics(): stays a thin settings fallback wrapper
- Lifecycle functions thread the host through to _ssh_run

Public repo rule: only doc placeholder IPs (192.0.2.x, RFC 5737) and
dummy MACs in fixtures.
"""
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.models.host import Host
from app.models.runtime import Runtime
from app.services import host_resolver, runtime_manager
from app.services.host_resolver import (
    ResolvedHost,
    resolve_host_for_runtime,
    resolve_host_from_runtime_fields,
    settings_fallback_host,
)

DUMMY_MAC = "00:00:5E:00:53:01"


def _make_runtime(slug: str = "test-rt", **kwargs) -> Runtime:
    defaults = dict(
        display_name=slug,
        runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1",
    )
    defaults.update(kwargs)
    return Runtime(slug=slug, **defaults)


@pytest.fixture
def dgx_settings(monkeypatch):
    """Classic single-box setup: only settings.dgx_ssh_* set."""
    monkeypatch.setattr(settings, "dgx_ssh_host", "192.0.2.60")
    monkeypatch.setattr(settings, "dgx_ssh_user", "mcuser")
    monkeypatch.setattr(settings, "dgx_ssh_key_path", "/keys/id_test")
    return settings


@pytest.fixture
def no_dgx_settings(monkeypatch):
    """Fresh install without a GPU box: no DGX env."""
    monkeypatch.setattr(settings, "dgx_ssh_host", "")
    monkeypatch.setattr(settings, "dgx_ssh_user", "")
    return settings


# ── Resolver chain stage 1: host_id → registry host ────────────────────────


async def test_stage1_host_id_wins_over_everything(session, dgx_settings):
    host = Host(
        slug="box-a", display_name="Box A", kind="ssh",
        ssh_host="192.0.2.20", ssh_user="boxuser", ssh_key_path="/keys/box_a",
    )
    session.add(host)
    await session.commit()
    await session.refresh(host)

    # Legacy field AND settings are set — host_id must still win.
    rt = _make_runtime(host_id=host.id, host="192.0.2.50")
    session.add(rt)
    await session.commit()

    resolved = await resolve_host_for_runtime(session, rt)
    assert resolved is not None
    assert resolved.source == "registry"
    assert resolved.slug == "box-a"
    assert resolved.ssh_host == "192.0.2.20"
    assert resolved.ssh_user == "boxuser"
    assert resolved.host_id == host.id


async def test_stage1_disabled_host_warns_but_no_silent_fallback(
    session, dgx_settings, caplog
):
    host = Host(
        slug="box-off", display_name="Box Off", kind="ssh",
        ssh_host="192.0.2.21", enabled=False,
    )
    session.add(host)
    await session.commit()
    await session.refresh(host)
    rt = _make_runtime(host_id=host.id)

    with caplog.at_level("WARNING", logger="mc.host_resolver"):
        resolved = await resolve_host_for_runtime(session, rt)

    # The disabled host is returned — NOT silently swapped for settings.
    assert resolved is not None
    assert resolved.slug == "box-off"
    assert resolved.enabled is False
    assert any("disabled" in r.message for r in caplog.records)


async def test_stage1_flask_wol_host_carries_control_fields(session, no_dgx_settings):
    host = Host(
        slug="wol-box", display_name="WoL Box", kind="flask_wol",
        ssh_host="192.0.2.30", control_url="http://192.0.2.30:5555",
        wol_mac_address=DUMMY_MAC, power_managed=True,
    )
    session.add(host)
    await session.commit()
    await session.refresh(host)
    rt = _make_runtime(runtime_type="unsloth_porsche", host_id=host.id)

    resolved = await resolve_host_for_runtime(session, rt)
    assert resolved.kind == "flask_wol"
    assert resolved.control_url == "http://192.0.2.30:5555"
    assert resolved.wol_mac_address == DUMMY_MAC
    assert resolved.power_managed is True


# ── Stage 2: legacy host field ──────────────────────────────────────────────


async def test_stage2_legacy_host_field(session, dgx_settings):
    rt = _make_runtime(host="192.0.2.50")

    resolved = await resolve_host_for_runtime(session, rt)
    assert resolved is not None
    assert resolved.source == "legacy_host_field"
    assert resolved.ssh_host == "192.0.2.50"
    # SSH user/key come from settings at stage 2 (pre-registry behavior).
    assert resolved.ssh_user == "mcuser"
    assert resolved.ssh_key_path == "/keys/id_test"


# ── Stage 3: settings.dgx_ssh_* fallback ────────────────────────────────────


async def test_stage3_settings_fallback(session, dgx_settings):
    rt = _make_runtime()  # no host_id, no legacy field

    resolved = await resolve_host_for_runtime(session, rt)
    assert resolved is not None
    assert resolved.source == "settings"
    assert resolved.ssh_host == "192.0.2.60"


def test_stage3_works_without_session_via_runtime_fields(dgx_settings):
    resolved = resolve_host_from_runtime_fields({"slug": "x"})
    assert resolved is not None
    assert resolved.source == "settings"
    assert resolved.ssh_host == "192.0.2.60"


# ── Stage 4: nothing configured → None ──────────────────────────────────────


async def test_stage4_none_when_nothing_configured(session, no_dgx_settings):
    rt = _make_runtime(runtime_type="cloud")
    assert await resolve_host_for_runtime(session, rt) is None
    assert settings_fallback_host() is None


async def test_stage4_ssh_run_raises_clear_no_host_error(no_dgx_settings):
    with pytest.raises(RuntimeError, match="Runtime hat keinen Host"):
        await runtime_manager._ssh_run("docker ps")


async def test_stage4_lifecycle_op_surfaces_clear_error(no_dgx_settings):
    """start_runtime without any host → clear error, no connect against ''."""
    rt = {
        "id": "orphan", "slug": "orphan", "display_name": "Orphan",
        "runtime_type": "vllm_docker", "container_name": "mc-orphan",
        "endpoint": "http://192.0.2.99:8000/v1",
    }
    result = await runtime_manager.start_runtime(rt)
    assert result["ok"] is False
    assert "keinen Host" in result["message"]


# ── Dict compatibility (model_dump / to_registry_dict) ──────────────────────


async def test_resolver_accepts_model_dump_dict(session, dgx_settings):
    host = Host(slug="box-d", display_name="Box D", kind="ssh", ssh_host="192.0.2.22")
    session.add(host)
    await session.commit()
    await session.refresh(host)
    rt = _make_runtime(host_id=host.id)
    session.add(rt)
    await session.commit()
    await session.refresh(rt)

    resolved = await resolve_host_for_runtime(session, rt.model_dump())
    assert resolved.slug == "box-d"


# ── Host-scoped eviction ─────────────────────────────────────────────────────


async def test_eviction_runs_on_the_runtimes_host():
    """All eviction SSH calls (stop + poll) must run on the starting
    runtime's host — never implicitly on the settings box."""
    box_b = ResolvedHost(ssh_host="192.0.2.70", ssh_user="u", slug="box-b", source="registry")
    ssh = AsyncMock(side_effect=[
        ("sparkrun_old_solo", "", 0),  # stop command
        ("", "", 0),                   # poll: box free
    ])
    with patch.object(runtime_manager, "_ssh_run", ssh), \
         patch.object(runtime_manager, "_evict_poll_interval", 0):
        result = await runtime_manager.evict_spark_runtime_containers(
            "qwen-general", host=box_b
        )

    assert result["ok"] is True
    assert ssh.call_args_list, "expected SSH calls"
    for c in ssh.call_args_list:
        assert c.kwargs.get("host") is box_b, f"call not host-scoped: {c}"


async def test_verify_started_is_host_scoped():
    box_b = ResolvedHost(ssh_host="192.0.2.70", slug="box-b", source="registry")
    ssh = AsyncMock(return_value=("abc123", "", 0))
    with patch.object(runtime_manager, "_ssh_run", ssh), \
         patch.object(runtime_manager, "_verify_poll_interval", 0):
        appeared = await runtime_manager.verify_spark_container_started(
            "qwen-general", host=box_b
        )
    assert appeared is True
    assert ssh.call_args.kwargs.get("host") is box_b


async def test_switch_recipe_evicts_and_starts_on_resolved_host(
    async_session, fake_redis
):
    """switch_recipe resolves the runtime's host and threads it through to
    eviction AND start (box A is never touched for a switch to box B)."""
    from app.services import sparkrun_manager

    host = Host(slug="box-a", display_name="Box A", kind="ssh", ssh_host="192.0.2.20")
    async_session.add(host)
    await async_session.commit()
    await async_session.refresh(host)
    rt = _make_runtime(
        slug="qwen-general",
        host_id=host.id,
        launch_command=(
            "uvx sparkrun run @official/old-recipe --solo --no-rm --ensure "
            "--no-follow --label mc.runtime.slug=qwen-general"
        ),
    )
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)

    evict = AsyncMock(return_value={"ok": True, "message": "evicted", "stopped": []})
    start = AsyncMock(return_value={"ok": True, "message": "starting"})

    async def _fake_get_redis():
        return fake_redis

    with (
        patch("app.services.runtime_manager.evict_spark_runtime_containers", evict),
        patch("app.services.runtime_manager.start_runtime", start),
        patch("app.services.runtime_model_resolver.get_redis", _fake_get_redis),
        patch(
            "app.services.agent_runtime_switch.probe_runtime_model",
            AsyncMock(return_value=None),
        ),
    ):
        result = await sparkrun_manager.switch_recipe(
            async_session, rt, "@official/new-recipe"
        )

    assert result["ok"] is True
    assert evict.call_args.kwargs["host"].slug == "box-a"
    assert start.call_args.kwargs["host"].slug == "box-a"


# ── get_host_metrics ─────────────────────────────────────────────────────────

_METRICS_OUT = (
    "47, 88064, 131072, 62\n---\n"
    "               total        used        free\n"
    "Mem:          128000       24000       80000\n"
)


async def test_get_host_metrics_ssh_host(dgx_settings):
    box = ResolvedHost(ssh_host="192.0.2.40", slug="box-c", source="registry")
    ssh = AsyncMock(return_value=(_METRICS_OUT, "", 0))
    with patch.object(runtime_manager, "_ssh_run", ssh):
        metrics = await runtime_manager.get_host_metrics(box)

    assert metrics["reachable"] is True
    assert metrics["gpu_util_pct"] == 47
    assert metrics["vram_total_mb"] == 131072
    assert metrics["ram_used_mb"] == 24000
    # The SSH call must run on the given host.
    assert ssh.call_args.kwargs.get("host") is box
    assert "nvidia-smi" in ssh.call_args.args[0]


async def test_get_host_metrics_flask_wol_uses_health_not_ssh():
    box = ResolvedHost(
        kind="flask_wol", control_url="http://192.0.2.30:5555",
        slug="wol-box", source="registry",
    )
    ssh = AsyncMock()
    with patch.object(runtime_manager, "_ssh_run", ssh), \
         patch.object(runtime_manager, "_porsche_reachable", AsyncMock(return_value=True)):
        metrics = await runtime_manager.get_host_metrics(box)

    assert metrics["reachable"] is True
    assert metrics["gpu_util_pct"] is None  # health status instead of GPU metrics
    ssh.assert_not_called()


async def test_get_host_metrics_local_is_empty():
    box = ResolvedHost(kind="local", slug="mc-host", source="registry")
    ssh = AsyncMock()
    with patch.object(runtime_manager, "_ssh_run", ssh):
        metrics = await runtime_manager.get_host_metrics(box)
    assert metrics["reachable"] is False
    ssh.assert_not_called()


async def test_get_host_metrics_ssh_failure_is_unreachable():
    box = ResolvedHost(ssh_host="192.0.2.41", slug="box-e", source="registry")
    ssh = AsyncMock(side_effect=OSError("connect refused"))
    with patch.object(runtime_manager, "_ssh_run", ssh):
        metrics = await runtime_manager.get_host_metrics(box)
    assert metrics == runtime_manager._SPARK_UNREACHABLE


async def test_get_spark_metrics_stays_settings_fallback_wrapper(dgx_settings):
    ssh = AsyncMock(return_value=(_METRICS_OUT, "", 0))
    with patch.object(runtime_manager, "_ssh_run", ssh):
        metrics = await runtime_manager.get_spark_metrics()
    assert metrics["reachable"] is True
    assert ssh.call_args.kwargs.get("host").ssh_host == "192.0.2.60"


# ── Lifecycle functions thread the host through ─────────────────────────────


async def test_start_runtime_threads_host_to_ssh():
    box = ResolvedHost(ssh_host="192.0.2.20", slug="box-a", source="registry")
    rt = {
        "id": "qwen", "slug": "qwen", "display_name": "Qwen",
        "runtime_type": "vllm_docker", "container_name": "mc-qwen",
        "endpoint": "http://192.0.2.20:8000/v1",
    }
    ssh = AsyncMock(return_value=("running", "", 0))
    with patch.object(runtime_manager, "_ssh_run", ssh):
        result = await runtime_manager.start_runtime(rt, host=box)

    assert result["ok"] is True
    for c in ssh.call_args_list:
        assert c.kwargs.get("host") is box


async def test_get_runtime_state_threads_host_to_ssh():
    box = ResolvedHost(ssh_host="192.0.2.20", slug="box-a", source="registry")
    rt = {
        "id": "qwen", "slug": "qwen", "runtime_type": "vllm_docker",
        "container_name": "mc-qwen", "endpoint": "http://192.0.2.20:8000/v1",
        "healthcheck_path": "/v1/models",
    }
    ssh = AsyncMock(return_value=("exited", "", 0))
    with patch.object(runtime_manager, "_ssh_run", ssh):
        state = await runtime_manager.get_runtime_state(rt, host=box)

    assert state["state"] == "stopped"
    assert ssh.call_args.kwargs.get("host") is box
