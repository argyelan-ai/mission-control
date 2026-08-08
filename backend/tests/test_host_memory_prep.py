"""Pre-start memory prep (PR 8) — prepare/finish round-trip and orphan repair.

The thing under test is not "does it run the right commands" — it is "does the
box end up the way it was found". Every test therefore asserts on the RESTORED
state, and the sabotage cases are the ones that matter: an exception mid-start,
a backend that died before it could clean up, and a watermark that was never
readable in the first place.

The SSH layer is mocked with a tiny fake shell: it records every command and
answers the three reads (min_free_kbytes, MemFree, uname) from mutable state,
so a write really does change what a later read returns. Asserting against a
command list alone would happily pass a restore that wrote the wrong number.
"""
from unittest.mock import AsyncMock, patch

import pytest

from app.redis_client import RedisKeys
from app.services import host_memory_prep as memprep
from app.services.host_resolver import ResolvedHost

SPARK = ResolvedHost(ssh_host="192.0.2.10", ssh_user="mc", kind="ssh", source="registry")

CONFIGURED_WATERMARK = 5242880  # 5 GiB — what Mark's Spark carries
LOWERED_WATERMARK = 2097152     # 2 GiB — what the start lowers it to


class FakeBox:
    """A box with a watermark, a MemFree value and a container list."""

    def __init__(self, *, watermark=CONFIGURED_WATERMARK, arch="aarch64", mem_free=8_000_000):
        self.watermark = watermark
        self.arch = arch
        self.mem_free = mem_free
        self.commands: list[str] = []
        self.containers: set[str] = set()
        self.fail_on: str | None = None

    async def run(self, command, *, host=None, timeout=None):
        self.commands.append(command)
        if self.fail_on and self.fail_on in command:
            return ("", "boom", 1)

        if "min_free_kbytes" in command and command.startswith("cat"):
            return (str(self.watermark), "", 0)
        if "MemFree" in command:
            return (str(self.mem_free), "", 0)
        if command.strip() == "uname -m":
            return (self.arch, "", 0)
        if "min_free_kbytes" in command:  # the privileged write
            value = command.rsplit("echo ", 1)[1].split(" ", 1)[0]
            self.watermark = int(value)
            return ("", "", 0)
        if "drop_caches" in command and "-d --name" in command:
            self.containers.add(memprep.DROPPER_CONTAINER)
            return ("id", "", 0)
        if "drop_caches" in command:
            # One-shot drop frees the page cache.
            self.mem_free += 40_000_000
            return ("", "", 0)
        if command.startswith(f"docker rm -f {memprep.DROPPER_CONTAINER}"):
            existed = memprep.DROPPER_CONTAINER in self.containers
            self.containers.discard(memprep.DROPPER_CONTAINER)
            return ("", "" if existed else "No such container", 0 if existed else 1)
        return ("", "", 0)

    def ran(self, needle: str) -> bool:
        return any(needle in c for c in self.commands)


@pytest.fixture
def box():
    return FakeBox()


@pytest.fixture(autouse=True)
def _no_events():
    """The activity feed needs a real session_scope; the prep only ever emits
    best-effort, so silence it rather than stand up a DB for every case."""
    with patch.object(memprep, "_emit", new=AsyncMock()):
        yield


def _patched(box, fake_redis):
    async def _get_redis():
        return fake_redis

    return (
        patch("app.services.runtime_manager._ssh_run", new=box.run),
        patch.object(memprep, "get_redis", _get_redis),
    )


# ── prepare / finish round-trip ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prepare_then_finish_restores_the_exact_original_watermark(box, fake_redis):
    ssh, redis = _patched(box, fake_redis)
    with ssh, redis:
        handle = await memprep.prepare_host_memory(
            SPARK, watermark_kb=LOWERED_WATERMARK, slug="ds4-sparkinfer"
        )

        # During the start window the box is prepared: watermark lowered, page
        # cache dropped once, dropper container running.
        assert box.watermark == LOWERED_WATERMARK
        assert handle.original_watermark_kb == CONFIGURED_WATERMARK
        assert handle.lowered_to_kb == LOWERED_WATERMARK
        assert handle.dropper_started is True
        assert memprep.DROPPER_CONTAINER in box.containers
        assert box.ran("sync; echo 3 > /proc/sys/vm/drop_caches")

        result = await memprep.finish(handle, host=SPARK, success=True)

    # The point of the whole module: the value that was found is the value that
    # comes back — not a hardcoded 5 GiB that happens to match today.
    assert box.watermark == CONFIGURED_WATERMARK
    assert result["restored"] is True
    assert memprep.DROPPER_CONTAINER not in box.containers
    assert await fake_redis.get(RedisKeys.host_mem_prep("192.0.2.10")) is None


@pytest.mark.asyncio
async def test_a_box_with_an_unusual_watermark_gets_that_one_back(fake_redis):
    """Sabotage: the box is NOT configured with the value we would guess."""
    box = FakeBox(watermark=3_333_333)
    ssh, redis = _patched(box, fake_redis)
    with ssh, redis:
        handle = await memprep.prepare_host_memory(SPARK, watermark_kb=LOWERED_WATERMARK)
        assert box.watermark == LOWERED_WATERMARK
        await memprep.finish(handle, host=SPARK, success=True)

    assert box.watermark == 3_333_333


@pytest.mark.asyncio
async def test_finish_runs_after_an_exception_in_the_start(box, fake_redis):
    """The failure this is built for: the start raises. A restore that only
    happens on the happy path is a restore that never happens when it counts."""
    ssh, redis = _patched(box, fake_redis)
    with ssh, redis:
        handle = await memprep.prepare_host_memory(SPARK, watermark_kb=LOWERED_WATERMARK)
        try:
            raise TimeoutError("engine never came up")
        except TimeoutError:
            await memprep.finish(handle, host=SPARK, success=False)

    assert box.watermark == CONFIGURED_WATERMARK
    assert memprep.DROPPER_CONTAINER not in box.containers


@pytest.mark.asyncio
async def test_no_watermark_configured_means_no_watermark_touched(box, fake_redis):
    """NULL prestart_watermark_kb — the default for every existing runtime —
    still drops the page cache but must not rewrite a kernel setting."""
    ssh, redis = _patched(box, fake_redis)
    with ssh, redis:
        handle = await memprep.prepare_host_memory(SPARK, watermark_kb=None)

    assert box.watermark == CONFIGURED_WATERMARK
    assert handle.lowered_to_kb is None
    assert handle.dropper_started is True
    assert box.ran("drop_caches")


@pytest.mark.asyncio
async def test_a_higher_target_is_not_treated_as_lowering(fake_redis):
    """Sabotage: a misconfigured 8 GiB target on a 5 GiB box. Raising the
    watermark would take memory AWAY from the engine — the opposite of the job."""
    box = FakeBox(watermark=CONFIGURED_WATERMARK)
    ssh, redis = _patched(box, fake_redis)
    with ssh, redis:
        handle = await memprep.prepare_host_memory(SPARK, watermark_kb=8_388_608)

    assert box.watermark == CONFIGURED_WATERMARK
    assert handle.lowered_to_kb is None


@pytest.mark.asyncio
async def test_an_unreadable_watermark_is_never_lowered(fake_redis):
    """Sabotage: /proc/sys/vm/min_free_kbytes cannot be read. Lowering a value
    we could not read means we could not put it back — so we do not touch it."""
    box = FakeBox()
    box.fail_on = "cat /proc/sys/vm/min_free_kbytes"
    ssh, redis = _patched(box, fake_redis)
    with ssh, redis:
        handle = await memprep.prepare_host_memory(SPARK, watermark_kb=LOWERED_WATERMARK)

    assert handle.original_watermark_kb is None
    assert handle.lowered_to_kb is None
    assert not any("echo 2097152" in c for c in box.commands)


@pytest.mark.asyncio
async def test_the_dropper_is_removed_before_it_is_started(box, fake_redis):
    """Idempotence: a leftover dropper from a crashed run must not survive as a
    second one. The `rm -f` comes first, always."""
    ssh, redis = _patched(box, fake_redis)
    with ssh, redis:
        await memprep.prepare_host_memory(SPARK, watermark_kb=None)

    rm_index = next(i for i, c in enumerate(box.commands) if c.startswith("docker rm -f"))
    run_index = next(i for i, c in enumerate(box.commands) if "-d --name" in c)
    assert rm_index < run_index


# ── applicability ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prep_applies_only_to_exclusive_runtimes_on_aarch64(box, fake_redis):
    ssh, redis = _patched(box, fake_redis)
    with ssh, redis:
        assert await memprep.applies_to({"exclusive_memory": True}, SPARK) is True
        assert await memprep.applies_to({"exclusive_memory": False}, SPARK) is False


@pytest.mark.asyncio
async def test_an_x86_box_is_left_alone(fake_redis):
    """On a box with discrete VRAM the engine does not size its KV cache
    against host MemFree, so the whole dance would be cost without benefit."""
    box = FakeBox(arch="x86_64")
    ssh, redis = _patched(box, fake_redis)
    with ssh, redis:
        assert await memprep.applies_to({"exclusive_memory": True}, SPARK) is False
        handle = await memprep.prepare_for_runtime({"exclusive_memory": True}, host=SPARK)

    assert handle is None
    assert not box.ran("drop_caches")


@pytest.mark.asyncio
async def test_arch_probe_is_cached_per_host(box, fake_redis):
    ssh, redis = _patched(box, fake_redis)
    with ssh, redis:
        await memprep.host_arch(SPARK)
        await memprep.host_arch(SPARK)

    assert sum(1 for c in box.commands if c.strip() == "uname -m") == 1
    assert await fake_redis.get(RedisKeys.host_arch("192.0.2.10")) == "aarch64"


# ── orphan repair ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_orphaned_prep_is_repaired_by_the_sweep(box, fake_redis):
    """The scenario: the backend dies between prepare and finish. Nobody is
    left holding the original watermark except the handle in Redis."""
    ssh, redis = _patched(box, fake_redis)
    with ssh, redis:
        handle = await memprep.prepare_host_memory(SPARK, watermark_kb=LOWERED_WATERMARK)
        assert box.watermark == LOWERED_WATERMARK

        # …backend restart. Age the handle past the 30-minute cutoff.
        handle.started_at = "2020-01-01T00:00:00+00:00"
        await fake_redis.set(RedisKeys.host_mem_prep(handle.host_key), handle.to_json())

        repaired = await memprep.recover_orphaned_preps(fake_redis)

    assert repaired == ["192.0.2.10"]
    assert box.watermark == CONFIGURED_WATERMARK
    assert memprep.DROPPER_CONTAINER not in box.containers
    assert await fake_redis.get(RedisKeys.host_mem_prep("192.0.2.10")) is None


@pytest.mark.asyncio
async def test_a_prep_still_in_flight_is_not_undone(box, fake_redis):
    """Sabotage: a 12-minute cold load is normal on this box. Restoring the
    watermark underneath a loading engine would cause the exact OOM the prep
    exists to prevent — so only handles older than 30 minutes are touched."""
    ssh, redis = _patched(box, fake_redis)
    with ssh, redis:
        await memprep.prepare_host_memory(SPARK, watermark_kb=LOWERED_WATERMARK)
        repaired = await memprep.recover_orphaned_preps(fake_redis)

    assert repaired == []
    assert box.watermark == LOWERED_WATERMARK
    assert await fake_redis.get(RedisKeys.host_mem_prep("192.0.2.10")) is not None


@pytest.mark.asyncio
async def test_a_handle_with_an_unreadable_timestamp_is_repaired_not_skipped(box, fake_redis):
    """Skipping it would pin a lowered watermark on the box forever. Restoring
    one prep too early is recoverable; never restoring is not."""
    ssh, redis = _patched(box, fake_redis)
    with ssh, redis:
        handle = await memprep.prepare_host_memory(SPARK, watermark_kb=LOWERED_WATERMARK)
        handle.started_at = "not-a-timestamp"
        await fake_redis.set(RedisKeys.host_mem_prep(handle.host_key), handle.to_json())
        await memprep.recover_orphaned_preps(fake_redis)

    assert box.watermark == CONFIGURED_WATERMARK


@pytest.mark.asyncio
async def test_a_failed_restore_keeps_the_handle_for_the_next_sweep(box, fake_redis):
    """Sabotage: the restore write itself fails. Dropping the handle then would
    throw away the only record of the original value."""
    ssh, redis = _patched(box, fake_redis)
    with ssh, redis:
        handle = await memprep.prepare_host_memory(SPARK, watermark_kb=LOWERED_WATERMARK)
        box.fail_on = f"echo {CONFIGURED_WATERMARK}"
        result = await memprep.finish(handle, host=SPARK, success=True)

    assert result["restored"] is False
    assert await fake_redis.get(RedisKeys.host_mem_prep("192.0.2.10")) is not None


@pytest.mark.asyncio
async def test_finish_for_host_is_idempotent_without_a_handle(box, fake_redis):
    ssh, redis = _patched(box, fake_redis)
    with ssh, redis:
        assert await memprep.finish_for_host(SPARK, success=True) is False


# ── Wiring into the start path ───────────────────────────────────────────────

SPARKINFER = {
    "id": "ds4-sparkinfer",
    "slug": "ds4-sparkinfer",
    "display_name": "DeepSeek V4 Flash",
    "runtime_type": "vllm_docker",
    "container_name": "mc-ds4-sparkinfer",
    "launch_command": "docker compose up -d",
    "exclusive_memory": True,
    "prestart_watermark_kb": LOWERED_WATERMARK,
}


def _start_patches(box, fake_redis, impl):
    from app.services import runtime_manager

    async def _get_redis():
        return fake_redis

    return (
        patch("app.services.runtime_manager._ssh_run", new=box.run),
        patch.object(memprep, "get_redis", _get_redis),
        patch("app.services.runtime_grace.get_redis", _get_redis),
        patch.object(runtime_manager, "_start_runtime_impl", new=impl),
        patch.object(runtime_manager, "ensure_exclusive_host",
                     new=AsyncMock(return_value={"ok": True, "message": "frei", "stopped": []})),
        patch.object(runtime_manager, "_emit_exclusive_event", new=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_a_successful_start_leaves_the_prep_for_the_watcher(box, fake_redis):
    """`docker compose up` returns in seconds; the engine then spends minutes
    pulling weights and only THEN sizes its KV cache. Removing the dropper here
    would take it away exactly before the measurement it exists for."""
    from app.services.runtime_manager import start_runtime

    impl = AsyncMock(return_value={"ok": True, "message": "läuft"})
    with_ = _start_patches(box, fake_redis, impl)
    with with_[0], with_[1], with_[2], with_[3], with_[4], with_[5]:
        result = await start_runtime(SPARKINFER, host=SPARK)

    assert result["ok"] is True
    assert box.watermark == LOWERED_WATERMARK           # still prepared
    assert memprep.DROPPER_CONTAINER in box.containers  # still dropping
    assert await fake_redis.get(RedisKeys.host_mem_prep("192.0.2.10")) is not None


@pytest.mark.asyncio
async def test_a_failed_start_undoes_the_prep_immediately(box, fake_redis):
    """Nothing is loading, so nothing needs the box held open."""
    from app.services.runtime_manager import start_runtime

    impl = AsyncMock(return_value={"ok": False, "message": "kein Container erschienen"})
    with_ = _start_patches(box, fake_redis, impl)
    with with_[0], with_[1], with_[2], with_[3], with_[4], with_[5]:
        result = await start_runtime(SPARKINFER, host=SPARK)

    assert result["ok"] is False
    assert box.watermark == CONFIGURED_WATERMARK
    assert memprep.DROPPER_CONTAINER not in box.containers
    assert await fake_redis.get(RedisKeys.host_mem_prep("192.0.2.10")) is None


@pytest.mark.asyncio
async def test_a_raising_start_still_undoes_the_prep(box, fake_redis):
    """Sabotage: SSH dies mid-start. The exception must reach the caller AND
    the box must be back to normal — a lowered watermark nobody knows about is
    the worst outcome of the three."""
    from app.services.runtime_manager import start_runtime

    impl = AsyncMock(side_effect=OSError("ssh connection reset"))
    with_ = _start_patches(box, fake_redis, impl)
    with with_[0], with_[1], with_[2], with_[3], with_[4], with_[5]:
        with pytest.raises(OSError, match="ssh connection reset"):
            await start_runtime(SPARKINFER, host=SPARK)

    assert box.watermark == CONFIGURED_WATERMARK
    assert memprep.DROPPER_CONTAINER not in box.containers


@pytest.mark.asyncio
async def test_a_non_exclusive_runtime_start_touches_nothing(box, fake_redis):
    from app.services.runtime_manager import start_runtime

    plain = {**SPARKINFER, "exclusive_memory": False, "prestart_watermark_kb": None}
    impl = AsyncMock(return_value={"ok": True, "message": "läuft"})
    with_ = _start_patches(box, fake_redis, impl)
    with with_[0], with_[1], with_[2], with_[3], with_[4], with_[5]:
        await start_runtime(plain, host=SPARK)

    assert box.watermark == CONFIGURED_WATERMARK
    assert not box.ran("drop_caches")
