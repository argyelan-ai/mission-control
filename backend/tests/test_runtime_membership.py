"""ADR-096 §1–§2 — one writer for ``runtime_hosts``, structure-only checks,
running runtimes keep their boxes, the healer, and solo stays untouched.

What is pinned here:
  * Architecture: no backend code outside ``services/runtime_membership.py``
    inserts or deletes ``RuntimeHost`` rows.
  * ``set_members`` checks structure (ranks, roles, distinct/existing/enabled
    boxes, node count, recipe count, power-managed → 422) and NOT occupancy:
    Duo→Duo on the same boxes keeps working.
  * A RUNNING runtime that should move to other boxes → 409; filling an
    empty set is allowed.
  * ``topology.worker_host_id`` is no longer stored; responses derive it.
  * The recipe start's already-running branch fills missing rows.
  * ``membership_audit``: repair at startup (+ event), mark-only in the
    watcher cycle (+ one event, deduplicated); runs before the watcher.
  * Solo runtimes: create / start / switch to duo and back, zero rows, no probe.

Test data: box-a / box-b / box-c, RFC 5737 addresses only. No network.
"""

from __future__ import annotations

import ast
import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select

from app.models.activity import ActivityEvent
from app.models.runtime import Runtime
from app.models.runtime_host import RuntimeHost
from app.redis_client import RedisKeys
from app.services import runtime_membership as rm
from tests.test_recipe_switcher_p3 import (
    _duo_recipe,
    _FakeBox,
    _host,
    _probe,
    _recipe,
    _run_watcher,
    _runtime,
)

APP = Path(__file__).resolve().parents[1] / "app"
WRITER = APP / "services" / "runtime_membership.py"
MODEL = APP / "models" / "runtime_host.py"


# ── 1. Architecture: one writer ──────────────────────────────────────────────


def _writes_runtime_host(tree: ast.AST) -> list[int]:
    """Line numbers where a function both names RuntimeHost and writes rows.

    Writes = ``RuntimeHost(...)`` constructor, ``insert/delete/update(RuntimeHost)``,
    ``RuntimeHost.__table__``, or ``.delete(`` on a session inside a function
    that also selects RuntimeHost rows (the delete-the-loaded-rows pattern).
    """
    hits: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id == "RuntimeHost":
                hits.append(node.lineno)
            if isinstance(fn, ast.Name) and fn.id in {"insert", "delete", "update"}:
                if any(isinstance(a, ast.Name) and a.id == "RuntimeHost" for a in node.args):
                    hits.append(node.lineno)
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "__table__"
            and isinstance(node.value, ast.Name)
            and node.value.id == "RuntimeHost"
        ):
            hits.append(node.lineno)
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        if "RuntimeHost" not in names:
            continue
        for call in [n for n in ast.walk(fn) if isinstance(n, ast.Call)]:
            if isinstance(call.func, ast.Attribute) and call.func.attr == "delete":
                hits.append(call.lineno)
    return hits


def test_nobody_but_the_membership_module_writes_runtime_hosts():
    offenders = []
    for path in APP.rglob("*.py"):
        if path in (WRITER, MODEL):
            continue
        text = path.read_text()
        if "RuntimeHost" not in text and "runtime_hosts" not in text:
            continue
        lowered = text.lower()
        for verb in ("insert into runtime_hosts", "delete from runtime_hosts", "update runtime_hosts"):
            if verb in lowered:
                offenders.append(f"{path.relative_to(APP)}: raw SQL '{verb}'")
        for line in _writes_runtime_host(ast.parse(text)):
            offenders.append(f"{path.relative_to(APP)}:{line}")
    assert offenders == [], "runtime_hosts written outside runtime_membership: " + ", ".join(offenders)


def test_the_architecture_check_catches_a_writer():
    """The check itself must bite (sabotage probe in code form)."""
    sample = (
        "async def f(session, rt, h):\n"
        "    session.add(RuntimeHost(runtime_id=rt, host_id=h, role='worker', node_rank=1))\n"
    )
    assert _writes_runtime_host(ast.parse(sample)) == [2]
    sample_delete = (
        "async def g(session, rid):\n"
        "    rows = (await session.exec(select(RuntimeHost))).all()\n"
        "    for r in rows:\n"
        "        await session.delete(r)\n"
    )
    assert _writes_runtime_host(ast.parse(sample_delete)) == [4]


# ── 2. Structure checks ──────────────────────────────────────────────────────


async def _duo_rt(session, slug, head, **kw):
    kw.setdefault("topology", {"nodes": 2})
    return await _runtime(session, slug, head, **kw)


async def _rows(session, runtime_id) -> list[tuple[str, int, uuid.UUID]]:
    rows = (
        await session.exec(select(RuntimeHost).where(RuntimeHost.runtime_id == runtime_id))
    ).all()
    return sorted((r.role, r.node_rank, r.host_id) for r in rows)


@pytest.mark.asyncio
async def test_set_members_writes_head_rank0_and_worker_rank1(session):
    a = await _host(session, "box-a")
    b = await _host(session, "box-b", ssh_host="192.0.2.11")
    rt = await _duo_rt(session, "duo", a)

    assert await rm.set_members(session, rt, rm.duo(a.id, b.id)) is True
    assert await _rows(session, rt.id) == [("head", 0, a.id), ("worker", 1, b.id)]
    # Same set again: nothing changes, no probe needed.
    assert await rm.set_members(session, rt, rm.duo(a.id, b.id)) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["gap", "head_not_host", "roles", "same_box", "nodes", "solo_set"],
)
async def test_bad_shapes_are_422(session, case):
    a = await _host(session, "box-a")
    b = await _host(session, "box-b", ssh_host="192.0.2.11")
    rt = await _duo_rt(session, "duo", a)
    members = {
        "gap": [rm.MemberSpec(a.id, "head", 0), rm.MemberSpec(b.id, "worker", 2)],
        "head_not_host": rm.duo(b.id, a.id),
        "roles": [rm.MemberSpec(a.id, "worker", 0), rm.MemberSpec(b.id, "head", 1)],
        "same_box": [rm.MemberSpec(a.id, "head", 0), rm.MemberSpec(a.id, "worker", 1)],
        "nodes": [*rm.duo(a.id, b.id), rm.MemberSpec(uuid.uuid4(), "worker", 2)],
        "solo_set": [rm.MemberSpec(a.id, "head", 0)],
    }[case]
    with pytest.raises(rm.MembershipError) as exc:
        await rm.set_members(session, rt, members)
    assert exc.value.status == 422
    assert await _rows(session, rt.id) == []


@pytest.mark.asyncio
async def test_unknown_disabled_and_sleeping_boxes_are_422(session):
    a = await _host(session, "box-a")
    off = await _host(session, "box-off", ssh_host="192.0.2.12", enabled=False)
    sleepy = await _host(session, "box-sleepy", ssh_host="192.0.2.13", power_managed=True)
    rt = await _duo_rt(session, "duo", a)

    for worker_id, words in (
        (uuid.uuid4(), "not a registered host"),
        (off.id, "switched off"),
        (sleepy.id, "sleeps on demand"),
    ):
        with pytest.raises(rm.MembershipError) as exc:
            await rm.set_members(session, rt, rm.duo(a.id, worker_id))
        assert exc.value.status == 422
        assert words in exc.value.detail


@pytest.mark.asyncio
async def test_the_recipe_box_count_must_match(session):
    a = await _host(session, "box-a")
    b = await _host(session, "box-b", ssh_host="192.0.2.11")
    solo_recipe = await _recipe(session, "recipe-solo")
    rt = await _duo_rt(session, "duo", a)
    with pytest.raises(rm.MembershipError) as exc:
        await rm.set_members(session, rt, rm.duo(a.id, b.id), recipe=solo_recipe)
    assert exc.value.status == 422
    assert "recipe-solo" in exc.value.detail


@pytest.mark.asyncio
async def test_duo_to_duo_on_the_same_boxes_is_not_refused(session):
    """Review blocker B1: occupancy is the start's job (eviction), not the
    writer's. A second duo on the same head→worker pair must be recordable
    while the first one RUNS."""
    a = await _host(session, "box-a")
    b = await _host(session, "box-b", ssh_host="192.0.2.11")
    first = await _duo_rt(session, "duo-one", a)
    second = await _duo_rt(session, "duo-two", a)
    await rm.set_members(session, first, rm.duo(a.id, b.id))

    with _probe({"duo-one"}):
        await rm.set_members(session, second, rm.duo(a.id, b.id))

    assert await _rows(session, first.id) == [("head", 0, a.id), ("worker", 1, b.id)]
    assert await _rows(session, second.id) == [("head", 0, a.id), ("worker", 1, b.id)]


# ── 3. A running runtime keeps its boxes ─────────────────────────────────────


@pytest.mark.asyncio
async def test_a_running_duo_cannot_move_to_another_worker(session):
    a = await _host(session, "box-a")
    b = await _host(session, "box-b", ssh_host="192.0.2.11")
    c = await _host(session, "box-c", ssh_host="192.0.2.12")
    rt = await _duo_rt(session, "duo", a)
    await rm.set_members(session, rt, rm.duo(a.id, b.id))

    with _probe({"duo"}):
        with pytest.raises(rm.MembershipError) as exc:
            await rm.set_members(session, rt, rm.duo(a.id, c.id))
        assert exc.value.status == 409
        assert "box-b" in exc.value.detail
        with pytest.raises(rm.MembershipError) as demote:
            await rm.clear_members(session, rt)
        assert demote.value.status == 409
    assert await _rows(session, rt.id) == [("head", 0, a.id), ("worker", 1, b.id)]

    # Stopped → the move is allowed.
    with _probe(set()):
        await rm.set_members(session, rt, rm.duo(a.id, c.id))
    assert await _rows(session, rt.id) == [("head", 0, a.id), ("worker", 1, c.id)]


@pytest.mark.asyncio
async def test_filling_an_empty_set_of_a_running_duo_is_allowed(session):
    a = await _host(session, "box-a")
    b = await _host(session, "box-b", ssh_host="192.0.2.11")
    rt = await _duo_rt(session, "duo", a)
    probe = AsyncMock(return_value=True)
    with patch("app.services.recipe_switcher.probe_running", probe):
        await rm.set_members(session, rt, rm.duo(a.id, b.id))
    probe.assert_not_awaited()
    assert len(await _rows(session, rt.id)) == 2


# ── 4. CRUD: no stored worker_host_id, derived on read, 409/422 mapped ───────


def _crud_body(slug, head_id, **kw):
    body = {
        "slug": slug,
        "display_name": slug,
        "runtime_type": "vllm_docker",
        "endpoint": "http://192.0.2.30:8000/v1",
        "launch_command": "bash -lc 'true'",
        "host_id": str(head_id),
    }
    body.update(kw)
    return body


@pytest.mark.asyncio
async def test_crud_stores_rows_not_the_topology_key_and_derives_it_on_read(auth_client, session):
    a = await _host(session, "box-a")
    b = await _host(session, "box-b", ssh_host="192.0.2.11")

    with _probe(set()):
        resp = await auth_client.post(
            "/api/v1/runtimes/db",
            json=_crud_body("duo", a.id, topology={"nodes": 2, "worker_host_id": str(b.id)}),
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["topology"]["worker_host_id"] == str(b.id)

    stored = (await session.exec(select(Runtime).where(Runtime.slug == "duo"))).one()
    await session.refresh(stored)
    assert "worker_host_id" not in (stored.topology or {})
    assert stored.topology["nodes"] == 2
    assert await _rows(session, stored.id) == [("head", 0, a.id), ("worker", 1, b.id)]

    with patch(
        "app.services.runtime_manager.get_runtime_state",
        AsyncMock(return_value={"state": "stopped"}),
    ):
        one = (await auth_client.get("/api/v1/runtimes/duo")).json()
        listed = {r["slug"]: r for r in (await auth_client.get("/api/v1/runtimes")).json()["runtimes"]}
    assert one["topology"]["worker_host_id"] == str(b.id)
    assert listed["duo"]["topology"]["worker_host_id"] == str(b.id)


@pytest.mark.asyncio
async def test_crud_patch_of_a_running_duo_to_another_worker_is_409(auth_client, session):
    a = await _host(session, "box-a")
    b = await _host(session, "box-b", ssh_host="192.0.2.11")
    c = await _host(session, "box-c", ssh_host="192.0.2.12")
    with _probe(set()):
        await auth_client.post(
            "/api/v1/runtimes/db",
            json=_crud_body("duo", a.id, topology={"nodes": 2, "worker_host_id": str(b.id)}),
        )
    with _probe({"duo"}):
        resp = await auth_client.patch(
            "/api/v1/runtimes/db/duo",
            json={"topology": {"nodes": 2, "worker_host_id": str(c.id)}},
        )
    assert resp.status_code == 409, resp.text
    assert "stop it" in resp.json()["detail"]
    rt = (await session.exec(select(Runtime).where(Runtime.slug == "duo"))).one()
    assert await _rows(session, rt.id) == [("head", 0, a.id), ("worker", 1, b.id)]


@pytest.mark.asyncio
async def test_crud_a_sleeping_worker_is_422(auth_client, session):
    a = await _host(session, "box-a")
    sleepy = await _host(session, "box-sleepy", ssh_host="192.0.2.13", power_managed=True)
    with _probe(set()):
        resp = await auth_client.post(
            "/api/v1/runtimes/db",
            json=_crud_body("duo", a.id, topology={"nodes": 2, "worker_host_id": str(sleepy.id)}),
        )
    assert resp.status_code == 422, resp.text
    assert "sleeps on demand" in resp.json()["detail"]
    assert (await session.exec(select(Runtime).where(Runtime.slug == "duo"))).first() is None


# ── 5. Recipe start: no stored key, recorded worker wins, running branch fills ─


async def _start(auth_client, head, slug, *, body=None, running=frozenset(), already=None):
    start = AsyncMock(return_value={"ok": True, "message": "läuft an"})
    with (
        _probe(set(running)),
        patch("app.services.runtime_manager._ssh_run", _FakeBox()),
        patch("app.services.runtime_manager.start_runtime", start),
        patch(
            "app.services.runtime_manager.already_running",
            AsyncMock(return_value=already),
        ),
    ):
        resp = await auth_client.post(f"/api/v1/hosts/{head.id}/recipes/{slug}/start", json=body)
    return resp, start


@pytest.mark.asyncio
async def test_a_known_duo_restarts_with_its_recorded_worker(auth_client, session):
    a = await _host(session, "box-a")
    await _host(session, "box-b", ssh_host="192.0.2.11")
    c = await _host(session, "box-c", ssh_host="192.0.2.12")
    await _duo_recipe(session)
    resp, _ = await _start(auth_client, a, "recipe-duo", body={"worker_host_id": str(c.id)})
    assert resp.status_code == 200, resp.text

    # Second start without a named worker: c (recorded), not b (first in list).
    resp, start = await _start(auth_client, a, "recipe-duo")
    assert resp.status_code == 200, resp.text
    assert resp.json()["worker_host_id"] == str(c.id)
    rt = (await session.exec(select(Runtime))).one()
    await session.refresh(rt)
    assert "worker_host_id" not in rt.topology
    assert await _rows(session, rt.id) == [("head", 0, a.id), ("worker", 1, c.id)]


@pytest.mark.asyncio
async def test_already_running_duo_without_rows_is_filled_from_known_data(auth_client, session):
    a = await _host(session, "box-a")
    b = await _host(session, "box-b", ssh_host="192.0.2.11")
    recipe = await _duo_recipe(session)
    rt = await _runtime(
        session, "recipe-duo-box-a", a,
        model_identifier=recipe.model_identifier,
        topology={"nodes": 2, "recipe_slug": recipe.slug, "worker_host_id": str(b.id)},
    )
    resp, start = await _start(
        auth_client, a, "recipe-duo", already={"ok": True, "message": "läuft schon"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["already_running"] is True
    assert resp.json()["worker_host_id"] == str(b.id)
    start.assert_not_awaited()
    assert await _rows(session, rt.id) == [("head", 0, a.id), ("worker", 1, b.id)]


@pytest.mark.asyncio
async def test_already_running_duo_asked_for_another_worker_is_409(auth_client, session):
    a = await _host(session, "box-a")
    b = await _host(session, "box-b", ssh_host="192.0.2.11")
    c = await _host(session, "box-c", ssh_host="192.0.2.12")
    recipe = await _duo_recipe(session)
    rt = await _runtime(
        session, "recipe-duo-box-a", a,
        model_identifier=recipe.model_identifier,
        topology={"nodes": 2, "recipe_slug": recipe.slug},
    )
    await rm.set_members(session, rt, rm.duo(a.id, b.id))
    resp, start = await _start(
        auth_client, a, "recipe-duo", body={"worker_host_id": str(c.id)},
        already={"ok": True, "message": "läuft schon"},
    )
    assert resp.status_code == 409, resp.text
    assert "box-b" in resp.json()["detail"]
    start.assert_not_awaited()
    assert await _rows(session, rt.id) == [("head", 0, a.id), ("worker", 1, b.id)]


# ── 6. Solo stays first-class: create, start, switch to duo and back ─────────


@pytest.mark.asyncio
async def test_solo_create_start_switch_to_duo_and_back(auth_client, session):
    a = await _host(session, "box-a")
    b = await _host(session, "box-b", ssh_host="192.0.2.11")
    await _recipe(session, "recipe-x")
    await _duo_recipe(session)

    # Create a solo runtime through the API: zero rows, no probe at all.
    probe = AsyncMock(return_value=False)
    with patch("app.services.recipe_switcher.probe_running", probe):
        resp = await auth_client.post("/api/v1/runtimes/db", json=_crud_body("hand-solo", a.id))
        assert resp.status_code == 200, resp.text
        assert resp.json()["member_hosts"] == []
        resp = await auth_client.patch(
            "/api/v1/runtimes/db/hand-solo", json={"topology": {"nodes": 1}}
        )
        assert resp.status_code == 200, resp.text
    probe.assert_not_awaited()

    # Start a solo recipe: no member rows.
    resp, start = await _start(auth_client, a, "recipe-x")
    assert resp.status_code == 200, resp.text
    start.assert_awaited_once()
    solo = (await session.exec(select(Runtime).where(Runtime.slug == resp.json()["runtime_slug"]))).one()
    assert await _rows(session, solo.id) == []

    # Switch the same boxes to the duo recipe: two rows.
    resp, start = await _start(auth_client, a, "recipe-duo")
    assert resp.status_code == 200, resp.text
    duo_rt = (await session.exec(select(Runtime).where(Runtime.slug == resp.json()["runtime_slug"]))).one()
    assert await _rows(session, duo_rt.id) == [("head", 0, a.id), ("worker", 1, b.id)]

    # And back to solo on the head: allowed, the solo instance still has no rows.
    resp, start = await _start(auth_client, a, "recipe-x")
    assert resp.status_code == 200, resp.text
    start.assert_awaited_once()
    assert await _rows(session, solo.id) == []

    # The worker box can run a solo model of its own once the duo is stopped.
    resp, start = await _start(auth_client, b, "recipe-x")
    assert resp.status_code == 200, resp.text

    # Demote the (stopped) duo to solo through the API: rows go.
    with _probe(set()):
        resp = await auth_client.patch(
            f"/api/v1/runtimes/db/{duo_rt.slug}",
            json={"topology": {"nodes": 1, "recipe_slug": "recipe-duo"}},
        )
    assert resp.status_code == 200, resp.text
    assert await _rows(session, duo_rt.id) == []


# ── 7. Healer: repair at startup, mark in the watcher cycle ──────────────────


@pytest.mark.asyncio
async def test_audit_repairs_a_legacy_worker_field_and_reports_it(session):
    a = await _host(session, "box-a")
    b = await _host(session, "box-b", ssh_host="192.0.2.11")
    rt = await _duo_rt(
        session, "legacy-duo", a, topology={"nodes": 2, "worker_host_id": str(b.id)}
    )

    findings = await rm.membership_audit(session, repair=False)
    assert [f.code for f in findings] == ["members_incomplete"]
    assert await _rows(session, rt.id) == []  # mark-only never writes

    findings = await rm.membership_audit(session, repair=True)
    assert [(f.code, f.repaired) for f in findings] == [("members_repaired", True)]
    assert await _rows(session, rt.id) == [("head", 0, a.id), ("worker", 1, b.id)]
    events = (
        await session.exec(
            select(ActivityEvent).where(ActivityEvent.event_type == "runtime.members_repaired")
        )
    ).all()
    assert len(events) == 1
    assert "box-b now counts as used" in events[0].title

    assert await rm.membership_audit(session, repair=True) == []


@pytest.mark.asyncio
async def test_audit_never_guesses_a_worker(session):
    a = await _host(session, "box-a")
    await _host(session, "box-b", ssh_host="192.0.2.11")
    rt = await _duo_rt(session, "no-worker", a, topology={"nodes": 2})
    findings = await rm.membership_audit(session, repair=True)
    assert [f.code for f in findings] == ["members_incomplete"]
    assert await _rows(session, rt.id) == []


@pytest.mark.asyncio
async def test_watcher_cycle_marks_once_and_never_writes(async_session, fake_redis):
    a = await _host(async_session, "box-a")
    b = await _host(async_session, "box-b", ssh_host="192.0.2.11")
    rt = await _duo_rt(
        async_session, "legacy-duo", a, topology={"nodes": 2, "worker_host_id": str(b.id)}
    )
    start = AsyncMock(return_value={"ok": True, "message": "läuft an"})
    await _run_watcher(async_session, fake_redis, start_mock=start, ticks=2)

    assert await _rows(async_session, rt.id) == []
    mark = json.loads(await fake_redis.get(RedisKeys.runtime_members_issue("legacy-duo")))
    assert mark["code"] == "members_incomplete"
    events = (
        await async_session.exec(
            select(ActivityEvent).where(ActivityEvent.event_type == "runtime.members_incomplete")
        )
    ).all()
    assert len(events) == 1


@pytest.mark.asyncio
async def test_watcher_autostart_takes_the_worker_from_members_and_never_guesses(
    async_session, fake_redis
):
    recipe = await _duo_recipe(async_session)
    a = await _host(async_session, "box-a", autostart_enabled=True, autostart_recipe_slug=recipe.slug)
    b = await _host(async_session, "box-b", ssh_host="192.0.2.11")
    rt = await _runtime(
        async_session, "recipe-duo-box-a", a,
        model_identifier=recipe.model_identifier,
        topology={"nodes": 2, "recipe_slug": recipe.slug},
    )
    switcher = AsyncMock(return_value={"ok": True, "message": "Verbund startet"})
    start = AsyncMock(return_value={"ok": True, "message": "läuft an"})
    with patch("app.services.recipe_switcher.start_recipe_on_host", switcher):
        await _run_watcher(async_session, fake_redis, start_mock=start)
    switcher.assert_not_awaited()  # no recorded worker → no guess
    start.assert_not_awaited()

    await rm.set_members(async_session, rt, rm.duo(a.id, b.id))
    await fake_redis.flushall()
    with patch("app.services.recipe_switcher.start_recipe_on_host", switcher):
        await _run_watcher(async_session, fake_redis, start_mock=start)
    switcher.assert_awaited_once()
    assert switcher.await_args.kwargs["worker_host_id"] == str(b.id)


def test_startup_audit_runs_before_the_watcher_starts():
    """Fable review 2, point 2: the healer runs in the startup path BEFORE the
    watcher task exists, so the watcher's first cycle (duo autostart reads
    runtime_hosts) never sees half-repaired rows. Awaited, not scheduled."""
    from app import background

    src = Path(background.__file__).read_text()
    body = src[src.index("async def start_background_services"):]
    audit_at = body.index("await runtime_membership.startup_audit()")
    assert audit_at < body.index("await runtime_watcher.start()")


@pytest.mark.asyncio
async def test_startup_audit_never_raises():
    with patch.object(rm, "membership_audit", AsyncMock(side_effect=RuntimeError("db down"))):
        assert await rm.startup_audit() == []
