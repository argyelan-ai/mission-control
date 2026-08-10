"""Runtime container ownership proof (Task #22).

Live incident (08./09.08.2026): a qwen sparkrun wrapper kept running unseen
while DeepSeek started — ``ensure_exclusive_host`` reported "box was already
free", a false all-clear from a matcher that never saw the container. That
half of the bug is a *discovery* gap and is closed in ``runtime_manager``
(the compose-project-label sweep). This module closes the other half: once a
container IS found, MC has never verified it is the container MC itself
started before stopping it. A container hand-recreated by an operator under
the same name or the same ``mc.runtime.slug`` label looks identical to ours.

Modelled on Local Studio's ownership pattern (see
``research/mc-local-first/EVAL-LOCAL-STUDIO.md`` §9.1,
``controller/src/modules/compute/launchers/docker.ts``): "ownership is a
label pair written at launch time... a container someone recreated by hand
under the same name is never signalled." Their rule, taken literally: never
stop what we cannot prove is ours.

How it works
------------
Every container MC creates for a slug is stamped with two labels:
``mc.runtime.slug=<slug>`` (existing, used for discovery) and
``mc.runtime.nonce=<uuid4>`` (new). The nonce MC expects for that slug is
recorded here, in Redis, at the moment the launch command is built — not at
every ``docker start`` of an already-existing container, only at the moment a
*new* container gets created (a fresh ``docker run`` / ``docker compose up``).
Before stopping a container that carries MC's slug label, the caller
compares its actual nonce label (read via ``docker inspect``) against the
stored expectation. A mismatch — or a missing nonce where one is expected —
means the label lied about ownership, so the caller must not stop it and
should raise the discrepancy instead.

Containers with no ``mc.runtime.slug`` label at all (the ``sparkrun_*_solo``
name sweep, the ``vllm_node`` manual-start name, and now the compose-project
sweep) are a different case: MC never claimed ownership of them via label in
the first place, so there is no nonce to check. Eviction still stops those —
that sweep exists specifically to catch CLI- or externally-started models
MC never labelled (the original P0 fix), and nonce-gating would silently
reopen that exact bug. Nonce verification only ever *restricts* stops that
would otherwise happen because a label said "this is mine".

No TTL on the stored nonce: it must outlive the whole runtime lifetime
(potentially days), not a short switch window. Redis down degrades safely —
``get_nonce`` returns ``None`` like "never verified", and callers treat that
as "cannot prove ownership" (the safe direction: skip + warn, not stop).
"""

from __future__ import annotations

import logging
import re
import uuid
from shlex import quote as shlex_quote

from app.redis_client import RedisKeys, get_redis

logger = logging.getLogger("mc.runtime_ownership")

# The label every MC-created container carries alongside mc.runtime.slug.
NONCE_LABEL = "mc.runtime.nonce"


def new_nonce() -> str:
    """A fresh, unguessable ownership token for a container MC is about to
    create. uuid4 — no structure to infer, nothing an operator would type by
    hand while recreating a container manually."""
    return uuid.uuid4().hex


async def set_nonce(slug: str | None, nonce: str) -> None:
    """Record the nonce MC expects for ``slug`` going forward. Best-effort —
    Redis being down must not block a launch; it only means later stop calls
    can't prove ownership and fall back to the safe (skip + warn) path."""
    if not slug:
        return
    try:
        redis = await get_redis()
        await redis.set(RedisKeys.runtime_nonce(slug), nonce)
    except Exception as exc:  # noqa: BLE001
        logger.debug("set_nonce(%s) failed: %s", slug, exc)


async def get_nonce(slug: str | None) -> str | None:
    """The nonce MC expects for ``slug``, or ``None`` when unknown (never
    set, or Redis unreachable) — both read as "cannot prove ownership"."""
    if not slug:
        return None
    try:
        redis = await get_redis()
        raw = await redis.get(RedisKeys.runtime_nonce(slug))
    except Exception as exc:  # noqa: BLE001
        logger.debug("get_nonce(%s) failed: %s", slug, exc)
        return None
    if raw is None:
        return None
    return raw.decode() if isinstance(raw, bytes) else raw


async def clear_nonce(slug: str | None) -> None:
    """Drop the stored nonce, e.g. when a runtime is deleted. Best-effort."""
    if not slug:
        return
    try:
        redis = await get_redis()
        await redis.delete(RedisKeys.runtime_nonce(slug))
    except Exception as exc:  # noqa: BLE001
        logger.debug("clear_nonce(%s) failed: %s", slug, exc)


def label_flag(slug: str, nonce: str) -> str:
    """The ``--label`` fragment a launch command appends to stamp ``nonce``
    onto the container it creates, alongside the existing slug label."""
    return f"--label {NONCE_LABEL}={shlex_quote(nonce)}"


async def inspect_labels(
    container_ids: list[str], *, host, ssh_run,
) -> dict[str, dict[str, str]]:
    """``{container_id: {"slug": ..., "nonce": ...}}`` for every id in
    ``container_ids``, one ``docker inspect`` round trip for all of them.

    ``ssh_run`` is injected (``runtime_manager._ssh_run``) rather than
    imported at module level — this module has no SSH dependency of its own
    and stays trivially unit-testable without mocking a transport.

    Empty labels (container carries neither the slug nor the nonce label)
    come back as empty strings, matched to how ``docker inspect --format``
    renders a missing label — never absent from the dict, so callers don't
    need a second existence check.
    """
    ids = [c for c in container_ids if c and c.strip()]
    if not ids:
        return {}
    # One inspect call for every id; --format renders one line per id in the
    # same order docker was given them, tab-separated for a plain split.
    fmt = (
        "{{.Id}}\t"
        f"{{{{index .Config.Labels \"mc.runtime.slug\"}}}}\t"
        f"{{{{index .Config.Labels \"{NONCE_LABEL}\"}}}}"
    )
    quoted_ids = " ".join(shlex_quote(c) for c in ids)
    cmd = f"docker inspect --format {shlex_quote(fmt)} {quoted_ids} 2>/dev/null"
    try:
        out, _, ec = await ssh_run(cmd, host=host, timeout=20)
    except Exception as exc:  # noqa: BLE001
        logger.warning("inspect_labels: docker inspect raised: %s", exc)
        return {}
    if ec != 0:
        logger.warning("inspect_labels: docker inspect exited %s", ec)
    result: dict[str, dict[str, str]] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if not parts or not parts[0].strip():
            continue
        cid = parts[0].strip()
        slug = parts[1].strip() if len(parts) > 1 else ""
        nonce = parts[2].strip() if len(parts) > 2 else ""
        result[cid] = {"slug": slug, "nonce": nonce}
    return result


async def partition_by_ownership(
    container_ids: list[str], *, host, ssh_run,
) -> tuple[list[str], list[dict]]:
    """Split discovered containers into ``(safe_to_stop, blocked)``.

    A container is blocked — kept out of ``safe_to_stop`` — only when it
    claims MC ownership via ``mc.runtime.slug`` AND MC's stored nonce for
    that slug does not match (or MC never recorded one). Unlabelled
    containers (no ``mc.runtime.slug`` at all — the CLI/externally-started
    case the sweep exists to catch) are always safe to stop; there is no
    ownership claim to disprove.

    Each ``blocked`` entry: ``{"container_id", "slug", "reason"}``.

    A slug with NO recorded expectation (``get_nonce`` returns ``None`` —
    never set, e.g. this feature shipped after the container's last launch,
    or a runtime whose currently-running container predates it) is treated
    as safe, not blocked: there is no expectation to verify the label
    against, so the label alone is trusted exactly like before this fix.
    Without this, every runtime running at deploy time would become
    un-evictable until its next recipe switch — the opposite of what this
    exists to prevent. Verification only ever restricts stops once MC has
    actually recorded what it expects.
    """
    labels = await inspect_labels(container_ids, host=host, ssh_run=ssh_run)
    safe: list[str] = []
    blocked: list[dict] = []
    for cid in container_ids:
        info = labels.get(cid, {"slug": "", "nonce": ""})
        slug = info.get("slug") or ""
        if not slug:
            # No ownership claim on this container — the sweep's whole job is
            # to catch exactly this case (CLI/manual/unlabelled containers).
            safe.append(cid)
            continue
        expected = await get_nonce(slug)
        if expected is None:
            safe.append(cid)
            continue
        actual = info.get("nonce") or ""
        if actual == expected:
            safe.append(cid)
        else:
            reason = (
                "Container trägt keinen Nonce-Label"
                if not actual
                else "Nonce stimmt nicht überein"
            )
            blocked.append({"container_id": cid, "slug": slug, "reason": reason})
    return safe, blocked


# Matches the compose-project label docker compose stamps on every container
# it creates, regardless of whether the service set container_name. Scoped
# to project names that look like ours or like a sparkrun-managed stack —
# see runtime_manager.py module docstring for why the exact sparkrun project
# name can't be derived from the registry, and docs/ARCHITECTURE.md for the
# live-incident writeup.
COMPOSE_PROJECT_NAME_PATTERN = re.compile(r"(^mc[-_])|sparkrun|solo", re.IGNORECASE)
