"""Runtime membership — the one writer of ``runtime_hosts`` (ADR-096 §1–§2).

Which boxes belong to a runtime is stored in exactly one place: the
``runtime_hosts`` table (one row per member: head = rank 0, workers = rank
1..n-1). Before this module there were two truths (``topology.worker_host_id``
and the table) and only the recipe start wrote the table — every other path
(``POST/PATCH /runtimes/db``, the already-running branch of the recipe start,
imports) left it empty, so a duo looked like a solo and its worker box looked
free.

Rules this module owns:

* **One writer.** :func:`set_members` and :func:`clear_members` are the only
  backend code that inserts or deletes ``RuntimeHost`` rows. An architecture
  test (``tests/test_runtime_membership.py``) rejects any other writer.
* **Structure only.** ``set_members`` checks ranks, roles, host existence,
  ``enabled``, ``power_managed`` and the node count. It does **not** check
  occupancy and **not** SSH reachability: switching between two duos on the
  same boxes is the normal case — the start evicts the old one
  (``recipe_switcher.start_recipe_on_host``), it must not be refused here.
* **A running runtime keeps its boxes.** If the runtime's engine runs and the
  requested member set differs from the stored one → 409. Filling an empty
  set (repairing a legacy row from known data) is allowed.
* **Solo runtimes have zero rows** and are never touched: no probe, no write.
* **``topology.worker_host_id`` is no longer written.** Readers use
  :func:`recorded_worker_id` / :func:`members_of`; API responses derive the
  old key from rank 1 (:func:`with_derived_worker`) so old clients keep
  working.

:func:`membership_audit` is the healer: with ``repair=True`` (backend start,
before the runtime watcher starts) it turns a legacy
``topology.worker_host_id`` without rows into rows and reports each repair as
an activity event; with ``repair=False`` (each watcher cycle) it only reports.
It never guesses a box.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.host import Host
from app.models.local_recipe import LocalRecipe
from app.models.runtime import Runtime
from app.models.runtime_host import RuntimeHost

logger = logging.getLogger("mc.runtime_membership")

ROLE_HEAD = "head"
ROLE_WORKER = "worker"

#: Legacy key in ``runtimes.topology``. Read once by the healer and accepted
#: as request input (translated); never written by MC any more.
LEGACY_WORKER_KEY = "worker_host_id"


@dataclass(frozen=True)
class MemberSpec:
    """One requested member: which box, which role, which rank."""

    host_id: uuid.UUID
    role: str
    rank: int


class MembershipError(Exception):
    """A membership write that is refused — HTTP status plus one sentence."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class AuditFinding:
    """One runtime whose members do not match its topology (ADR-096 I1–I4)."""

    runtime_id: uuid.UUID
    slug: str
    code: str
    sentence: str
    repaired: bool = False


# ── Small helpers ────────────────────────────────────────────────────────────


def as_uuid(value: Any) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def topology_nodes(topology: Any) -> int:
    """``topology.nodes`` leniently: NULL/broken = 1 (solo), as everywhere else."""
    try:
        nodes = int((topology or {}).get("nodes", 1)) if isinstance(topology, dict) else 1
    except (TypeError, ValueError):
        return 1
    return nodes if nodes >= 1 else 1


def duo(head_id: uuid.UUID, worker_id: uuid.UUID) -> list[MemberSpec]:
    """The two-box member set: head rank 0, worker rank 1."""
    return [
        MemberSpec(head_id, ROLE_HEAD, 0),
        MemberSpec(worker_id, ROLE_WORKER, 1),
    ]


def without_legacy_worker(topology: Any) -> dict | None:
    """A copy of ``topology`` without ``worker_host_id`` (the old second truth)."""
    if not isinstance(topology, dict):
        return topology
    return {k: v for k, v in topology.items() if k != LEGACY_WORKER_KEY}


def with_derived_worker(topology: Any, worker_id: uuid.UUID | None) -> Any:
    """``topology`` for an API response: ``worker_host_id`` derived from rank 1.

    Old readers (frontend, scripts) keep seeing the key; the stored value is
    never the source. Solo or no recorded worker → the key is dropped, so a
    stale stored value can never reappear as if it were true.
    """
    if not isinstance(topology, dict):
        return topology
    out = without_legacy_worker(topology) or {}
    if worker_id is not None:
        out[LEGACY_WORKER_KEY] = str(worker_id)
    return out


# ── Reading ──────────────────────────────────────────────────────────────────


async def members_of(session: AsyncSession, runtime_id: uuid.UUID) -> list[RuntimeHost]:
    """Member rows of one runtime, head (rank 0) first."""
    return list(
        (
            await session.exec(
                select(RuntimeHost)
                .where(RuntimeHost.runtime_id == runtime_id)
                .order_by(RuntimeHost.node_rank)  # type: ignore[arg-type]
            )
        ).all()
    )


def worker_of(rows: Iterable[RuntimeHost]) -> uuid.UUID | None:
    """The rank-1 worker box among member rows, if any."""
    for row in rows:
        if row.role == ROLE_WORKER and row.node_rank == 1:
            return row.host_id
    for row in rows:
        if row.role == ROLE_WORKER:
            return row.host_id
    return None


async def recorded_worker_id(session: AsyncSession, runtime_id: uuid.UUID) -> uuid.UUID | None:
    """The worker box ``runtime_hosts`` holds for this runtime, if any."""
    return worker_of(await members_of(session, runtime_id))


async def workers_by_runtime(
    session: AsyncSession, runtime_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    """``{runtime_id: worker_host_id}`` for many runtimes in one query."""
    ids = list(runtime_ids)
    if not ids:
        return {}
    rows = (
        await session.exec(
            select(RuntimeHost)
            .where(RuntimeHost.runtime_id.in_(ids))  # type: ignore[attr-defined]
            .order_by(RuntimeHost.node_rank)  # type: ignore[arg-type]
        )
    ).all()
    grouped: dict[uuid.UUID, list[RuntimeHost]] = {}
    for row in rows:
        grouped.setdefault(row.runtime_id, []).append(row)
    out: dict[uuid.UUID, uuid.UUID] = {}
    for rid, members in grouped.items():
        worker = worker_of(members)
        if worker is not None:
            out[rid] = worker
    return out


def _as_set(members: Iterable[Any]) -> set[tuple[uuid.UUID, str, int]]:
    out = set()
    for m in members:
        if isinstance(m, MemberSpec):
            out.add((m.host_id, m.role, m.rank))
        else:
            out.add((m.host_id, m.role, m.node_rank))
    return out


# ── Structure check (no occupancy, no reachability) ──────────────────────────


async def validate_members(
    session: AsyncSession,
    runtime: Runtime,
    members: list[MemberSpec],
    *,
    recipe: LocalRecipe | None = None,
) -> dict[uuid.UUID, Host]:
    """Raise :class:`MembershipError` (422) when the set is not a valid shape.

    Returns the member hosts by id. Deliberately NOT checked here: whether a
    box is busy (the start evicts — ADR-096 §1.2, review blocker B1) and
    whether a box answers on SSH (a sleeping box must not make *recording* a
    runtime impossible; the start checks that).
    """
    n = len(members)
    if n < 2:
        raise MembershipError(
            422,
            "a runtime on one box has no member rows — set topology.nodes to 1 instead",
        )
    ranks = sorted(m.rank for m in members)
    if ranks != list(range(n)):
        raise MembershipError(
            422, f"member ranks must be 0..{n - 1} without gaps or repeats, got {ranks}"
        )
    by_rank = {m.rank: m for m in members}
    head = by_rank[0]
    if runtime.host_id is None or head.host_id != runtime.host_id:
        raise MembershipError(
            422, "rank 0 must be the runtime's own host_id (the head box)"
        )
    if head.role != ROLE_HEAD or any(m.role != ROLE_WORKER for m in members if m.rank != 0):
        raise MembershipError(422, "rank 0 has role 'head', every other rank role 'worker'")
    host_ids = [m.host_id for m in members]
    if len(set(host_ids)) != n:
        raise MembershipError(422, "every member must be a different box")
    nodes = topology_nodes(runtime.topology)
    if nodes != n:
        raise MembershipError(
            422, f"topology.nodes is {nodes} but {n} member boxes were given"
        )
    if recipe is not None:
        recipe_n = topology_nodes(recipe.topology)
        if recipe_n != n:
            raise MembershipError(
                422,
                f"recipe '{recipe.slug}' needs {recipe_n} box(es), not {n}",
            )
    hosts: dict[uuid.UUID, Host] = {}
    for hid in host_ids:
        host = await session.get(Host, hid)
        if host is None:
            raise MembershipError(
                422, f"member box {hid} is not a registered host (GET /api/v1/hosts)"
            )
        if not host.enabled:
            raise MembershipError(
                422, f"box '{host.slug}' is switched off in MC and cannot be a member"
            )
        if host.power_managed:
            raise MembershipError(
                422,
                f"box '{host.slug}' sleeps on demand; multi-box runtimes cannot use it yet",
            )
        hosts[hid] = host
    return hosts


# ── Running check ────────────────────────────────────────────────────────────


async def _is_running(session: AsyncSession, runtime: Runtime) -> bool:
    """Does this runtime's engine answer right now? Same probe as the switcher.

    Only called when a write would CHANGE an existing member set, so solo
    runtimes and unchanged duos never trigger a probe.
    """
    from app.services import recipe_switcher
    from app.services.host_resolver import resolved_host_from_row

    head = await session.get(Host, runtime.host_id) if runtime.host_id else None
    resolved = resolved_host_from_row(head) if head is not None else None
    try:
        return bool(await recipe_switcher.probe_running(runtime, host=resolved))
    except Exception:  # noqa: BLE001 — no evidence = not running (same rule as the probe)
        logger.debug("membership: probe failed for %s", runtime.slug, exc_info=True)
        return False


async def _refuse_if_running(
    session: AsyncSession, runtime: Runtime, stored: list[RuntimeHost], running: bool | None
) -> None:
    if running is None:
        running = await _is_running(session, runtime)
    if not running:
        return
    slugs = []
    for row in stored:
        host = await session.get(Host, row.host_id)
        slugs.append(host.slug if host is not None else str(row.host_id))
    raise MembershipError(
        409,
        f"'{runtime.slug}' runs on {', '.join(slugs)} — stop it before changing its boxes",
    )


# ── Writing (the only place) ─────────────────────────────────────────────────


async def _delete_rows(session: AsyncSession, rows: list[RuntimeHost]) -> None:
    for row in rows:
        await session.delete(row)
    await session.flush()


async def _insert_rows(
    session: AsyncSession, runtime_id: uuid.UUID, members: list[MemberSpec]
) -> None:
    for m in sorted(members, key=lambda spec: spec.rank):
        session.add(
            RuntimeHost(runtime_id=runtime_id, host_id=m.host_id, role=m.role, node_rank=m.rank)
        )


async def set_members(
    session: AsyncSession,
    runtime: Runtime,
    members: list[MemberSpec],
    *,
    recipe: LocalRecipe | None = None,
    running: bool | None = None,
    commit: bool = True,
) -> bool:
    """Record ``members`` for ``runtime``. Returns True when rows changed.

    ``running``: the caller's knowledge whether the runtime's engine runs.
    ``None`` = probe it, but only if the stored set is non-empty AND differs.
    The recipe start passes ``False`` (it checked ``already_running`` right
    before); the CRUD endpoints leave it ``None``.

    Delete-then-insert in the caller's transaction (``commit=False``) or its
    own — an upsert would collide with the unique rule (runtime_id, node_rank)
    when the worker box changes.
    """
    await validate_members(session, runtime, members, recipe=recipe)
    stored = await members_of(session, runtime.id)
    if _as_set(stored) == _as_set(members):
        return False
    if stored:
        await _refuse_if_running(session, runtime, stored, running)
    await _delete_rows(session, stored)
    await _insert_rows(session, runtime.id, members)
    if commit:
        await session.commit()
    else:
        await session.flush()
    return True


async def clear_members(
    session: AsyncSession,
    runtime: Runtime,
    *,
    running: bool | None = None,
    commit: bool = True,
) -> bool:
    """Demote to solo: drop every member row. Returns True when rows changed.

    A solo runtime (zero rows) is a no-op without a probe. A running duo is
    refused (409): its worker would look free while the engine still uses it.
    """
    stored = await members_of(session, runtime.id)
    if not stored:
        return False
    await _refuse_if_running(session, runtime, stored, running)
    await _delete_rows(session, stored)
    if commit:
        await session.commit()
    return True


# ── Healer / audit (ADR-096 §2) ──────────────────────────────────────────────


async def _recipe_for(session: AsyncSession, runtime: Runtime) -> LocalRecipe | None:
    topology = runtime.topology if isinstance(runtime.topology, dict) else {}
    slug = topology.get("recipe_slug")
    if not slug:
        return None
    return (
        await session.exec(select(LocalRecipe).where(LocalRecipe.slug == slug))
    ).first()


async def _check_one(
    session: AsyncSession, runtime: Runtime, *, repair: bool
) -> AuditFinding | None:
    topology = runtime.topology if isinstance(runtime.topology, dict) else {}
    nodes = topology_nodes(topology)
    stored = await members_of(session, runtime.id)

    def finding(code: str, sentence: str, repaired: bool = False) -> AuditFinding:
        return AuditFinding(runtime.id, runtime.slug, code, sentence, repaired)

    if nodes <= 1:
        if stored:
            return finding(
                "rows_on_solo",
                f"'{runtime.slug}' is recorded as one box but has {len(stored)} member "
                "row(s) — set topology.nodes or stop and re-save it",
            )
        return None

    recipe = await _recipe_for(session, runtime)
    if not stored:
        legacy = as_uuid(topology.get(LEGACY_WORKER_KEY))
        if legacy is None or runtime.host_id is None:
            return finding(
                "members_incomplete",
                f"'{runtime.slug}' needs {nodes} boxes but no worker box is known — "
                "pick a worker (PATCH topology.worker_host_id)",
            )
        if not repair:
            return finding(
                "members_incomplete",
                f"'{runtime.slug}' has its worker only in the old topology field — "
                "it is recorded at the next backend start",
            )
        try:
            await set_members(
                session, runtime, duo(runtime.host_id, legacy), recipe=recipe, commit=True
            )
        except MembershipError as exc:
            await session.rollback()
            return finding("members_invalid", f"'{runtime.slug}': {exc.detail}")
        return finding("members_repaired", f"'{runtime.slug}': worker box recorded", True)

    try:
        await validate_members(
            session,
            runtime,
            [MemberSpec(r.host_id, r.role, r.node_rank) for r in stored],
            recipe=recipe,
        )
    except MembershipError as exc:
        return finding("members_invalid", f"'{runtime.slug}': {exc.detail}")
    return None


async def membership_audit(session: AsyncSession, *, repair: bool) -> list[AuditFinding]:
    """Check I1–I4 for every runtime; with ``repair`` fill legacy rows.

    Slot runtimes (ADR-078) are excluded: they never span boxes. Each runtime
    is checked on its own; one broken row never stops the others.
    """
    runtimes = (await session.exec(select(Runtime))).all()
    findings: list[AuditFinding] = []
    for runtime in runtimes:
        if runtime.is_slot:
            continue
        try:
            result = await _check_one(session, runtime, repair=repair)
        except Exception:  # noqa: BLE001 — one runtime must not stop the audit
            logger.exception("membership audit failed for %s", runtime.slug)
            try:
                await session.rollback()
            except Exception:  # noqa: BLE001
                logger.exception("rollback after membership audit error failed")
            continue
        if result is not None:
            findings.append(result)
    if repair:
        await _report_repairs(session, [f for f in findings if f.repaired])
    return findings


async def _report_repairs(session: AsyncSession, repaired: list[AuditFinding]) -> None:
    """One activity event per repaired runtime — what changed, and what that
    means for eviction (the worker box now counts as used)."""
    from app.services.activity import emit_event

    for item in repaired:
        runtime = await session.get(Runtime, item.runtime_id)
        if runtime is None:
            continue
        worker_id = await recorded_worker_id(session, runtime.id)
        worker = await session.get(Host, worker_id) if worker_id else None
        worker_slug = worker.slug if worker is not None else str(worker_id)
        name = runtime.display_name or runtime.slug
        await emit_event(
            session,
            "runtime.members_repaired",
            f"{runtime.slug}: worker box {worker_slug} recorded as member — "
            f"{worker_slug} now counts as used by {name}; starting another "
            f"model there will stop it",
            severity="info",
            detail={
                "runtime_id": str(runtime.id),
                "slug": runtime.slug,
                "worker_host_id": str(worker_id) if worker_id else None,
                "worker_slug": worker_slug,
            },
        )
        logger.warning("membership: %s — worker %s recorded at startup", runtime.slug, worker_slug)


async def startup_audit() -> list[AuditFinding]:
    """Backend start: repair + report, in its own session. Never raises."""
    from app.services.runtime_model_resolver import session_scope

    try:
        async with session_scope() as session:
            findings = await membership_audit(session, repair=True)
    except Exception:  # noqa: BLE001 — a broken audit must never stop the backend
        logger.exception("membership audit at startup failed")
        return []
    for item in findings:
        if not item.repaired:
            logger.warning("membership: %s (%s)", item.sentence, item.code)
    return findings
