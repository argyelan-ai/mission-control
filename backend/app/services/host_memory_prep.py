"""Pre-start host memory preparation for exclusive_memory runtimes (PR 8).

Why this exists
---------------
The live session that finally got DeepSeek V4 Flash serving on the DGX Spark
did not fail on a flag or a recipe — it failed on arithmetic. vLLM decides at
start time how much KV cache it may allocate, and it decides it from MemFree as
CUDA sees it. On a GB10 (unified memory, no separate VRAM pool) three things
are subtracted from that number before the engine ever sees it:

* the **page cache**, which a ~100 GB weight download has just filled to the
  brim — clean, reclaimable, and completely invisible to the engine's check,
* ``vm.min_free_kbytes``, the kernel's "never hand out the last N" watermark,
  which on this box sits at 5 GiB (raised in July as crash protection),
* the desktop/baseline footprint.

The engine does not reclaim any of it. It reads MemFree, finds too little, and
dies — or worse, comes up with a KV cache so small the model is useless. The
manual fix was three commands before the start and two after. This module is
those five commands, in a shape that cannot forget the "after" half.

Why prepare also WAITS (PR 10)
-------------------------------
The five commands are not always enough by themselves. The live reboot test
that motivated this: a crash-looped engine was stopped, but the crashed
process still held ~100 GB of NVRM allocations three minutes later when MC's
next start ran. The prep itself reported success — cache dropped, watermark
lowered — and the start went ahead anyway, straight into the same failure:
vLLM saw 11.58 GiB free out of 121.69 GiB total. Page cache and the watermark
were never the problem that time; a slow OS-level reclaim after the kill was.
So prepare additionally *waits*, up to a timeout, for ``MemAvailable`` (not
MemFree — the dropper is still running and inflating MemFree with page cache
that has not actually been reclaimed yet; MemAvailable already discounts
that) to clear a threshold before it hands control back. A start that goes
ahead anyway despite the timeout is the exact blind retry that produced the
original failure, so the caller (``runtime_manager.start_runtime``) aborts
instead — see :attr:`PrepHandle.mem_wait_timed_out`.

How, without sudo
-----------------
MC's SSH user is in the ``docker`` group, which is root-equivalent on that box.
So every privileged operation runs as a throwaway ``--privileged`` container
rather than assuming a passwordless sudo that may not exist:

* one-shot drop: ``sync; echo 3 > /proc/sys/vm/drop_caches``
* a **continuous dropper** for the whole start window (the download/coalesce
  phase refills the cache while the engine is loading) — a named container so
  it is idempotent and, more importantly, findable and removable by anyone
* the watermark, lowered and then **restored to the value that was read before
  lowering**. Never to a hardcoded default: what this box is configured with is
  the operator's decision, and a start must not quietly rewrite it.

Why the handle lives in Redis
-----------------------------
The dangerous state is not the drop — that is instantaneous and harmless. It is
the lowered watermark and the running dropper: if the backend restarts between
prepare and finish, the box keeps a 2 GiB watermark and a container spinning
``sync`` every second, forever, with nobody left who knows the original value.
So the handle is written to ``mc:host-memprep:{host}`` BEFORE anything is
changed, and :func:`recover_orphaned_preps` (called from the runtime watcher)
repairs any handle older than 30 minutes.

Everything is best-effort in ONE direction only: a failure to *prepare* logs and
lets the start proceed (a start that might have worked beats a start that never
happened), while a failure to *restore* is retried by the orphan sweep. The
restore path itself runs in every exit path — success, failure and exception.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from app.redis_client import RedisKeys, get_redis
from app.services.host_resolver import ResolvedHost

logger = logging.getLogger("mc.host_memory_prep")

#: Throwaway privileged container image. Alpine because it is ~4 MB and already
#: on every box that has ever run a compose stack.
DROPPER_IMAGE = "alpine"

#: Name of the continuous dropper. Fixed on purpose: one per box is enough, and
#: a fixed name makes the cleanup idempotent (``docker rm -f`` before every
#: start) and the leftover findable by a human with ``docker ps``.
DROPPER_CONTAINER = "mc-cache-dropper"

#: Dropping ~100 GB of page cache is not instant, and neither is pulling the
#: alpine image the first time.
_DROP_TIMEOUT = 180
_SHORT_TIMEOUT = 30

#: A start window that has not finished after this long is not a start window
#: any more — it is a crashed backend. The orphan sweep restores it.
ORPHAN_MAX_AGE = timedelta(minutes=30)

#: How long ``prepare_host_memory`` waits for MemAvailable to clear the
#: threshold before giving up (PR 10). ``~3 min`` per the live reboot test:
#: long enough for a killed engine's allocations to actually drain, short
#: enough that a genuinely stuck box does not hold a start hostage forever.
#: Overridable via ``settings.memory_prep_wait_timeout_seconds``.
DEFAULT_MEM_WAIT_TIMEOUT = 180
_MEM_WAIT_POLL_INTERVAL = 10

#: Conservative floor when a runtime does not configure
#: ``prestart_min_available_kb`` (PR 10). The schema has no
#: gpu_memory_utilization/model-size column to derive a precise figure from,
#: so this is deliberately generic: bigger than the smallest KV-cache-only
#: failure observed in practice (~12 GiB, see test fixtures), small enough
#: not to block a legitimately tight box. A runtime with a known footprint
#: should configure its own value rather than lean on this default.
DEFAULT_MIN_AVAILABLE_KB = 20 * 1024 * 1024  # 20 GiB

#: How many CONSECUTIVE polls must clear the threshold before the wait
#: declares victory (PR 10 follow-up). A single crossing is not enough: the
#: reboot test's failure mode was a number that looked fine for one reading
#: and then kept draining (a container mid-teardown, or the dropper's own
#: ``sync`` still catching up) — starting on that first good reading is the
#: same blind bet the wait exists to avoid, just moved one poll later.
#: Overridable via ``settings.memory_prep_stable_readings``.
DEFAULT_STABLE_READINGS = 2

#: Architectures the GB10 memory equation applies to. An x86 box with discrete
#: VRAM does not size its KV cache against host MemFree, so touching its page
#: cache and watermark would be cost without benefit.
GB10_ARCHS = ("aarch64", "arm64")

#: uname is stable until reboot; a day is a conservative cache.
_ARCH_TTL = 24 * 3600


@dataclass
class PrepHandle:
    """What was changed on a host, and what has to be put back.

    Serialisable by construction — this is what lands in Redis, and what the
    orphan sweep has to be able to act on without any in-process state. That is
    why the SSH coordinates are carried along: after a backend restart there is
    no runtime, no session and no resolved host left to ask.
    """

    host_key: str
    #: The value found BEFORE anything was lowered. This, and only this, is
    #: what gets restored. ``None`` = could not be read → do not touch.
    original_watermark_kb: int | None = None
    #: What it was lowered to, or ``None`` when no lowering happened (no
    #: prestart_watermark_kb configured, or it was not actually lower).
    lowered_to_kb: int | None = None
    dropper_started: bool = False
    started_at: str = ""
    slug: str | None = None
    mem_free_before_kb: int | None = None
    ssh_host: str | None = None
    ssh_user: str | None = None
    ssh_key_path: str | None = None
    #: Phase 2 review finding #5 (30.08.2026): a host onboarded via
    #: services/host_onboarding.py typically has ssh_key_path=None — its ONLY
    #: credential lives in the Vault, referenced by this id. Without it,
    #: as_host() reconstructs a ResolvedHost that can't authenticate at all
    #: for such a host, which is exactly the crash-recovery path this handle
    #: exists for (see the class docstring: "after a backend restart there
    #: is no runtime, no session and no resolved host left to ask"). Stored
    #: as str (not uuid.UUID) — this dataclass round-trips through
    #: json.dumps(asdict(self)), which doesn't know how to serialize a UUID.
    ssh_credential_id: str | None = None
    #: PR 10 — the MemAvailable floor this start waited for, or ``None`` when
    #: no wait was requested (e.g. the runtime is not GB10-applicable).
    mem_wait_threshold_kb: int | None = None
    #: The last MemAvailable reading when the wait ended, whichever way.
    mem_available_after_wait_kb: int | None = None
    #: True when the wait timed out without reaching the threshold. The
    #: caller (runtime_manager.start_runtime) must treat this as "abort the
    #: start" — the whole point is no blind attempt on a box that never
    #: actually freed up.
    mem_wait_timed_out: bool = False

    @property
    def changed_anything(self) -> bool:
        return self.dropper_started or self.lowered_to_kb is not None

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str | bytes) -> PrepHandle | None:
        if isinstance(raw, bytes):
            raw = raw.decode()
        try:
            doc = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(doc, dict):
            return None
        known = {f for f in cls.__dataclass_fields__}  # noqa: SLF001 — dataclass API
        return cls(**{k: v for k, v in doc.items() if k in known})

    def as_host(self) -> ResolvedHost:
        credential_id: uuid.UUID | None = None
        if self.ssh_credential_id:
            try:
                credential_id = uuid.UUID(self.ssh_credential_id)
            except ValueError:
                logger.warning(
                    "PrepHandle %s: ssh_credential_id %r ist keine gültige UUID — ignoriert.",
                    self.host_key, self.ssh_credential_id,
                )
        return ResolvedHost(
            ssh_host=self.ssh_host,
            ssh_user=self.ssh_user,
            ssh_key_path=self.ssh_key_path,
            ssh_credential_id=credential_id,
            kind="ssh",
            source="memprep_handle",
        )


def host_key(host: ResolvedHost | None) -> str:
    """Stable per-box identity for the Redis key.

    ``ssh_host`` rather than the registry slug: the thing being modified is a
    kernel setting on a machine, and two runtime rows pointing at the same IP
    are the same machine even when they resolve through different chain stages.
    """
    if host is None or not host.ssh_host:
        return "default"
    return str(host.ssh_host)


async def _ssh(command: str, *, host: ResolvedHost | None, timeout: float) -> tuple[str, str, int]:
    # Imported lazily: runtime_manager imports this module for the start path,
    # so a module-level import would close the cycle.
    from app.services.runtime_manager import _ssh_run  # noqa: SLF001

    return await _ssh_run(command, host=host, timeout=timeout)


async def read_mem_free_kb(host: ResolvedHost | None) -> int | None:
    """MemFree in kB, or ``None`` when it could not be read.

    MemFree, not MemAvailable, deliberately: MemAvailable counts reclaimable
    page cache as available, which is exactly the optimism that made the engine
    fail. The number MC reports is the number the engine reads.
    """
    try:
        stdout, _, exit_code = await _ssh(
            "awk '/^MemFree:/ {print $2}' /proc/meminfo", host=host, timeout=_SHORT_TIMEOUT
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("memprep: MemFree unreadable: %s", exc)
        return None
    if exit_code != 0:
        return None
    try:
        return int(stdout.strip())
    except (TypeError, ValueError):
        return None


async def read_mem_available_kb(host: ResolvedHost | None) -> int | None:
    """MemAvailable in kB, or ``None`` when it could not be read.

    MemAvailable, not MemFree, deliberately here — the opposite choice from
    :func:`read_mem_free_kb`. That function reads what the ENGINE reads at
    start time (pessimistic on purpose). This one is used only to decide
    whether it is worth attempting a start at all, while the continuous
    cache dropper is still running and inflating MemFree with page cache that
    has not actually drained yet. MemAvailable already discounts that, which
    is exactly the more honest number while waiting for a killed process's
    allocations to clear.
    """
    try:
        stdout, _, exit_code = await _ssh(
            "awk '/^MemAvailable:/ {print $2}' /proc/meminfo", host=host, timeout=_SHORT_TIMEOUT
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("memprep: MemAvailable unreadable: %s", exc)
        return None
    if exit_code != 0:
        return None
    try:
        return int(stdout.strip())
    except (TypeError, ValueError):
        return None


async def read_watermark_kb(host: ResolvedHost | None) -> int | None:
    """Current ``vm.min_free_kbytes``, or ``None`` when it could not be read.

    A ``None`` here disables the whole watermark half of the prep: lowering a
    value we could not read means we could not put it back either.
    """
    try:
        stdout, _, exit_code = await _ssh(
            "cat /proc/sys/vm/min_free_kbytes", host=host, timeout=_SHORT_TIMEOUT
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("memprep: watermark unreadable: %s", exc)
        return None
    if exit_code != 0:
        return None
    try:
        return int(stdout.strip())
    except (TypeError, ValueError):
        return None


async def _write_watermark_kb(host: ResolvedHost | None, value_kb: int) -> bool:
    stdout, stderr, exit_code = await _ssh(
        f"docker run --rm --privileged {DROPPER_IMAGE} sh -c "
        f"'echo {int(value_kb)} > /proc/sys/vm/min_free_kbytes'",
        host=host,
        timeout=_SHORT_TIMEOUT,
    )
    if exit_code != 0:
        logger.warning(
            "memprep: setting min_free_kbytes=%s failed (exit %s): %s",
            value_kb, exit_code, stderr or stdout,
        )
    return exit_code == 0


async def _drop_caches_once(host: ResolvedHost | None) -> bool:
    _, stderr, exit_code = await _ssh(
        f"docker run --rm --privileged {DROPPER_IMAGE} sh -c "
        f"'sync; echo 3 > /proc/sys/vm/drop_caches'",
        host=host,
        timeout=_DROP_TIMEOUT,
    )
    if exit_code != 0:
        logger.warning("memprep: drop_caches failed (exit %s): %s", exit_code, stderr)
    return exit_code == 0


async def _start_dropper(host: ResolvedHost | None) -> bool:
    """Keep the page cache down for the whole start window.

    One drop before the launch is not enough: the container pulls, unpacks and
    coalesces weights while the engine is still deciding how much KV cache it
    may have, and every one of those reads refills the cache. The loop is
    crude on purpose — a second of granularity, no state, and removable with a
    single ``docker rm -f`` by MC or by a human.
    """
    await _ssh(
        f"docker rm -f {DROPPER_CONTAINER} 2>/dev/null || true",
        host=host,
        timeout=_SHORT_TIMEOUT,
    )
    _, stderr, exit_code = await _ssh(
        f"docker run -d --name {DROPPER_CONTAINER} --privileged {DROPPER_IMAGE} sh -c "
        f"'while true; do sync; echo 3 > /proc/sys/vm/drop_caches; sleep 1; done'",
        host=host,
        timeout=_SHORT_TIMEOUT,
    )
    if exit_code != 0:
        logger.warning("memprep: cache dropper did not start (exit %s): %s", exit_code, stderr)
    return exit_code == 0


async def _stop_dropper(host: ResolvedHost | None) -> bool:
    _, stderr, exit_code = await _ssh(
        f"docker rm -f {DROPPER_CONTAINER}", host=host, timeout=_SHORT_TIMEOUT
    )
    if exit_code != 0:
        # "No such container" is the normal outcome when the dropper never
        # started or somebody removed it by hand — not worth a warning.
        logger.debug("memprep: dropper removal returned %s: %s", exit_code, stderr)
    return exit_code == 0


async def _wait_for_available_memory(
    host: ResolvedHost | None,
    *,
    min_available_kb: int,
    timeout_seconds: int,
    poll_interval: int = _MEM_WAIT_POLL_INTERVAL,
    watermark_kb: int = 0,
    stable_readings: int = DEFAULT_STABLE_READINGS,
    sleep=asyncio.sleep,
    now=time.monotonic,
) -> tuple[bool, int | None]:
    """Poll MemAvailable until it clears ``min_available_kb`` (adjusted for the
    watermark reserve) for ``stable_readings`` polls in a row, or time runs out.

    Two refinements on top of "poll MemAvailable once":

    **GPU-visible approximation, not raw MemAvailable.** ``nvidia-smi
    --query-gpu=memory.used`` returns ``[N/A]`` on the GB10 — verified live,
    there is no direct per-process GPU-memory number to read on this box, so
    there is no way to ask the driver "how much is free" the way an x86 box
    with discrete VRAM could. What IS known: ``vm.min_free_kbytes`` (the
    watermark this module lowers and restores) is memory the kernel never
    hands to *any* allocation, engine included, regardless of what
    MemAvailable estimates. Subtracting the currently-active watermark from
    the MemAvailable reading before comparing it to ``min_available_kb`` is
    therefore a closer stand-in for "what the engine can actually get" than
    the raw kernel figure — but it IS an approximation, not a measurement of
    the engine's own view. It has not been validated against an actual vLLM
    "Free memory on device cuda:0" reading; that comparison is a live-test
    task, not something provable from here.

    **Stability window, not a single crossing.** A reading that clears the
    threshold once and then drops back (a container mid-teardown still
    releasing, the dropper's own ``sync`` catching up) is not evidence the
    box is actually ready — it is the same kind of premature-success signal
    that caused the original bug (prep reported success, start went ahead,
    OOM three minutes later). ``stable_readings`` consecutive polls must
    each clear the (watermark-adjusted) threshold before the wait returns
    success; any poll that falls back below it resets the streak to zero.

    Returns ``(reached, last_reading)`` — ``last_reading`` is the raw
    MemAvailable value (not watermark-adjusted), which is what
    :class:`PrepHandle` records for operators to read directly against
    ``/proc/meminfo``. A read that fails counts as "streak broken, not
    reached yet" rather than raising or aborting early — SSH can be flaky
    for a beat right after a container was force-stopped — but it still
    consumes the timeout budget: a box that never answers at all times out
    exactly like a box that never frees memory, which is the correct outcome
    (no signal is not permission to start blind).

    ``sleep`` and ``now`` are injectable so tests can drive the timeout
    deterministically without a real wall-clock wait.
    """
    deadline = now() + timeout_seconds
    last: int | None = None
    consecutive = 0
    stable_readings = max(1, stable_readings)
    while True:
        last = await read_mem_available_kb(host)
        effective = (last - watermark_kb) if last is not None else None
        if effective is not None and effective >= min_available_kb:
            consecutive += 1
            if consecutive >= stable_readings:
                return True, last
        else:
            consecutive = 0
        if now() >= deadline:
            return False, last
        await sleep(poll_interval)


# ── Applicability ────────────────────────────────────────────────────────────


async def host_arch(host: ResolvedHost | None) -> str | None:
    """``uname -m`` of the box, cached in Redis for a day.

    Prefers an ``arch`` attribute if the hosts table ever grows one — until
    then a probe is the only honest answer, and it is cheap once per day.
    """
    declared = getattr(host, "arch", None)
    if declared:
        return str(declared)

    key = RedisKeys.host_arch(host_key(host))
    try:
        redis = await get_redis()
        cached = await redis.get(key)
        if cached:
            return cached.decode() if isinstance(cached, bytes) else str(cached)
    except Exception as exc:  # noqa: BLE001 — a cache miss is not a failure
        logger.debug("memprep: arch cache unavailable: %s", exc)
        redis = None

    try:
        stdout, _, exit_code = await _ssh("uname -m", host=host, timeout=_SHORT_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        logger.debug("memprep: uname failed: %s", exc)
        return None
    if exit_code != 0 or not stdout.strip():
        return None
    arch = stdout.strip()
    if redis is not None:
        try:
            await redis.setex(key, _ARCH_TTL, arch)
        except Exception as exc:  # noqa: BLE001
            logger.debug("memprep: arch cache write failed: %s", exc)
    return arch


async def applies_to(runtime: dict, host: ResolvedHost | None) -> bool:
    """Should this start get the memory dance?

    Narrow by design: only a runtime that claims the whole box, only on an SSH
    host, only on the architecture where host MemFree is what the engine reads.
    Anything else and the prep would be a privileged container run for nothing.
    """
    if not runtime.get("exclusive_memory"):
        return False
    if host is not None and host.kind not in (None, "ssh"):
        return False
    arch = await host_arch(host)
    return bool(arch) and arch in GB10_ARCHS


# ── Prepare / finish ─────────────────────────────────────────────────────────


async def prepare_host_memory(
    host: ResolvedHost | None,
    *,
    watermark_kb: int | None = None,
    slug: str | None = None,
    min_available_kb: int | None = None,
    wait_timeout_seconds: int = DEFAULT_MEM_WAIT_TIMEOUT,
) -> PrepHandle:
    """Free the box's memory for an imminent start. Never raises.

    Order matters and is the whole point:

    1. read the CURRENT watermark — before anything is changed, because this is
       the value that has to come back,
    2. persist the handle — before anything is changed, because a crash after
       step 3 with no handle is exactly the state nobody can repair,
    3. lower the watermark (only when a target is configured AND it is actually
       lower — "lowering" to a higher value would be a silent config change),
    4. drop the page cache once,
    5. start the continuous dropper for the load window,
    6. (PR 10) when ``min_available_kb`` is given, wait for MemAvailable to
       clear it — see :attr:`PrepHandle.mem_wait_timed_out` for what a
       timeout means to the caller.

    A failure in any of 3–5 is logged and the start continues: this is an
    optimisation of the conditions, not a precondition. Step 6 is different —
    a timeout is recorded on the handle precisely so the caller CAN turn it
    into a precondition, because "the box reported free but was not" is the
    live failure this exists to prevent.
    """
    handle = PrepHandle(
        host_key=host_key(host),
        started_at=datetime.now(timezone.utc).isoformat(),
        slug=slug,
        ssh_host=host.ssh_host if host else None,
        ssh_user=host.ssh_user if host else None,
        ssh_key_path=host.ssh_key_path if host else None,
        ssh_credential_id=str(host.ssh_credential_id) if host and host.ssh_credential_id else None,
    )

    handle.original_watermark_kb = await read_watermark_kb(host)
    handle.mem_free_before_kb = await read_mem_free_kb(host)
    await _store_handle(handle)

    try:
        if (
            watermark_kb
            and handle.original_watermark_kb is not None
            and int(watermark_kb) < handle.original_watermark_kb
        ):
            if await _write_watermark_kb(host, int(watermark_kb)):
                handle.lowered_to_kb = int(watermark_kb)
                await _store_handle(handle)
        elif watermark_kb and handle.original_watermark_kb is None:
            logger.warning(
                "memprep: refusing to lower min_free_kbytes on %s — the current "
                "value could not be read, so it could not be restored either",
                handle.host_key,
            )

        await _drop_caches_once(host)
        handle.dropper_started = await _start_dropper(host)
        await _store_handle(handle)

        if min_available_kb:
            # The watermark actually in effect on the box right now — lowered
            # if step 3 above succeeded, otherwise whatever was read as the
            # original. Either way it is memory the kernel will not hand to
            # the engine, so it belongs in the GPU-visible approximation
            # (see _wait_for_available_memory's docstring).
            active_watermark_kb = (
                handle.lowered_to_kb
                if handle.lowered_to_kb is not None
                else (handle.original_watermark_kb or 0)
            )
            reached, last = await _wait_for_available_memory(
                host,
                min_available_kb=int(min_available_kb),
                timeout_seconds=wait_timeout_seconds,
                watermark_kb=active_watermark_kb,
                stable_readings=_wait_stable_readings(),
            )
            handle.mem_wait_threshold_kb = int(min_available_kb)
            handle.mem_available_after_wait_kb = last
            handle.mem_wait_timed_out = not reached
            if not reached:
                logger.warning(
                    "memprep: %s on %s never reached %s kB available "
                    "(last reading %s kB after %ss) — start must abort",
                    slug or "runtime", handle.host_key, min_available_kb,
                    last, wait_timeout_seconds,
                )
            await _store_handle(handle)
    except Exception:  # noqa: BLE001 — preparation is best-effort, the start is not
        logger.exception("memprep: preparation failed on %s", handle.host_key)

    return handle


async def finish(
    handle: PrepHandle | None,
    *,
    host: ResolvedHost | None = None,
    success: bool = True,
) -> dict:
    """Undo everything :func:`prepare_host_memory` changed. Never raises.

    Called from a ``finally`` — a start that raised, timed out or returned an
    error must leave the box exactly as it was found. The handle key is dropped
    LAST, so a crash inside this function still leaves the orphan sweep an
    accurate record of what is outstanding.
    """
    if handle is None:
        return {"restored": False, "dropper_removed": False}

    target = host or handle.as_host()
    result = {"restored": False, "dropper_removed": False, "success": success}

    try:
        if handle.dropper_started:
            result["dropper_removed"] = await _stop_dropper(target)
        if handle.lowered_to_kb is not None and handle.original_watermark_kb is not None:
            result["restored"] = await _write_watermark_kb(
                target, handle.original_watermark_kb
            )
            if not result["restored"]:
                # Leave the handle in Redis: the orphan sweep is the retry.
                logger.error(
                    "memprep: could NOT restore min_free_kbytes=%s on %s — the "
                    "handle stays for the watcher to repair",
                    handle.original_watermark_kb, handle.host_key,
                )
                return result
        result["mem_free_after_kb"] = await read_mem_free_kb(target)
    except Exception:  # noqa: BLE001
        logger.exception("memprep: cleanup failed on %s", handle.host_key)
        return result

    await _clear_handle(handle.host_key)
    return result


async def prepare_for_runtime(
    runtime: dict, *, host: ResolvedHost | None = None
) -> PrepHandle | None:
    """:func:`prepare_host_memory` for a runtime, or ``None`` if it does not apply.

    The one call site that :func:`applies_to` guards, so the start path stays a
    single ``if handle is not None`` on the way out.
    """
    try:
        if not await applies_to(runtime, host):
            return None
    except Exception:  # noqa: BLE001
        logger.exception("memprep: applicability check failed — skipping prep")
        return None

    slug = runtime.get("slug") or runtime.get("id")
    # PR 10: a runtime with a known footprint configures its own floor;
    # everything else falls back to the conservative default rather than
    # skipping the wait entirely (see DEFAULT_MIN_AVAILABLE_KB).
    min_available_kb = runtime.get("prestart_min_available_kb") or DEFAULT_MIN_AVAILABLE_KB
    handle = await prepare_host_memory(
        host,
        watermark_kb=runtime.get("prestart_watermark_kb"),
        slug=str(slug) if slug else None,
        min_available_kb=min_available_kb,
        wait_timeout_seconds=_wait_timeout_seconds(),
    )
    await _emit(
        "runtime.memory_prep_started",
        f"{slug}: Box-Speicher vorbereitet — Page-Cache geleert"
        + (
            f", Watermark {handle.original_watermark_kb} → {handle.lowered_to_kb} kB"
            if handle.lowered_to_kb is not None
            else ", Watermark unverändert"
        ),
        severity="info",
        detail={
            "slug": slug,
            "host": handle.host_key,
            "mem_free_before_kb": handle.mem_free_before_kb,
            "watermark_original_kb": handle.original_watermark_kb,
            "watermark_lowered_to_kb": handle.lowered_to_kb,
            "dropper_started": handle.dropper_started,
            "mem_available_after_wait_kb": handle.mem_available_after_wait_kb,
            "mem_wait_threshold_kb": handle.mem_wait_threshold_kb,
        },
    )
    if handle.mem_wait_timed_out:
        # A separate, unambiguous event: "memory_prep_started" above is
        # accurate (cache and watermark WERE touched) but does not by itself
        # say the start is about to be aborted — this one does.
        await _emit(
            "runtime.memory_prep_timeout",
            f"{slug}: Box-Speicher nach {_wait_timeout_seconds()}s immer noch nicht "
            f"frei ({(handle.mem_available_after_wait_kb or 0) // 1024} MiB verfügbar, "
            f"benötigt {(handle.mem_wait_threshold_kb or 0) // 1024} MiB) — Start wird "
            f"abgebrochen statt blind zu versuchen",
            severity="warning",
            detail={
                "slug": slug,
                "host": handle.host_key,
                "mem_available_after_wait_kb": handle.mem_available_after_wait_kb,
                "mem_wait_threshold_kb": handle.mem_wait_threshold_kb,
            },
        )
    return handle


def _wait_timeout_seconds() -> int:
    """``settings.memory_prep_wait_timeout_seconds`` when set, else the
    module default. A function, not a module-level constant, so tests can
    monkeypatch ``settings`` without reimporting this module."""
    try:
        from app.config import settings

        return int(settings.memory_prep_wait_timeout_seconds)
    except Exception:  # noqa: BLE001 — a bad/missing setting must not block a start
        return DEFAULT_MEM_WAIT_TIMEOUT


def _wait_stable_readings() -> int:
    """``settings.memory_prep_stable_readings`` when set, else the module
    default. Same pattern as :func:`_wait_timeout_seconds` — a function so
    tests can monkeypatch ``settings`` without reimporting this module, and
    a bad/missing value falls back rather than blocking a start."""
    try:
        from app.config import settings

        return int(settings.memory_prep_stable_readings)
    except Exception:  # noqa: BLE001 — a bad/missing setting must not block a start
        return DEFAULT_STABLE_READINGS


async def finish_for_runtime(
    handle: PrepHandle | None, *, host: ResolvedHost | None = None, success: bool
) -> None:
    """:func:`finish` plus the closing event. Safe to call with ``None``."""
    if handle is None:
        return
    result = await finish(handle, host=host, success=success)
    await _emit(
        "runtime.memory_prep_finished",
        f"{handle.slug or handle.host_key}: Box-Speicher zurückgesetzt "
        f"(Watermark {'wiederhergestellt' if result.get('restored') else 'unverändert'}, "
        f"Dropper {'entfernt' if result.get('dropper_removed') else 'nicht aktiv'})",
        severity="info",
        detail={
            "slug": handle.slug,
            "host": handle.host_key,
            "start_ok": success,
            "mem_free_before_kb": handle.mem_free_before_kb,
            "mem_free_after_kb": result.get("mem_free_after_kb"),
            "watermark_restored_kb": (
                handle.original_watermark_kb if result.get("restored") else None
            ),
            **result,
        },
    )


async def load_handle(key: str) -> PrepHandle | None:
    """The outstanding prep for a host key, or ``None``."""
    try:
        redis = await get_redis()
        raw = await redis.get(RedisKeys.host_mem_prep(key))
    except Exception as exc:  # noqa: BLE001
        logger.debug("memprep: handle read failed for %s: %s", key, exc)
        return None
    return PrepHandle.from_json(raw) if raw else None


async def finish_for_host(host: ResolvedHost | None, *, success: bool) -> bool:
    """End whatever prep is outstanding for *host*. Returns True if one was.

    This is the call the runtime watcher makes when a probe finally sees the
    engine serving: that moment — not the return of ``docker compose up`` — is
    when the load window closed and the box may have its watermark and its page
    cache back. Idempotent: no handle means nothing to do.
    """
    key = host_key(host)
    handle = await load_handle(key)
    if handle is None:
        return False
    await finish_for_runtime(handle, host=host, success=success)
    return True


# ── Orphan repair ────────────────────────────────────────────────────────────


async def recover_orphaned_preps(redis=None) -> list[str]:
    """Restore hosts whose prep never finished. Returns the repaired host keys.

    The failure this exists for: the backend dies (deploy, OOM, crash) between
    prepare and finish. Nobody is left holding the handle, the box keeps a 2 GiB
    watermark and a container spinning ``sync`` every second — and the next
    person to look has no idea what the original value was. The handle in Redis
    is that knowledge, so the sweep is simply "act on it".

    30 minutes is deliberately longer than the worst observed cold start
    (10–15 min): a prep that is still legitimately in flight must never be
    undone underneath a loading engine.
    """
    try:
        redis = redis or await get_redis()
        keys = [k async for k in redis.scan_iter(match="mc:host-memprep:*")]
    except Exception as exc:  # noqa: BLE001 — a watcher add-on may not break the tick
        logger.debug("memprep: orphan scan unavailable: %s", exc)
        return []

    repaired: list[str] = []
    cutoff = datetime.now(timezone.utc) - ORPHAN_MAX_AGE
    for key in keys:
        raw = await redis.get(key)
        if not raw:
            continue
        handle = PrepHandle.from_json(raw)
        if handle is None:
            # Unparseable = nothing actionable in it; dropping it is the only
            # way it stops being scanned every tick.
            await redis.delete(key)
            continue
        if not _older_than(handle.started_at, cutoff):
            continue
        if not handle.changed_anything:
            await redis.delete(key)
            continue
        logger.warning(
            "memprep: orphaned preparation on %s (started %s) — restoring",
            handle.host_key, handle.started_at,
        )
        result = await finish(handle, success=False)
        if result.get("restored") or handle.lowered_to_kb is None:
            repaired.append(handle.host_key)
            await _emit(
                "runtime.memory_prep_recovered",
                f"{handle.host_key}: verwaiste Speicher-Vorbereitung aufgeräumt "
                f"(Watermark zurück auf {handle.original_watermark_kb} kB, "
                f"Cache-Dropper entfernt)",
                severity="warning",
                detail={
                    "host": handle.host_key,
                    "slug": handle.slug,
                    "started_at": handle.started_at,
                    "watermark_restored_kb": handle.original_watermark_kb,
                },
            )
    return repaired


def _older_than(started_at: str, cutoff: datetime) -> bool:
    """A handle with an unparseable timestamp counts as old.

    The alternative — skipping it — means a malformed timestamp pins a lowered
    watermark on the box forever. Restoring one prep too early is recoverable;
    never restoring is not.
    """
    if not started_at:
        return True
    try:
        parsed = datetime.fromisoformat(started_at)
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed < cutoff


# ── Redis + events ───────────────────────────────────────────────────────────


async def _store_handle(handle: PrepHandle) -> None:
    try:
        redis = await get_redis()
        # No TTL: expiry would silently discard the only record of the original
        # watermark. The orphan sweep is what ends a handle's life.
        await redis.set(RedisKeys.host_mem_prep(handle.host_key), handle.to_json())
    except Exception as exc:  # noqa: BLE001
        logger.warning("memprep: handle could not be persisted (%s)", exc)


async def _clear_handle(key: str) -> None:
    try:
        redis = await get_redis()
        await redis.delete(RedisKeys.host_mem_prep(key))
    except Exception as exc:  # noqa: BLE001
        logger.debug("memprep: handle delete failed: %s", exc)


async def _emit(event: str, message: str, *, severity: str, detail: dict) -> None:
    """Activity event, best-effort — a failing feed never blocks a start."""
    try:
        from app.services.activity import emit_event
        from app.services.runtime_model_resolver import session_scope

        async with session_scope() as session:
            await emit_event(session, event, message, severity=severity, detail=detail)
    except Exception as exc:  # noqa: BLE001
        logger.debug("memprep: event %s not emitted: %s", event, exc)
