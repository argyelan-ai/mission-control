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

# Captured before the autouse fixture below ever patches the module attribute
# — the PR 10 tests that exercise the wait loop ITSELF call this reference
# directly, so they reach the real function no matter what
# `memprep._wait_for_available_memory` currently points to.
_REAL_WAIT_FOR_AVAILABLE_MEMORY = memprep._wait_for_available_memory


class FakeBox:
    """A box with a watermark, a MemFree value and a container list."""

    def __init__(
        self, *, watermark=CONFIGURED_WATERMARK, arch="aarch64", mem_free=8_000_000,
        mem_available_sequence=None,
    ):
        self.watermark = watermark
        self.arch = arch
        self.mem_free = mem_free
        self.commands: list[str] = []
        self.containers: set[str] = set()
        self.fail_on: str | None = None
        # PR 10: successive `awk MemAvailable` reads pop through this list —
        # the shape of a box whose reclaim genuinely takes a few polls, or (a
        # single-element / empty list) one that never budges.
        self.mem_available_sequence = list(mem_available_sequence or [mem_free])

    async def run(self, command, *, host=None, timeout=None):
        self.commands.append(command)
        if self.fail_on and self.fail_on in command:
            return ("", "boom", 1)

        if "min_free_kbytes" in command and command.startswith("cat"):
            return (str(self.watermark), "", 0)
        if "MemAvailable" in command:
            value = self.mem_available_sequence[0]
            if len(self.mem_available_sequence) > 1:
                self.mem_available_sequence.pop(0)
            return (str(value), "", 0)
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


@pytest.fixture(autouse=True)
def _instant_mem_wait():
    """PR 10's MemAvailable wait defaults to reaching instantly.

    Every pre-existing test in this file exercises prepare_host_memory /
    prepare_for_runtime / start_runtime WITHOUT caring about the wait — and
    since DEFAULT_MIN_AVAILABLE_KB now applies to every exclusive_memory
    runtime whether or not it configures its own threshold, an unpatched
    wait would poll FakeBox's default ~7.6 GiB MemFree against a 20 GiB
    floor for real, in real asyncio.sleep(10) increments, for the real
    3-minute timeout — turning every one of those tests into a multi-minute
    hang. The PR 10 section below re-patches this per test to exercise the
    wait itself; this fixture is what every OTHER test gets for free.
    """
    with patch.object(
        memprep, "_wait_for_available_memory",
        new=AsyncMock(return_value=(True, memprep.DEFAULT_MIN_AVAILABLE_KB)),
    ):
        yield


def _patched(box, fake_redis):
    async def _get_redis():
        return fake_redis

    return (
        patch("app.services.runtime_manager._ssh_run", new=box.run),
        patch.object(memprep, "get_redis", _get_redis),
    )


# ── ssh_credential_id round-trips through PrepHandle (review finding #5,
#    30.08.2026) ─────────────────────────────────────────────────────────────
#
# A host onboarded via services/host_onboarding.py (Fleet & Rezepte v2,
# Phase 2) typically has ssh_key_path=None — its ONLY credential lives in
# the Vault. Crash recovery (recover_orphaned_preps) has no session and no
# runtime left after a backend restart, only what PrepHandle.as_host()
# reconstructs — so if that reconstruction drops ssh_credential_id, such a
# host can never be authenticated to during recovery, no matter how correct
# the rest of the handle is.


def test_as_host_carries_ssh_credential_id_through():
    import uuid

    cred_id = uuid.uuid4()
    handle = memprep.PrepHandle(
        host_key="192.0.2.50", ssh_host="192.0.2.50", ssh_user="mcfleet",
        ssh_key_path=None, ssh_credential_id=str(cred_id),
    )
    resolved = handle.as_host()
    assert resolved.ssh_credential_id == cred_id
    assert resolved.ssh_key_path is None


def test_as_host_tolerates_missing_or_malformed_credential_id():
    handle_missing = memprep.PrepHandle(host_key="192.0.2.50", ssh_host="192.0.2.50")
    assert handle_missing.as_host().ssh_credential_id is None

    handle_bad = memprep.PrepHandle(
        host_key="192.0.2.50", ssh_host="192.0.2.50", ssh_credential_id="not-a-uuid"
    )
    assert handle_bad.as_host().ssh_credential_id is None  # logs a warning, doesn't raise


@pytest.mark.asyncio
async def test_prepare_host_memory_persists_ssh_credential_id_on_the_handle(box, fake_redis):
    """The actual construction site (prepare_host_memory) must populate the
    field from the ResolvedHost it was given — and it must survive the
    to_json/from_json round-trip the handle takes through Redis."""
    import uuid

    cred_id = uuid.uuid4()
    onboarded_host = ResolvedHost(
        ssh_host="192.0.2.50", ssh_user="mcfleet", ssh_key_path=None,
        ssh_credential_id=cred_id, kind="ssh", source="registry",
    )
    ssh, redis = _patched(box, fake_redis)
    with ssh, redis:
        handle = await memprep.prepare_host_memory(onboarded_host, watermark_kb=LOWERED_WATERMARK)

    assert handle.ssh_credential_id == str(cred_id)
    round_tripped = memprep.PrepHandle.from_json(handle.to_json())
    assert round_tripped.as_host().ssh_credential_id == cred_id


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


# ── PR 10: MemAvailable wait ─────────────────────────────────────────────────
#
# The gap live in the reboot test: a crash-looped engine's ~100 GB of NVRM
# allocations had not actually drained three minutes after prepare_host_memory
# reported success (cache dropped, watermark lowered). vLLM saw 11.58 GiB free
# out of 121.69 GiB. The fix is a poll loop with a hard timeout; the tests
# below cover the loop itself (deterministic clock, no real sleeping), the
# handle it leaves behind, and the abort it forces on the start path.

THRESHOLD = 40_000_000  # 40 GiB in kB, arbitrary for these tests


class _FakeClock:
    """A controllable ``now()``/``sleep()`` pair — advances only when the
    wait loop itself calls ``sleep``, so the test asserts real elapsed
    *ticks*, not wall-clock time."""

    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


@pytest.mark.asyncio
async def test_wait_for_available_memory_reaches_the_threshold(box, fake_redis):
    """The box reclaims memory over a few polls — the loop must return as
    soon as the (default) 2-reading stability streak is complete, not spin
    until the timeout, and not stop on the FIRST crossing either."""
    box.mem_available_sequence = [10_000_000, 25_000_000, THRESHOLD, THRESHOLD]
    clock = _FakeClock()
    ssh, redis = _patched(box, fake_redis)
    with ssh, redis:
        reached, last = await _REAL_WAIT_FOR_AVAILABLE_MEMORY(
            SPARK, min_available_kb=THRESHOLD, timeout_seconds=180,
            sleep=clock.sleep, now=clock.now,
        )

    assert reached is True
    assert last == THRESHOLD
    # Reads: 10M (streak 0), 25M (streak 0), THRESHOLD (streak 1 — not
    # enough on its own), THRESHOLD (streak 2 — done). Three sleeps, not
    # one: the default stability window (2) means the first crossing alone
    # is not sufficient.
    assert len(clock.sleeps) == 3


@pytest.mark.asyncio
async def test_wait_for_available_memory_resets_the_streak_on_a_dip(box, fake_redis):
    """Sabotage-relevant: a single reading above the threshold, immediately
    followed by one back below it, must NOT count toward the stability
    streak — this is the exact scenario the stability window exists for
    (a reading that looked fine once and then kept draining)."""
    box.mem_available_sequence = [
        THRESHOLD,       # streak 1
        10_000_000,      # dip — streak resets to 0
        THRESHOLD,       # streak 1 again
        THRESHOLD,       # streak 2 — done
    ]
    clock = _FakeClock()
    ssh, redis = _patched(box, fake_redis)
    with ssh, redis:
        reached, last = await _REAL_WAIT_FOR_AVAILABLE_MEMORY(
            SPARK, min_available_kb=THRESHOLD, timeout_seconds=180,
            stable_readings=2, sleep=clock.sleep, now=clock.now,
        )

    assert reached is True
    assert last == THRESHOLD
    # Four reads were needed, not two — the dip cost the streak everything
    # it had built up, proving a lone good reading cannot short-circuit it.
    assert len(clock.sleeps) == 3


@pytest.mark.asyncio
async def test_wait_for_available_memory_subtracts_the_active_watermark(box, fake_redis):
    """GPU-visible approximation: vm.min_free_kbytes is memory the kernel
    never hands to any allocation, so a reading of raw MemAvailable that
    LOOKS like it clears the threshold must still fail if watermark_kb
    eats the margin — and pass again once MemAvailable actually clears
    threshold + watermark."""
    watermark = 5_000_000
    box.mem_available_sequence = [
        THRESHOLD,               # raw crosses, but effective = THRESHOLD - 5M < THRESHOLD
        THRESHOLD + watermark,   # effective now exactly THRESHOLD — streak 1
        THRESHOLD + watermark,   # streak 2 — done
    ]
    clock = _FakeClock()
    ssh, redis = _patched(box, fake_redis)
    with ssh, redis:
        reached, last = await _REAL_WAIT_FOR_AVAILABLE_MEMORY(
            SPARK, min_available_kb=THRESHOLD, timeout_seconds=180,
            watermark_kb=watermark, stable_readings=2,
            sleep=clock.sleep, now=clock.now,
        )

    assert reached is True
    assert last == THRESHOLD + watermark
    assert len(clock.sleeps) == 2


@pytest.mark.asyncio
async def test_wait_for_available_memory_times_out_on_a_box_that_never_frees(box, fake_redis):
    """Sabotage: the box's MemAvailable never moves. The loop must give up at
    the timeout rather than spin forever."""
    box.mem_available_sequence = [5_000_000]  # constant, never reaches THRESHOLD
    clock = _FakeClock()
    ssh, redis = _patched(box, fake_redis)
    with ssh, redis:
        reached, last = await _REAL_WAIT_FOR_AVAILABLE_MEMORY(
            SPARK, min_available_kb=THRESHOLD, timeout_seconds=30, poll_interval=10,
            sleep=clock.sleep, now=clock.now,
        )

    assert reached is False
    assert last == 5_000_000
    assert clock.t >= 30  # the fake clock actually ran out the budget


@pytest.mark.asyncio
async def test_wait_for_available_memory_survives_an_unreadable_box(fake_redis):
    """Sabotage: MemAvailable can never be read. Not-reached, not a crash —
    and it still consumes the timeout budget instead of returning instantly
    (an unreadable box is not evidence the box is fine)."""
    box = FakeBox()
    box.fail_on = "MemAvailable"
    clock = _FakeClock()
    ssh, redis = _patched(box, fake_redis)
    with ssh, redis:
        reached, last = await _REAL_WAIT_FOR_AVAILABLE_MEMORY(
            SPARK, min_available_kb=THRESHOLD, timeout_seconds=20, poll_interval=10,
            sleep=clock.sleep, now=clock.now,
        )

    assert reached is False
    assert last is None
    assert clock.t >= 20


@pytest.mark.asyncio
async def test_prepare_records_a_successful_wait_on_the_handle(box, fake_redis):
    box.mem_available_sequence = [THRESHOLD]
    ssh, redis = _patched(box, fake_redis)
    with ssh, redis, patch.object(
        memprep, "_wait_for_available_memory",
        new=AsyncMock(return_value=(True, THRESHOLD)),
    ):
        handle = await memprep.prepare_host_memory(
            SPARK, min_available_kb=THRESHOLD, slug="ds4-sparkinfer",
        )

    assert handle.mem_wait_timed_out is False
    assert handle.mem_wait_threshold_kb == THRESHOLD
    assert handle.mem_available_after_wait_kb == THRESHOLD
    # The wait is additive — the existing dropper/watermark mechanics still ran.
    assert memprep.DROPPER_CONTAINER in box.containers


@pytest.mark.asyncio
async def test_prepare_threads_the_lowered_watermark_into_the_wait(box, fake_redis):
    """prepare_host_memory must hand _wait_for_available_memory the watermark
    that is ACTUALLY active on the box after step 3 — the lowered value, not
    the pre-lowering original — since that is what the kernel is holding back
    from the engine during the wait."""
    box.mem_available_sequence = [THRESHOLD]
    ssh, redis = _patched(box, fake_redis)
    wait = AsyncMock(return_value=(True, THRESHOLD))
    with ssh, redis, patch.object(memprep, "_wait_for_available_memory", new=wait):
        await memprep.prepare_host_memory(
            SPARK, watermark_kb=LOWERED_WATERMARK, min_available_kb=THRESHOLD,
            slug="ds4-sparkinfer",
        )

    assert wait.await_args.kwargs["watermark_kb"] == LOWERED_WATERMARK
    assert wait.await_args.kwargs["stable_readings"] == memprep.DEFAULT_STABLE_READINGS


@pytest.mark.asyncio
async def test_prepare_falls_back_to_the_original_watermark_when_lowering_did_not_happen(
    box, fake_redis
):
    """No watermark_kb configured (or the write failed) → nothing was
    lowered. The wait must still account for the ORIGINAL watermark — the
    kernel reserves it either way — not treat it as zero."""
    box.mem_available_sequence = [THRESHOLD]
    ssh, redis = _patched(box, fake_redis)
    wait = AsyncMock(return_value=(True, THRESHOLD))
    with ssh, redis, patch.object(memprep, "_wait_for_available_memory", new=wait):
        await memprep.prepare_host_memory(
            SPARK, min_available_kb=THRESHOLD, slug="ds4-sparkinfer",
        )

    assert wait.await_args.kwargs["watermark_kb"] == CONFIGURED_WATERMARK


@pytest.mark.asyncio
async def test_prepare_records_a_timed_out_wait_on_the_handle(box, fake_redis):
    ssh, redis = _patched(box, fake_redis)
    with ssh, redis, patch.object(
        memprep, "_wait_for_available_memory",
        new=AsyncMock(return_value=(False, 11_000_000)),
    ):
        handle = await memprep.prepare_host_memory(
            SPARK, min_available_kb=THRESHOLD, slug="ds4-sparkinfer",
        )

    assert handle.mem_wait_timed_out is True
    assert handle.mem_wait_threshold_kb == THRESHOLD
    assert handle.mem_available_after_wait_kb == 11_000_000
    # A timed-out wait still leaves the dropper/watermark changes in place —
    # they are undone by finish(), same as any other aborted start.
    assert memprep.DROPPER_CONTAINER in box.containers


@pytest.mark.asyncio
async def test_no_threshold_means_no_wait_at_all(box, fake_redis):
    """Existing callers that never pass min_available_kb (or a runtime that
    is not GB10-applicable) must see byte-for-byte the same behaviour as
    before PR 10 — no MemAvailable read, handle fields all None/False."""
    ssh, redis = _patched(box, fake_redis)
    with ssh, redis:
        handle = await memprep.prepare_host_memory(SPARK, watermark_kb=None)

    assert handle.mem_wait_timed_out is False
    assert handle.mem_wait_threshold_kb is None
    assert not box.ran("MemAvailable")


@pytest.mark.asyncio
async def test_prepare_for_runtime_falls_back_to_the_conservative_default(box, fake_redis):
    """A runtime without prestart_min_available_kb still gets a wait — the
    whole point of PR 10 is that every exclusive_memory GB10 runtime is
    covered, tuned or not."""
    ssh, redis = _patched(box, fake_redis)
    wait = AsyncMock(return_value=(True, memprep.DEFAULT_MIN_AVAILABLE_KB))
    with ssh, redis, patch.object(memprep, "_wait_for_available_memory", new=wait):
        await memprep.prepare_for_runtime(SPARKINFER, host=SPARK)

    assert wait.await_args.kwargs["min_available_kb"] == memprep.DEFAULT_MIN_AVAILABLE_KB


@pytest.mark.asyncio
async def test_prepare_for_runtime_respects_a_configured_threshold(box, fake_redis):
    configured = {**SPARKINFER, "prestart_min_available_kb": 90_000_000}
    ssh, redis = _patched(box, fake_redis)
    wait = AsyncMock(return_value=(True, 90_000_000))
    with ssh, redis, patch.object(memprep, "_wait_for_available_memory", new=wait):
        await memprep.prepare_for_runtime(configured, host=SPARK)

    assert wait.await_args.kwargs["min_available_kb"] == 90_000_000


@pytest.mark.asyncio
async def test_prepare_for_runtime_emits_a_timeout_event(box, fake_redis):
    ssh, redis = _patched(box, fake_redis)
    wait = AsyncMock(return_value=(False, 12_000_000))
    with (
        ssh, redis,
        patch.object(memprep, "_wait_for_available_memory", new=wait),
        patch.object(memprep, "_emit", new=AsyncMock()) as emit,
    ):
        await memprep.prepare_for_runtime(SPARKINFER, host=SPARK)

    events = [call.args[0] for call in emit.await_args_list]
    assert "runtime.memory_prep_started" in events  # the prep itself still happened
    assert "runtime.memory_prep_timeout" in events  # …but the caller must abort


@pytest.mark.asyncio
async def test_start_runtime_aborts_without_calling_impl_when_the_wait_times_out(
    box, fake_redis
):
    """The whole point: a timed-out wait must never reach the actual launch
    command — that is the blind retry the reboot test failed on."""
    from app.services.runtime_manager import start_runtime

    impl = AsyncMock(return_value={"ok": True, "message": "läuft"})
    timed_out_handle = memprep.PrepHandle(
        host_key="192.0.2.10",
        original_watermark_kb=CONFIGURED_WATERMARK,
        lowered_to_kb=LOWERED_WATERMARK,
        dropper_started=True,
        mem_wait_threshold_kb=THRESHOLD,
        mem_available_after_wait_kb=11_000_000,
        mem_wait_timed_out=True,
    )
    box.containers.add(memprep.DROPPER_CONTAINER)
    box.watermark = LOWERED_WATERMARK

    with_ = _start_patches(box, fake_redis, impl)
    with (
        with_[0], with_[1], with_[2], with_[3],
        patch.object(memprep, "prepare_for_runtime",
                     new=AsyncMock(return_value=timed_out_handle)),
        with_[4], with_[5],
    ):
        result = await start_runtime(SPARKINFER, host=SPARK)

    impl.assert_not_awaited()
    assert result["ok"] is False
    assert "nicht rechtzeitig frei" in result["message"]
    # Cleanup still ran — the dropper/watermark the prep changed are undone,
    # same as any other start that never got off the ground.
    assert box.watermark == CONFIGURED_WATERMARK
    assert memprep.DROPPER_CONTAINER not in box.containers
