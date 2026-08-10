"""Task #22 — ownership-verified eviction.

Pins the two hardenings on top of ``evict_spark_runtime_containers``'s P0/P1:

  (a) compose-project discovery: a compose service with no ``container_name:``
      gets a docker-generated name that matches neither the
      ``mc.runtime.slug`` label nor either name filter — only the
      ``com.docker.compose.project`` sweep finds it (the live incident,
      08./09.08.2026: a qwen sparkrun wrapper survived eviction unseen).
  (b) ownership verification: a container that DOES carry MC's own
      ``mc.runtime.slug`` label is only stopped if its ``mc.runtime.nonce``
      label matches what MC recorded at launch — "never stop what we cannot
      prove is ours" (Local Studio's ownership pattern, see
      ``runtime_ownership.py`` module docstring). A mismatch blocks the stop
      and raises a ``runtime.eviction_ownership_blocked`` warning event
      instead.
  (c) diagnostics: every eviction call logs (and a "nothing found" result
      surfaces) the box-wide container total plus the per-matcher breakdown,
      so a false "box already free" is debuggable from the logs alone.

All SSH and Redis are mocked/faked — nothing touches the real Spark.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services import runtime_manager, runtime_ownership


def _discovery_output(*, total=0, label=None, solo=None, manual=None, project=None) -> str:
    lines = [
        "__TOTAL__", str(total),
        "__LABEL__", *(label or []),
        "__SOLO__", *(solo or []),
        "__MANUAL__", *(manual or []),
        "__PROJECT__", *(f"{cid}|{proj}" for cid, proj in (project or [])),
    ]
    return "\n".join(lines)


def _inspect_output(rows) -> str:
    return "\n".join(f"{cid}\t{slug}\t{nonce}" for cid, slug, nonce in rows)


# ── (a) compose-project discovery finds a nameless compose service ──────────


@pytest.mark.asyncio
async def test_compose_container_without_container_name_is_found_and_stopped():
    """A compose service with no container_name gets a docker-generated id
    (e.g. ``sparkrun-deepseek-v4-flash-1``) that matches neither the label
    nor either name filter. The compose-project sweep must still find it,
    and — carrying no mc.runtime.slug label of its own — it is safe to stop
    without a nonce (there is no ownership claim to verify)."""
    ssh = AsyncMock(side_effect=[
        (_discovery_output(
            total=3,
            project=[("f00dbeef1234", "sparkrun-deepseek-v4-flash")],
        ), "", 0),
        (_inspect_output([("f00dbeef1234", "", "")]), "", 0),
        ("f00dbeef1234", "", 0),
        ("", "", 0),
    ])
    with patch.object(runtime_manager, "_ssh_run", ssh), \
         patch.object(runtime_manager, "_evict_poll_interval", 0):
        result = await runtime_manager.evict_spark_runtime_containers("deepseek-v4-flash")

    assert result["ok"] is True
    assert result["stopped"] == ["f00dbeef1234"]
    assert result.get("blocked") == []


@pytest.mark.asyncio
async def test_compose_project_name_not_matching_pattern_is_ignored():
    """An unrelated compose stack on the same box (a project name that does
    not look like ours or like sparkrun's) must NOT be swept — an unscoped
    label match would risk stopping something MC has nothing to do with."""
    ssh = AsyncMock(
        return_value=(_discovery_output(
            total=2,
            project=[("unrelated123", "some-other-app")],
        ), "", 0)
    )
    with patch.object(runtime_manager, "_ssh_run", ssh):
        result = await runtime_manager.evict_spark_runtime_containers("qwen-general")

    # Nothing matched — the unrelated project must not appear anywhere.
    assert result["ok"] is True
    assert result["stopped"] == []
    assert ssh.call_count == 1  # discovery only — no inspect/stop needed


# ── (b) nonce mismatch/missing blocks the stop, matching nonce allows it ────


@pytest.mark.asyncio
async def test_container_with_mismatched_nonce_is_not_stopped_and_warns():
    """MC recorded nonce 'expected-abc' for this slug at the last switch. A
    container now carrying the same slug label but a DIFFERENT (or absent)
    nonce was not created by this MC instance — most likely hand-recreated
    by an operator under the same name/label — and must be left running."""
    await runtime_ownership.set_nonce("qwen-general", "expected-abc")

    inspect_ssh = AsyncMock(
        side_effect=[
            (_discovery_output(total=1, label=["mismatched1"]), "", 0),
            (_inspect_output([("mismatched1", "qwen-general", "someone-elses-value")]), "", 0),
        ]
    )
    emit_mock = AsyncMock()
    with patch.object(runtime_manager, "_ssh_run", inspect_ssh), \
         patch.object(runtime_manager, "_emit_ownership_blocked_event", emit_mock):
        result = await runtime_manager.evict_spark_runtime_containers("qwen-general")

    assert result["ok"] is False
    assert result["stopped"] == []
    assert len(result["blocked"]) == 1
    assert result["blocked"][0]["container_id"] == "mismatched1"
    emit_mock.assert_awaited_once()
    # Only discovery + inspect were called — never a docker stop.
    assert inspect_ssh.call_count == 2
    for c in inspect_ssh.call_args_list:
        assert not c.args[0].startswith("docker stop")


@pytest.mark.asyncio
async def test_container_with_missing_nonce_label_is_not_stopped_and_warns():
    """Same as the mismatch case but the container carries no nonce label at
    all (e.g. hand-run `docker run` with only the slug label copied over)."""
    await runtime_ownership.set_nonce("qwen-general", "expected-abc")

    ssh = AsyncMock(
        side_effect=[
            (_discovery_output(total=1, label=["nolabel1"]), "", 0),
            (_inspect_output([("nolabel1", "qwen-general", "")]), "", 0),
        ]
    )
    with patch.object(runtime_manager, "_ssh_run", ssh), \
         patch.object(runtime_manager, "_emit_ownership_blocked_event", AsyncMock()):
        result = await runtime_manager.evict_spark_runtime_containers("qwen-general")

    assert result["ok"] is False
    assert result["stopped"] == []
    assert result["blocked"][0]["reason"] == "Container trägt keinen Nonce-Label"


@pytest.mark.asyncio
async def test_own_container_with_matching_nonce_is_stopped():
    """The expected, common case: MC's own container, nonce matches what was
    recorded at the last launch — stopped exactly like before Task #22."""
    await runtime_ownership.set_nonce("qwen-general", "abc-123")

    ssh = AsyncMock(side_effect=[
        (_discovery_output(total=1, label=["ownmatch1"]), "", 0),
        (_inspect_output([("ownmatch1", "qwen-general", "abc-123")]), "", 0),
        ("ownmatch1", "", 0),
        ("", "", 0),
    ])
    with patch.object(runtime_manager, "_ssh_run", ssh), \
         patch.object(runtime_manager, "_evict_poll_interval", 0), \
         patch.object(runtime_manager, "_emit_ownership_blocked_event", AsyncMock()) as emit_mock:
        result = await runtime_manager.evict_spark_runtime_containers("qwen-general")

    assert result["ok"] is True
    assert result["stopped"] == ["ownmatch1"]
    assert result["blocked"] == []
    emit_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_recorded_nonce_is_not_a_block_backward_compat():
    """Before this slug's first post-Task#22 switch, MC never recorded a
    nonce expectation at all. That must NOT block eviction — otherwise every
    runtime already running at deploy time becomes un-evictable until its
    next recipe switch, which is worse than the bug this fixes."""
    # Deliberately no runtime_ownership.set_nonce() call for this slug.
    ssh = AsyncMock(side_effect=[
        (_discovery_output(total=1, label=["legacy1"]), "", 0),
        (_inspect_output([("legacy1", "laguna-s21", "")]), "", 0),
        ("legacy1", "", 0),
        ("", "", 0),
    ])
    with patch.object(runtime_manager, "_ssh_run", ssh), \
         patch.object(runtime_manager, "_evict_poll_interval", 0):
        result = await runtime_manager.evict_spark_runtime_containers("laguna-s21")

    assert result["ok"] is True
    assert result["stopped"] == ["legacy1"]


@pytest.mark.asyncio
async def test_unlabelled_container_needs_no_nonce():
    """A container with no mc.runtime.slug label at all (the CLI-started /
    externally-started case the sweep exists to catch) has no ownership
    claim to verify and is always stoppable — nonce-gating must never reopen
    the original P0 bug this replaced."""
    await runtime_ownership.set_nonce("qwen-general", "abc-123")  # unrelated slug

    ssh = AsyncMock(side_effect=[
        (_discovery_output(total=1, solo=["cli_started_solo"]), "", 0),
        (_inspect_output([("cli_started_solo", "", "")]), "", 0),
        ("cli_started_solo", "", 0),
        ("", "", 0),
    ])
    with patch.object(runtime_manager, "_ssh_run", ssh), \
         patch.object(runtime_manager, "_evict_poll_interval", 0):
        result = await runtime_manager.evict_spark_runtime_containers("qwen-general")

    assert result["ok"] is True
    assert result["stopped"] == ["cli_started_solo"]


# ── (c) diagnostics: "box already free" carries proof, not just silence ────


@pytest.mark.asyncio
async def test_free_box_message_carries_diagnostic_numbers():
    ssh = AsyncMock(return_value=(_discovery_output(total=5), "", 0))
    with patch.object(runtime_manager, "_ssh_run", ssh):
        result = await runtime_manager.evict_spark_runtime_containers("qwen-general")
    assert result["ok"] is True
    assert "5" in result["message"]
    assert "Container insgesamt auf der Box" in result["message"]


# ── runtime_ownership unit-level coverage ────────────────────────────────────


@pytest.mark.asyncio
async def test_set_get_clear_nonce_roundtrip():
    assert await runtime_ownership.get_nonce("some-slug") is None
    await runtime_ownership.set_nonce("some-slug", "value-1")
    assert await runtime_ownership.get_nonce("some-slug") == "value-1"
    await runtime_ownership.clear_nonce("some-slug")
    assert await runtime_ownership.get_nonce("some-slug") is None


def test_new_nonce_is_unique_and_unstructured():
    a = runtime_ownership.new_nonce()
    b = runtime_ownership.new_nonce()
    assert a != b
    assert len(a) >= 16


@pytest.mark.asyncio
async def test_partition_by_ownership_splits_safe_and_blocked():
    await runtime_ownership.set_nonce("slugA", "nonce-a")

    async def fake_ssh_run(cmd, **kwargs):
        return (_inspect_output([
            ("c1", "slugA", "nonce-a"),   # matches — safe
            ("c2", "slugA", "wrong"),      # mismatch — blocked
            ("c3", "", ""),                 # unlabelled — safe
        ]), "", 0)

    safe, blocked = await runtime_ownership.partition_by_ownership(
        ["c1", "c2", "c3"], host=None, ssh_run=fake_ssh_run
    )
    assert safe == ["c1", "c3"]
    assert len(blocked) == 1
    assert blocked[0]["container_id"] == "c2"


# ── sparkrun_manager: nonce stamped at build time, persisted at switch time ──


def test_build_launch_command_appends_nonce_label_after_slug_label():
    from app.services import sparkrun_manager

    cmd = sparkrun_manager.build_launch_command(
        "@official/qwen3.6-35b-a3b-fp8-vllm", slug="qwen-general", nonce="the-nonce"
    )
    assert "--label mc.runtime.slug=qwen-general" in cmd
    assert "--label mc.runtime.nonce=the-nonce" in cmd
    assert cmd.index("mc.runtime.slug") < cmd.index("mc.runtime.nonce")


def test_build_launch_command_without_nonce_is_unchanged():
    from app.services import sparkrun_manager

    cmd = sparkrun_manager.build_launch_command(
        "@official/qwen3.6-35b-a3b-fp8-vllm", slug="qwen-general"
    )
    assert "mc.runtime.nonce" not in cmd


@pytest.mark.asyncio
async def test_switch_recipe_records_nonce_before_persisting_command(
    async_session,
):
    from sqlmodel.ext.asyncio.session import AsyncSession as _AsyncSession  # noqa: F401
    from app.models.runtime import Runtime
    from app.services import sparkrun_manager

    rt = Runtime(
        slug="qwen-general",
        display_name="Spark Qwen vLLM",
        runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1",
        model_identifier="Qwen/Qwen3.6-35B-A3B-FP8",
        launch_command=(
            "uvx sparkrun run @official/qwen3.6-35b-a3b-fp8-vllm "
            "--solo --no-rm --ensure --no-follow --label mc.runtime.slug=qwen-general"
        ),
        container_name=None,
        enabled=True,
    )
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)

    with (
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
            async_session, rt, "@official/qwen3.6-27b-fp8-mtp-vllm"
        )

    assert result["ok"] is True
    stored_nonce = await runtime_ownership.get_nonce("qwen-general")
    assert stored_nonce is not None
    # The persisted launch_command must carry that exact nonce.
    assert f"mc.runtime.nonce={stored_nonce}" in rt.launch_command
