"""Write switch for the legacy ``board_memory`` table (clean-up stage 2, PR B).

``board_memory`` is the second truth next to the vault (ADR-102 principle 6,
ADR-104). The switch ``BOARD_MEMORY_WRITE_ENABLED`` (default off) stops every
write path; reading stays, the table and its rows stay untouched.

The tests cover three things:

1. Completeness, read from the source: every ``BoardMemory(...)`` construction
   in ``app/`` sits in a function that asks the gate, and every mutating route
   of the legacy memory API carries the gate dependency. A new write path
   without the gate turns this red.
2. Behaviour with the switch off (the default): the API answers 410, nothing
   is inserted, changed or deleted, and reading still works. The background
   writers (auto_memory) write nothing.
3. The way back: switch on, the same call writes a row again.
"""
from __future__ import annotations

import ast
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.routing import APIRoute
from sqlmodel import func, select
from sqlmodel.ext.asyncio.session import AsyncSession

import app.config
from app.models.memory import BoardMemory
from tests.conftest import test_engine

APP_DIR = Path(__file__).resolve().parents[1] / "app"
GATE_NAMES = ("board_memory_writes_enabled", "board_memory_write_skipped", "require_board_memory_writes")
MUTATING = {"POST", "PATCH", "PUT", "DELETE"}
# POST, but a read: semantic search over the existing rows.
READ_ONLY_POSTS = {"/api/v1/memory/query"}


@pytest.fixture
def writes_on(monkeypatch):
    monkeypatch.setattr(app.config.settings, "board_memory_write_enabled", True)


async def _count() -> int:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return (await s.exec(select(func.count()).select_from(BoardMemory))).one()


async def _seed(**kwargs) -> uuid.UUID:
    entry = BoardMemory(
        id=uuid.uuid4(), content="old note", title="Old", source="user", **kwargs
    )
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(entry)
        await s.commit()
    return entry.id


# ── 1. Completeness ───────────────────────────────────────────────────────


def test_switch_defaults_to_off():
    assert app.config.Settings.model_fields["board_memory_write_enabled"].default is False


def _boardmemory_constructions():
    """Yield (file, line, enclosing function node) for every BoardMemory(...)."""
    for path in sorted(APP_DIR.rglob("*.py")):
        if path.name == "board_memory_gate.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "BoardMemory"
            ):
                fn = node
                while fn is not None and not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    fn = parents.get(fn)
                yield path.relative_to(APP_DIR.parent), node.lineno, fn


def test_every_boardmemory_construction_asks_the_gate():
    found = list(_boardmemory_constructions())
    # Sanity: the grep of 2026-11-20 found 11 write sites in app/.
    assert len(found) >= 11, found
    ungated = []
    for rel, line, fn in found:
        src = ast.unparse(fn) if fn is not None else ""
        if not any(name in src for name in GATE_NAMES):
            ungated.append(f"{rel}:{line}")
    assert not ungated, f"BoardMemory written without the write switch: {ungated}"


def _gated(route: APIRoute) -> bool:
    from app.services.board_memory_gate import require_board_memory_writes

    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        if dep.call is require_board_memory_writes:
            return True
        stack.extend(dep.dependencies)
    return False


def test_every_mutating_memory_route_carries_the_gate():
    from app.main import app as fastapi_app

    must = []
    for route in fastapi_app.routes:
        if not isinstance(route, APIRoute):
            continue
        mod = route.endpoint.__module__
        name = route.endpoint.__name__
        mutating = bool(route.methods & MUTATING) and route.path not in READ_ONLY_POSTS
        if mod == "app.routers.memory" and mutating:
            must.append(route)
        elif (mod, name) in {
            ("app.routers.agent_scoped", "agent_create_memory"),
            ("app.routers.agent_scoped", "agent_create_knowledge"),
            ("app.routers.groups", "memorize_group_result"),
        }:
            must.append(route)
    # create/update/delete memory, create/update/delete knowledge, attachment
    # upload/delete, merge_into, keep_both, unrelated + 3 outside memory.py.
    assert len(must) >= 14, [r.path for r in must]
    ungated = [f"{sorted(r.methods)} {r.path}" for r in must if not _gated(r)]
    assert not ungated, f"mutating board_memory routes without the gate: {ungated}"


# ── 2. Switch off (default): nothing is written ──────────────────────────


@pytest.mark.asyncio
async def test_api_create_is_refused_and_writes_nothing(auth_client, make_board):
    board = await make_board(slug="bm-gate")
    before = await _count()

    r1 = await auth_client.post(
        "/api/v1/knowledge", json={"content": "new", "title": "New", "memory_type": "knowledge"}
    )
    r2 = await auth_client.post(
        f"/api/v1/boards/{board.id}/memory", json={"content": "new", "source": "user"}
    )

    assert r1.status_code == 410, r1.text
    assert r2.status_code == 410, r2.text
    assert "BOARD_MEMORY_WRITE_ENABLED" in r1.json()["detail"]
    assert await _count() == before


@pytest.mark.asyncio
async def test_api_change_and_delete_are_refused_rows_stay(auth_client, make_board):
    board = await make_board(slug="bm-gate-2")
    entry_id = await _seed(board_id=board.id)

    patch_k = await auth_client.patch(f"/api/v1/knowledge/{entry_id}", json={"title": "Changed"})
    patch_b = await auth_client.patch(
        f"/api/v1/boards/{board.id}/memory/{entry_id}", json={"is_pinned": True}
    )
    del_k = await auth_client.delete(f"/api/v1/knowledge/{entry_id}")
    del_b = await auth_client.delete(f"/api/v1/boards/{board.id}/memory/{entry_id}")

    assert [patch_k.status_code, patch_b.status_code, del_k.status_code, del_b.status_code] == [410] * 4
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        row = await s.get(BoardMemory, entry_id)
    assert row is not None
    assert row.title == "Old"
    assert row.is_pinned is False


@pytest.mark.asyncio
async def test_reading_still_works(auth_client, make_board):
    board = await make_board(slug="bm-gate-3")
    entry_id = await _seed(board_id=board.id)

    one = await auth_client.get(f"/api/v1/knowledge/{entry_id}")
    listed = await auth_client.get(f"/api/v1/boards/{board.id}/memory")

    assert one.status_code == 200, one.text
    assert one.json()["entry"]["title"] == "Old"
    assert listed.status_code == 200
    assert [e["id"] for e in listed.json()] == [str(entry_id)]


@pytest.mark.asyncio
async def test_background_writer_writes_nothing(make_board, make_task):
    from app.services import auto_memory

    board = await make_board(slug="bm-gate-4")
    task = await make_task(board.id, title="Failing task")
    before = await _count()
    dedup = AsyncMock(return_value=True)

    with patch.object(auto_memory, "engine", test_engine), patch.object(auto_memory, "_dedup_check", dedup):
        await auto_memory.record_task_failure(task.id, None)
        await auto_memory.record_feedback_lesson(task.id, None, "rejected", "no")
        await auto_memory.record_phase_completion(task.id, [])
        await auto_memory.generate_weekly_digest()

    assert await _count() == before
    # The dedup key is not used up while the switch is off, so turning it on
    # later does not silently swallow the first real event.
    dedup.assert_not_awaited()


# ── 3. Way back: switch on, writing works again ──────────────────────────


@pytest.mark.asyncio
async def test_switch_on_writes_again(auth_client, writes_on, make_board, make_task):
    from app.services import auto_memory

    before = await _count()
    r = await auth_client.post(
        "/api/v1/knowledge", json={"content": "back", "title": "Back", "memory_type": "knowledge"}
    )
    assert r.status_code == 201, r.text

    board = await make_board(slug="bm-gate-5")
    task = await make_task(board.id, title="Failing task")
    with patch.object(auto_memory, "engine", test_engine), patch.object(
        auto_memory, "_dedup_check", AsyncMock(return_value=True)
    ), patch("app.services.memory_indexing.index_memory", AsyncMock(return_value=None)):
        await auto_memory.record_task_failure(task.id, None)

    assert await _count() == before + 2
