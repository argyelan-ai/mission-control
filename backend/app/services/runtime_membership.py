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


def member_capable(host: Host) -> bool:
    """May this box be a member of a multi-box runtime at all?

    The one rule for ``validate_members`` AND for the recipe switcher's worker
    candidates: a box switched off in MC or one that sleeps on demand
    (``power_managed``) never is. Offering such a box in the start dialog and
    refusing it only after the instance row was written left a half duo
    behind (critic 19.10.2026).
    """
    return bool(host.enabled) and not host.power_managed


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
            "Eine Runtime auf einer Box hat keine Mitglieder-Zeilen — "
            "topology.nodes auf 1 setzen",
        )
    ranks = sorted(m.rank for m in members)
    if ranks != list(range(n)):
        raise MembershipError(
            422, f"Mitglieder-Ränge müssen 0..{n - 1} ohne Lücke und ohne Doppel sein, nicht {ranks}"
        )
    by_rank = {m.rank: m for m in members}
    head = by_rank[0]
    if runtime.host_id is None or head.host_id != runtime.host_id:
        raise MembershipError(
            422, "Rang 0 muss die eigene Box der Runtime sein (host_id, der Head)"
        )
    if head.role != ROLE_HEAD or any(m.role != ROLE_WORKER for m in members if m.rank != 0):
        raise MembershipError(422, "Rang 0 hat die Rolle 'head', jeder andere Rang 'worker'")
    host_ids = [m.host_id for m in members]
    if len(set(host_ids)) != n:
        raise MembershipError(422, "Jedes Mitglied muss eine andere Box sein")
    nodes = topology_nodes(runtime.topology)
    if nodes != n:
        raise MembershipError(
            422, f"topology.nodes ist {nodes}, aber es wurden {n} Boxen angegeben"
        )
    if recipe is not None:
        recipe_n = topology_nodes(recipe.topology)
        if recipe_n != n:
            raise MembershipError(
                422,
                f"Rezept '{recipe.slug}' braucht {recipe_n} Box(en), nicht {n}",
            )
    hosts: dict[uuid.UUID, Host] = {}
    for hid in host_ids:
        host = await session.get(Host, hid)
        if host is None:
            raise MembershipError(
                422, f"Box {hid} ist in MC nicht eingetragen (GET /api/v1/hosts)"
            )
        if not host.enabled:
            raise MembershipError(
                422, f"Box '{host.slug}' ist in MC ausgeschaltet und kann kein Mitglied sein"
            )
        if not member_capable(host):
            raise MembershipError(
                422,
                f"Box '{host.slug}' schläft bei Bedarf — Mehrbox-Runtimes können sie "
                "noch nicht nutzen",
            )
        hosts[hid] = host
    return hosts


# ── Running check ────────────────────────────────────────────────────────────


async def is_running(session: AsyncSession, runtime: Runtime) -> bool:
    """Does this runtime's engine answer right now? Same probe as the switcher.

    Asks the runtime's CURRENT head (``runtime.host_id`` as loaded). A caller
    that is about to change the row (PATCH /runtimes/db) asks this BEFORE the
    change and with no transaction open, and passes the answer as
    ``running=`` — asked afterwards, the probe would go to the new head.
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
        running = await is_running(session, runtime)
    if not running:
        return
    slugs = []
    for row in stored:
        host = await session.get(Host, row.host_id)
        slugs.append(host.slug if host is not None else str(row.host_id))
    raise MembershipError(
        409,
        f"'{runtime.slug}' läuft auf {', '.join(slugs)} — erst stoppen, dann die Boxen ändern",
    )


# ── Writing (the only place) ─────────────────────────────────────────────────


async def _delete_rows(session: AsyncSession, rows: list[RuntimeHost]) -> None:
    for row in rows:
        await session.delete(row)
    await session.flush()


async def lock_runtime(session: AsyncSession, runtime_id: uuid.UUID) -> None:
    """Row lock on the runtime (``SELECT … FOR UPDATE``, ADR-096 §1.1).

    Every membership writer takes it BEFORE it reads the stored set, so two
    writers of one runtime — the API and the worker are separate processes —
    run one after the other instead of interleaving their delete-then-insert.
    Held until the caller's transaction ends. Lock only: the runtime object in
    the session is not reloaded, so a caller's pending change (a PATCH that
    moves ``host_id``) is kept. SQLite has no row locks; the clause is dropped
    there.
    """
    await session.exec(select(Runtime.id).where(Runtime.id == runtime_id).with_for_update())


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
    when the worker box changes. The stored set is read only after
    :func:`lock_runtime`, so a concurrent writer's rows are seen and replaced,
    not collided with.
    """
    await validate_members(session, runtime, members, recipe=recipe)
    await lock_runtime(session, runtime.id)
    stored = await members_of(session, runtime.id)
    if _as_set(stored) == _as_set(members):
        if commit:
            await session.commit()  # end the transaction: never sit on the lock
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
    await lock_runtime(session, runtime.id)
    stored = await members_of(session, runtime.id)
    if not stored:
        if commit:
            await session.commit()  # end the transaction: never sit on the lock
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


async def _is_switching(slug: str) -> bool:
    """Is a start/stop of this runtime in flight (``runtime_grace`` marker)?"""
    from app.services.runtime_grace import get_switching

    return await get_switching(slug) is not None


async def _check_one(
    session: AsyncSession, runtime: Runtime, *, repair: bool, locked: bool = False
) -> AuditFinding | None:
    """Check one runtime; with ``repair`` fill legacy rows.

    The decision to WRITE is never taken on what was read without the lock:
    a repair first takes the row lock (:func:`_repair_under_lock`), reloads
    the runtime and decides again (``locked=True``) — a start that committed
    a new worker in between is seen, not overwritten (review #893).
    """
    topology = runtime.topology if isinstance(runtime.topology, dict) else {}
    nodes = topology_nodes(topology)
    stored = await members_of(session, runtime.id)

    def finding(code: str, sentence: str, repaired: bool = False) -> AuditFinding:
        return AuditFinding(runtime.id, runtime.slug, code, sentence, repaired)

    if nodes <= 1:
        if stored:
            return finding(
                "rows_on_solo",
                f"'{runtime.slug}' ist als Ein-Box-Runtime eingetragen, hat aber "
                f"{len(stored)} Mitglieder-Zeile(n) — topology.nodes setzen oder stoppen "
                "und neu speichern",
            )
        return None

    recipe = await _recipe_for(session, runtime)
    if not stored:
        legacy = as_uuid(topology.get(LEGACY_WORKER_KEY))
        if legacy is None or runtime.host_id is None:
            return finding(
                "members_incomplete",
                f"'{runtime.slug}' braucht {nodes} Boxen, aber keine Worker-Box ist "
                "eingetragen — Worker wählen (Start-Dialog oder PATCH "
                "topology.worker_host_id); der Wächter startet sie bis dahin nicht",
            )
        if not repair:
            return finding(
                "members_incomplete",
                f"'{runtime.slug}' hat seine Worker-Box nur im alten topology-Feld — "
                "sie wird beim nächsten Backend-Start eingetragen",
            )
        if not locked:
            return await _repair_under_lock(session, runtime)
        try:
            await set_members(
                session, runtime, duo(runtime.host_id, legacy), recipe=recipe, commit=True
            )
        except MembershipError as exc:
            await session.rollback()
            return finding("members_invalid", f"'{runtime.slug}': {exc.detail}")
        return finding("members_repaired", f"'{runtime.slug}': Worker-Box eingetragen", True)

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


async def _repair_under_lock(session: AsyncSession, runtime: Runtime) -> AuditFinding | None:
    """Take the row lock, reload the runtime, skip it if a switch began, and
    decide again on the fresh row (ADR-096 §2: re-check I1–I4 under the lock).

    The lock is held from the re-read to the write's commit, so no other
    membership writer can slip in between. A path that does not write ends
    the transaction here (rollback) — the healer never sits on the lock.
    """
    # populate_existing: overwrite what the first read put into the identity
    # map with the row as it is now (Postgres READ COMMITTED: each statement
    # sees what was committed before it; the lock waits for open writers).
    fresh = await session.get(
        Runtime, runtime.id, with_for_update=True, populate_existing=True
    )
    try:
        if fresh is None or fresh.is_slot or await _is_switching(fresh.slug):
            return None
        return await _check_one(session, fresh, repair=True, locked=True)
    finally:
        if session.in_transaction():
            await session.rollback()


async def membership_audit(session: AsyncSession, *, repair: bool) -> list[AuditFinding]:
    """Check I1–I4 for every runtime; with ``repair`` fill legacy rows.

    Slot runtimes (ADR-078) are excluded: they never span boxes. Each runtime
    is checked on its own; one broken row never stops the others. Runtimes
    with an active switching marker are skipped; a repair re-checks I1–I4
    under the row lock before it writes (ADR-096 §2).
    """
    # Ids only, then each row fresh: a list of Runtime objects loaded once is
    # a snapshot that ages while the audit works (review #893), and a
    # rollback after one broken runtime would expire every one of them.
    targets = (
        await session.exec(
            select(Runtime.id, Runtime.slug).where(Runtime.is_slot == False)  # noqa: E712
        )
    ).all()
    findings: list[AuditFinding] = []
    for runtime_id, slug in targets:
        try:
            # ADR-096 §2: a runtime that is switching right now is left alone —
            # its start is writing members; reading it mid-way is no finding.
            if await _is_switching(slug):
                continue
            runtime = await session.get(Runtime, runtime_id, populate_existing=True)
            if runtime is None or runtime.is_slot:
                continue
            result = await _check_one(session, runtime, repair=repair)
        except Exception:  # noqa: BLE001 — one runtime must not stop the audit
            logger.exception("membership audit failed for %s", slug)
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
            f"{runtime.slug}: Worker-Box {worker_slug} als Mitglied eingetragen — "
            f"{worker_slug} gilt jetzt als belegt durch {name}; ein anderes Modell "
            f"dort zu starten stoppt es",
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
