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


# The one write site that is allowed to stay open with the switch off, and why:
# the operator's own "keep the result" in the group chat Finish dialog. It is
# an explicit operator action (not a background second truth), the dialog
# treats any refusal as a failure, and with "delete everything" a skipped
# write would lose the result. The group tab goes in clean-up PR H; that PR
# removes this exemption.
EXEMPT_FUNCTIONS = {("app/routers/groups.py", "memorize_group_result")}

#: Calls that write rows without constructing ``BoardMemory(...)`` directly.
_SQL_WRITERS = {"insert", "update", "delete"}


def _mentions_boardmemory(node: ast.AST, aliases: set[str]) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in aliases:
            return True
        if isinstance(sub, ast.Attribute) and sub.attr in aliases:
            return True
    return False


def _write_sites(source: str):
    """Yield (line, enclosing function node) for every place in ``source``
    that creates or bulk-changes ``board_memory`` rows:

    - ``BoardMemory(...)``, also under an import alias or as ``models.BoardMemory(...)``
    - ``BoardMemory.model_validate(...)`` / ``.model_construct(...)``
    - ``insert(...)`` / ``update(...)`` / ``delete(...)`` whose argument names BoardMemory
    """
    tree = ast.parse(source)
    aliases = {"BoardMemory"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == "BoardMemory" and a.asname:
                    aliases.add(a.asname)
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        hit = False
        if isinstance(f, ast.Name) and f.id in aliases:
            hit = True
        elif isinstance(f, ast.Attribute) and f.attr in aliases:
            hit = True
        elif (
            isinstance(f, ast.Attribute)
            and f.attr in {"model_validate", "model_construct"}
            and _mentions_boardmemory(f.value, aliases)
        ):
            hit = True
        elif (
            isinstance(f, (ast.Name, ast.Attribute))
            and (f.id if isinstance(f, ast.Name) else f.attr) in _SQL_WRITERS
            and any(_mentions_boardmemory(a, aliases) for a in node.args)
        ):
            hit = True
        if not hit:
            continue
        fn = node
        while fn is not None and not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn = parents.get(fn)
        yield node.lineno, fn


def _gate_line(fn: ast.AST | None) -> int | None:
    """First line in ``fn`` that asks the gate: a call to one of the gate
    helpers, or the gate dependency in the route decorator."""
    if fn is None:
        return None
    lines = []
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Name) and sub.id in GATE_NAMES:
            lines.append(sub.lineno)
    return min(lines) if lines else None


def _boardmemory_constructions():
    """Yield (file, line, enclosing function node) for every write site in app/."""
    for path in sorted(APP_DIR.rglob("*.py")):
        if path.name == "board_memory_gate.py":
            continue
        source = path.read_text(encoding="utf-8")
        for line, fn in _write_sites(source):
            yield path.relative_to(APP_DIR.parent), line, fn


def test_every_boardmemory_construction_asks_the_gate():
    found = list(_boardmemory_constructions())
    # Sanity: the grep of 2026-11-20 found 11 write sites in app/.
    assert len(found) >= 11, found
    ungated = []
    for rel, line, fn in found:
        if fn is not None and (str(rel), fn.name) in EXEMPT_FUNCTIONS:
            continue
        gate = _gate_line(fn)
        # The gate must be asked BEFORE the write, not just somewhere in the
        # function (decorator dependencies sit above the def, so they count).
        if gate is None or gate > line:
            ungated.append(f"{rel}:{line}")
    assert not ungated, f"BoardMemory written without the write switch: {ungated}"


@pytest.mark.parametrize(
    "source",
    [
        "from app.models.memory import BoardMemory as BM\ndef f():\n    BM(content='x')\n",
        "import app.models.memory as m\ndef f():\n    m.BoardMemory(content='x')\n",
        "def f():\n    BoardMemory.model_validate({'content': 'x'})\n",
        "def f(s):\n    s.execute(insert(BoardMemory).values(content='x'))\n",
        "def f(s):\n    s.execute(update(BoardMemory).values(title='x'))\n",
        "def f(s):\n    s.execute(delete(BoardMemory.__table__))\n",
    ],
)
def test_guard_detects_indirect_writes(source):
    """The completeness guard is not fooled by an alias, an attribute call,
    model_validate or a raw insert/update/delete."""
    sites = list(_write_sites(source))
    assert sites, source
    assert all(_gate_line(fn) is None for _, fn in sites)


def test_guard_rejects_a_gate_asked_after_the_write():
    source = (
        "def f():\n"
        "    BoardMemory(content='x')\n"
        "    if board_memory_write_skipped('late'):\n"
        "        return\n"
    )
    [(line, fn)] = list(_write_sites(source))
    assert _gate_line(fn) > line


def test_operator_group_memorize_is_the_only_exemption():
    """Exactly one write site stays open, and it is the operator's explicit
    'keep the result' (see EXEMPT_FUNCTIONS). A second exemption needs a
    reason here, not a silent add."""
    assert EXEMPT_FUNCTIONS == {("app/routers/groups.py", "memorize_group_result")}
    from app.main import app as fastapi_app

    route = next(
        r for r in fastapi_app.routes
        if isinstance(r, APIRoute) and r.endpoint.__name__ == "memorize_group_result"
    )
    assert not _gated(route), "group memorize must not answer 410 while the group tab exists"


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
        }:
            must.append(route)
    # create/update/delete memory, create/update/delete knowledge, attachment
    # upload/delete, merge_into, keep_both, unrelated + 2 outside memory.py.
    assert len(must) >= 13, [r.path for r in must]
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


# ── 4. What agents are told (TOOLS.md) ───────────────────────────────────


def _tools_md() -> str:
    from app.services.tools_md_builder import generate_tools_md

    # scopes=[] = every section (backward compat), so every write block shows.
    return generate_tools_md(
        name="T", emoji="x", raw_token="tkn", board_id="board-1", scopes=[]
    )


def test_tools_md_does_not_advertise_refused_writes_when_off():
    md = _tools_md()
    assert "POST $MC_API_URL/api/v1/agent/knowledge" not in md
    assert "/api/v1/agent/knowledge\\\" \\\\" not in md  # Flow 6 curl
    assert "-X POST \"$MC_API_URL/api/v1/agent/knowledge\"" not in md
    assert "POST $MC_API_URL/api/v1/agent/boards/board-1/memory" not in md
    # Reading the old entries is still documented, and writing points to the vault.
    assert "GET $MC_API_URL/api/v1/agent/knowledge" in md
    flow6 = md.split("### Flow 6")[1].split("### Flow")[0]
    assert "mc remember" in flow6


def test_tools_md_advertises_writes_again_when_on(writes_on):
    md = _tools_md()
    assert "POST $MC_API_URL/api/v1/agent/knowledge" in md
    assert "POST $MC_API_URL/api/v1/agent/boards/board-1/memory" in md
    assert "-X POST \"$MC_API_URL/api/v1/agent/knowledge\"" in md


# ── 5. Log noise ──────────────────────────────────────────────────────────


def test_skip_logs_once_per_place_at_info(caplog):
    import logging

    from app.services import board_memory_gate as gate

    gate._logged_skips.clear()
    with caplog.at_level(logging.DEBUG, logger="mc.board_memory_gate"):
        for _ in range(3):
            assert gate.board_memory_write_skipped("intelligence_daily_report")
        assert gate.board_memory_write_skipped("weekly_digest")
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert [r.getMessage().split("(")[1].split(")")[0] for r in infos] == [
        "intelligence_daily_report",
        "weekly_digest",
    ]
