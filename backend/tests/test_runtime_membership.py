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
import re
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


#: Calls that may take ``RuntimeHost`` (or its table) as an argument without
#: writing: building a SELECT, a typed ``session.get``, type checks. Anything
#: else that receives the model is treated as a write — a ratchet that fails
#: closed: a new legitimate reader is one entry here, a new writer is red.
_READ_CALLEES = {"select", "get", "isinstance", "issubclass", "type", "col", "func"}
_WRITE_CALLEES = {"insert", "delete", "update", "upsert"}
_RAW_SQL = re.compile(
    r"\b(insert\s+into|delete\s+from|update|merge\s+into)\s+[\"'`]?runtime_hosts\b",
    re.IGNORECASE,
)


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    """``{local name: original name}`` — ``from sqlalchemy import delete as d``."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                out[alias.asname or alias.name] = alias.name
    return out


def _callee(fn: ast.AST, aliases: dict[str, str]) -> str | None:
    if isinstance(fn, ast.Name):
        return aliases.get(fn.id, fn.id)
    if isinstance(fn, ast.Attribute):
        return fn.attr
    return None


def _names_runtime_host(arg: ast.AST) -> bool:
    """``RuntimeHost``, ``RuntimeHost.__table__`` or ``models.RuntimeHost``."""
    if isinstance(arg, ast.Name) and arg.id == "RuntimeHost":
        return True
    if isinstance(arg, ast.Attribute):
        if arg.attr == "RuntimeHost":
            return True
        if arg.attr == "__table__":
            return _names_runtime_host(arg.value)
    return False


def _writes_runtime_host(tree: ast.AST) -> list[int]:
    """Line numbers where code writes ``runtime_hosts`` rows.

    Writes = the ``RuntimeHost(...)`` constructor; any use of
    ``RuntimeHost.__table__`` (Core writes go through it); any call that gets
    the model as an argument and is not a known read (``insert``/``delete``/
    ``update`` under any import alias, ``session.execute(...)`` …); ``.delete(``
    on a session inside a function that also names RuntimeHost (the
    delete-the-loaded-rows pattern); raw SQL writes in string constants, with
    any spacing or case.
    """
    aliases = _import_aliases(tree)
    hits: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _callee(node.func, aliases)
            if _names_runtime_host(node.func) and not (
                isinstance(node.func, ast.Attribute) and node.func.attr != "RuntimeHost"
            ):
                hits.add(node.lineno)  # RuntimeHost(...) / models.RuntimeHost(...)
            if any(_names_runtime_host(a) for a in [*node.args, *(k.value for k in node.keywords)]):
                if name in _WRITE_CALLEES or name not in _READ_CALLEES:
                    hits.add(node.lineno)
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "__table__"
            and _names_runtime_host(node.value)
        ):
            hits.add(node.lineno)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _RAW_SQL.search(node.value):
                hits.add(node.lineno)
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        if "RuntimeHost" not in names:
            continue
        for call in [n for n in ast.walk(fn) if isinstance(n, ast.Call)]:
            if isinstance(call.func, ast.Attribute) and call.func.attr == "delete":
                hits.add(call.lineno)
    return sorted(hits)


def test_nobody_but_the_membership_module_writes_runtime_hosts():
    offenders = []
    for path in APP.rglob("*.py"):
        if path in (WRITER, MODEL):
            continue
        text = path.read_text()
        if "RuntimeHost" not in text and "runtime_hosts" not in text.lower():
            continue
        for line in _writes_runtime_host(ast.parse(text)):
            offenders.append(f"{path.relative_to(APP)}:{line}")
    assert offenders == [], "runtime_hosts written outside runtime_membership: " + ", ".join(offenders)


@pytest.mark.parametrize(
    "sample",
    [
        "async def f(session, rt, h):\n"
        "    session.add(RuntimeHost(runtime_id=rt, host_id=h, role='worker', node_rank=1))\n",
        "async def g(session, rid):\n"
        "    rows = (await session.exec(select(RuntimeHost))).all()\n"
        "    for r in rows:\n"
        "        await session.delete(r)\n",
        # aliased Core construct
        "from sqlalchemy import delete as sa_delete\n"
        "async def h(session):\n"
        "    await session.execute(sa_delete(RuntimeHost))\n",
        # the table object handed to execute / insert
        "async def i(session):\n"
        "    await session.execute(RuntimeHost.__table__.delete())\n",
        # unknown callee getting the model
        "async def j(session, rows):\n"
        "    await session.run_sync(lambda s: s.bulk_insert_mappings(RuntimeHost, rows))\n",
        # raw SQL, other spacing / case / newline
        "from sqlalchemy import text\n"
        "async def k(session):\n"
        "    await session.execute(text('DELETE  FROM\\n runtime_hosts WHERE 1=1'))\n",
        "async def l(session):\n"
        "    await session.execute(text(\"Insert Into runtime_hosts VALUES (1)\"))\n",
    ],
    ids=["ctor", "loaded-delete", "alias", "table", "bulk", "raw-spacing", "raw-case"],
)
def test_the_architecture_check_catches_a_writer(sample):
    """The check itself must bite (sabotage probe in code form)."""
    assert _writes_runtime_host(ast.parse(sample)) != []


def test_the_architecture_check_lets_readers_pass():
    sample = (
        "async def r(session, rid, hid):\n"
        "    rows = (await session.exec(select(RuntimeHost).where(RuntimeHost.runtime_id == rid))).all()\n"
        "    one = await session.get(RuntimeHost, hid)\n"
        "    return rows, one, isinstance(one, RuntimeHost)\n"
    )
    assert _writes_runtime_host(ast.parse(sample)) == []


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
        (uuid.uuid4(), "nicht eingetragen"),
        (off.id, "ausgeschaltet"),
        (sleepy.id, "schläft bei Bedarf"),
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
    assert "erst stoppen" in resp.json()["detail"]
    rt = (await session.exec(select(Runtime).where(Runtime.slug == "duo"))).one()
    assert await _rows(session, rt.id) == [("head", 0, a.id), ("worker", 1, b.id)]


@pytest.mark.asyncio
async def test_crud_a_sleeping_worker_is_refused(auth_client, session):
    a = await _host(session, "box-a")
    sleepy = await _host(session, "box-sleepy", ssh_host="192.0.2.13", power_managed=True)
    with _probe(set()):
        resp = await auth_client.post(
            "/api/v1/runtimes/db",
            json=_crud_body("duo", a.id, topology={"nodes": 2, "worker_host_id": str(sleepy.id)}),
        )
    # Refused by the candidate rule (same as the recipe start) before any write.
    assert resp.status_code == 409, resp.text
    assert "schläft bei Bedarf" in resp.json()["detail"]
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
    assert "box-b gilt jetzt als belegt" in events[0].title

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


# ── 8. Solo <-> Duo on the same boxes WITH engines running (critic 19.10.) ───


def _evict_ok():
    return AsyncMock(return_value={"ok": True, "message": "Box freigegeben", "stopped": []})


async def _solo_rt(session, slug, host, recipe):
    return await _runtime(
        session, slug, host,
        model_identifier=recipe.model_identifier,
        topology={"nodes": 1, "recipe_slug": recipe.slug},
    )


@pytest.mark.asyncio
async def test_two_running_solos_switch_to_the_duo_on_the_same_boxes(auth_client, session):
    """Mark 19.10.: Solo<->Duo on the same boxes. A solo on the worker box is
    evicted like the old duo in Duo->Duo (worker first, then head) — not 409."""
    a = await _host(session, "box-a")
    b = await _host(session, "box-b", ssh_host="192.0.2.11")
    solo_x = await _recipe(session, "recipe-x", min_vram_gb=100.0)
    solo_y = await _recipe(session, "recipe-y", min_vram_gb=100.0)
    await _duo_recipe(session)
    await _solo_rt(session, "recipe-x-box-a", a, solo_x)
    await _solo_rt(session, "recipe-y-box-b", b, solo_y)

    evict = _evict_ok()
    with patch("app.services.runtime_manager.ensure_exclusive_host", evict):
        resp, start = await _start(
            auth_client, a, "recipe-duo", running={"recipe-x-box-a", "recipe-y-box-b"}
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["worker_host_id"] == str(b.id)
    start.assert_awaited_once()  # the head's own eviction runs inside start_runtime
    evict.assert_awaited_once()
    assert evict.await_args.kwargs["host_id"] == b.id  # worker box freed first
    duo_rt = (
        await session.exec(select(Runtime).where(Runtime.slug == resp.json()["runtime_slug"]))
    ).one()
    assert await _rows(session, duo_rt.id) == [("head", 0, a.id), ("worker", 1, b.id)]


@pytest.mark.asyncio
async def test_a_running_duo_switches_to_a_solo_on_its_worker_box(auth_client, session):
    """The other direction: the solo start on the worker box evicts the duo
    (ensure_exclusive_host knows members) instead of answering 409."""
    a = await _host(session, "box-a")
    b = await _host(session, "box-b", ssh_host="192.0.2.11")
    await _recipe(session, "recipe-x", min_vram_gb=100.0)
    recipe = await _duo_recipe(session)
    duo_rt = await _runtime(
        session, "recipe-duo-box-a", a,
        model_identifier=recipe.model_identifier,
        topology={"nodes": 2, "recipe_slug": recipe.slug},
    )
    await rm.set_members(session, duo_rt, rm.duo(a.id, b.id))

    resp, start = await _start(auth_client, b, "recipe-x", running={"recipe-duo-box-a"})
    assert resp.status_code == 200, resp.text
    start.assert_awaited_once()
    assert resp.json()["switch_lock"]["state"] in {"idle", "unknown"}  # it displaces the duo


@pytest.mark.asyncio
async def test_a_non_exclusive_solo_on_a_running_duos_worker_box_is_still_refused(
    auth_client, session
):
    """A solo that does not claim the box would not evict anything — it would
    load next to the duo's worker. That stays a 409."""
    a = await _host(session, "box-a")
    b = await _host(session, "box-b", ssh_host="192.0.2.11")
    await _recipe(session, "recipe-small")  # no min_vram_gb → not exclusive
    recipe = await _duo_recipe(session)
    duo_rt = await _runtime(
        session, "recipe-duo-box-a", a,
        model_identifier=recipe.model_identifier,
        topology={"nodes": 2, "recipe_slug": recipe.slug},
    )
    await rm.set_members(session, duo_rt, rm.duo(a.id, b.id))

    resp, start = await _start(auth_client, b, "recipe-small", running={"recipe-duo-box-a"})
    assert resp.status_code == 409, resp.text
    start.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_box_that_heads_another_running_duo_is_not_taken_as_worker(auth_client, session):
    """Only a plain solo is evictable for a worker slot. A box that heads (or
    serves as worker for) a duo of another head stays busy."""
    a = await _host(session, "box-a")
    b = await _host(session, "box-b", ssh_host="192.0.2.11")
    c = await _host(session, "box-c", ssh_host="192.0.2.12")
    await _duo_recipe(session)
    other = await _duo_rt(session, "other-duo", b)
    await rm.set_members(session, other, rm.duo(b.id, c.id))

    resp, start = await _start(auth_client, a, "recipe-duo", running={"other-duo"})
    assert resp.status_code == 409, resp.text
    start.assert_not_awaited()
    assert (await session.exec(select(Runtime).where(Runtime.slug != "other-duo"))).all() == []


# ── 9. A power-managed box is never offered as a worker (no half row) ────────


@pytest.mark.asyncio
async def test_a_power_managed_box_is_no_worker_candidate_and_leaves_no_row(auth_client, session):
    a = await _host(session, "box-a")
    p = await _host(session, "box-p", ssh_host="192.0.2.14", power_managed=True)
    await _duo_recipe(session)

    resp, start = await _start(auth_client, a, "recipe-duo")
    assert resp.status_code == 409, resp.text
    resp, _ = await _start(auth_client, a, "recipe-duo", body={"worker_host_id": str(p.id)})
    assert resp.status_code == 409, resp.text
    start.assert_not_awaited()
    assert (await session.exec(select(Runtime))).all() == []
    assert await rm.membership_audit(session, repair=False) == []

    with _probe(set()):
        listed = (await auth_client.get(f"/api/v1/hosts/{a.id}/recipes")).json()
    entries = listed if isinstance(listed, list) else listed.get("recipes", [])
    duo_entry = next(e for e in entries if e["slug"] == "recipe-duo")
    assert all(w["slug"] != "box-p" for w in duo_entry["candidate_workers"])


# ── 10. Watcher: a deterministic refusal is not a failed recovery ────────────


async def _watched_duo(session, *, worker=True):
    recipe = await _duo_recipe(session)
    a = await _host(session, "box-a", autostart_enabled=True, autostart_recipe_slug=recipe.slug)
    b = await _host(session, "box-b", ssh_host="192.0.2.11")
    rt = await _runtime(
        session, "recipe-duo-box-a", a,
        model_identifier=recipe.model_identifier,
        topology={"nodes": 2, "recipe_slug": recipe.slug},
    )
    if worker:
        await rm.set_members(session, rt, rm.duo(a.id, b.id))
    return a, b, rt


async def _recovery_events(session):
    return [
        e.event_type
        for e in (await session.exec(select(ActivityEvent))).all()
        if e.event_type.startswith("runtime.auto_recovery")
    ]


@pytest.mark.asyncio
async def test_watcher_does_not_burn_attempts_on_a_duo_without_worker(async_session, fake_redis):
    _, _, rt = await _watched_duo(async_session, worker=False)
    switcher = AsyncMock(return_value={"ok": True, "message": "Verbund startet"})
    start = AsyncMock(return_value={"ok": True, "message": "läuft an"})
    with patch("app.services.recipe_switcher.start_recipe_on_host", switcher):
        await _run_watcher(async_session, fake_redis, start_mock=start, ticks=8)
    switcher.assert_not_awaited()
    assert await _recovery_events(async_session) == []
    assert await fake_redis.get(RedisKeys.runtime_recovery_failures(rt.slug)) is None
    # Reported once, as what it is: members incomplete.
    incomplete = (
        await async_session.exec(
            select(ActivityEvent).where(ActivityEvent.event_type == "runtime.members_incomplete")
        )
    ).all()
    assert len(incomplete) == 1


@pytest.mark.asyncio
async def test_watcher_never_evicts_an_active_solo_on_the_duos_worker_box(
    async_session, fake_redis
):
    """Auto-recovery must not overrule the operator: a solo the operator
    started on the worker box blocks the duo's revival — without counting a
    failed attempt."""
    _, b, rt = await _watched_duo(async_session)
    solo = await _recipe(async_session, "recipe-x", min_vram_gb=100.0)
    await _solo_rt(async_session, "recipe-x-box-b", b, solo)
    await fake_redis.set(
        RedisKeys.runtime_switching("recipe-x-box-b"),
        json.dumps({"phase": "loading", "source": "switch"}),
    )
    switcher = AsyncMock(return_value={"ok": True, "message": "Verbund startet"})
    start = AsyncMock(return_value={"ok": True, "message": "läuft an"})
    with patch("app.services.recipe_switcher.start_recipe_on_host", switcher):
        await _run_watcher(async_session, fake_redis, start_mock=start, ticks=8)
    switcher.assert_not_awaited()
    assert await _recovery_events(async_session) == []
    assert await fake_redis.get(RedisKeys.runtime_recovery_failures(rt.slug)) is None


# ── 11. CRUD: "is it running?" is asked about the boxes it runs on NOW ───────


@pytest.mark.asyncio
async def test_crud_moving_the_head_of_a_running_duo_is_409(auth_client, session):
    """The probe must ask the CURRENT head, before the row is changed. Asked
    after the mutation it probed the new head, saw nothing and let a running
    duo move."""
    a = await _host(session, "box-a")
    b = await _host(session, "box-b", ssh_host="192.0.2.11")
    c = await _host(session, "box-c", ssh_host="192.0.2.12")
    with _probe(set()):
        await auth_client.post(
            "/api/v1/runtimes/db",
            json=_crud_body("duo", a.id, topology={"nodes": 2, "worker_host_id": str(b.id)}),
        )

    async def _runs_on_a(runtime, host=None, **kw):
        return runtime.slug == "duo" and host is not None and host.ssh_host == "192.0.2.10"

    with patch("app.services.recipe_switcher.probe_running", _runs_on_a):
        resp = await auth_client.patch("/api/v1/runtimes/db/duo", json={"host_id": str(c.id)})
    assert resp.status_code == 409, resp.text
    rt = (await session.exec(select(Runtime).where(Runtime.slug == "duo"))).one()
    await session.refresh(rt)
    assert rt.host_id == a.id
    assert await _rows(session, rt.id) == [("head", 0, a.id), ("worker", 1, b.id)]


# ── 12. Review #893: the healer never races a start ──────────────────────────


@pytest.mark.asyncio
async def test_audit_skips_a_runtime_that_is_switching(session):
    """ADR-096 §2: a runtime with an active switching marker is mid-start —
    its members are being written right now. The healer does not touch it
    (no write, no event); the next backend start repairs it if still needed."""
    from app.services import runtime_grace

    a = await _host(session, "box-a")
    b = await _host(session, "box-b", ssh_host="192.0.2.11")
    rt = await _duo_rt(
        session, "legacy-duo", a, topology={"nodes": 2, "worker_host_id": str(b.id)}
    )
    await runtime_grace.mark_switching(
        "legacy-duo", runtime_grace.PHASE_LAUNCHING, runtime_grace.SOURCE_SWITCH
    )

    for repair in (True, False):
        assert await rm.membership_audit(session, repair=repair) == []
    assert await _rows(session, rt.id) == []
    events = (
        await session.exec(
            select(ActivityEvent).where(ActivityEvent.event_type == "runtime.members_repaired")
        )
    ).all()
    assert events == []

    await runtime_grace.clear_switching("legacy-duo")
    findings = await rm.membership_audit(session, repair=True)
    assert [(f.code, f.repaired) for f in findings] == [("members_repaired", True)]


@pytest.mark.asyncio
async def test_audit_rereads_the_runtime_instead_of_trusting_its_snapshot(session):
    """The healer must decide on the row as it is NOW, not as it was loaded.

    The session already holds the runtime with the legacy worker field (as
    the old one-shot ``select(Runtime)`` did); another writer has meanwhile
    dropped that field (a start of the same duo with a new worker). Writing
    the stale worker back would put the old box into the table."""
    from tests.conftest import test_engine
    from sqlmodel.ext.asyncio.session import AsyncSession

    a = await _host(session, "box-a")
    b = await _host(session, "box-b", ssh_host="192.0.2.11")
    rt = await _duo_rt(
        session, "legacy-duo", a, topology={"nodes": 2, "worker_host_id": str(b.id)}
    )
    assert rt.topology.get("worker_host_id") == str(b.id)  # the session's snapshot

    async with AsyncSession(test_engine, expire_on_commit=False) as other:
        fresh = await other.get(Runtime, rt.id, populate_existing=True)
        fresh.topology = {"nodes": 2}
        other.add(fresh)
        await other.commit()

    findings = await rm.membership_audit(session, repair=True)
    assert [f.code for f in findings] == ["members_incomplete"]
    assert await _rows(session, rt.id) == []


@pytest.mark.asyncio
async def test_watcher_reports_a_duo_held_back_by_a_busy_worker_box_once(
    async_session, fake_redis
):
    """ADR-096 "Mark sees it": a duo the watcher does NOT revive because its
    worker box is held by another engine is an activity event — the existing
    ``runtime.exclusive_blocked`` with ``box=worker`` — not just a log line.
    Once per blocker, not once per cycle; in both UI languages."""
    _, b, rt = await _watched_duo(async_session)
    solo = await _recipe(async_session, "recipe-x", min_vram_gb=100.0)
    await _solo_rt(async_session, "recipe-x-box-b", b, solo)
    await fake_redis.set(
        RedisKeys.runtime_switching("recipe-x-box-b"),
        json.dumps({"phase": "loading", "source": "switch"}),
    )
    switcher = AsyncMock(return_value={"ok": True, "message": "Verbund startet"})
    start = AsyncMock(return_value={"ok": True, "message": "läuft an"})
    with patch("app.services.recipe_switcher.start_recipe_on_host", switcher):
        await _run_watcher(async_session, fake_redis, start_mock=start, ticks=8)
    switcher.assert_not_awaited()

    blocked = (
        await async_session.exec(
            select(ActivityEvent).where(ActivityEvent.event_type == "runtime.exclusive_blocked")
        )
    ).all()
    assert len(blocked) == 1
    event = blocked[0]
    assert event.severity == "warning"
    assert event.detail["slug"] == rt.slug
    assert event.detail["box"] == "worker"
    assert event.detail["source"] == "auto_recovery"
    assert event.detail["blocked_by"] == "recipe-x-box-b"
    assert "recipe-x-box-b" in event.title and "box-b" in event.title
    assert set(event.detail["sentence"]) == {"de", "en"}
    assert "recipe-x-box-b" in event.detail["sentence"]["en"]
