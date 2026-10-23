"""Bench round-2 user test (t2 MUSS-1, t2 SOLL-1, r1 NICE-7).

- MUSS-1: the dialog's default row "Aktives Modell (auto)" sends
  ``label: ""`` on purpose (the backend fills it from the model it resolves
  at create time). The schema used to demand ``min_length=1`` and refused
  the request with a Pydantic 422 before the fill-in ever ran.
- SOLL-1: entries were returned (and composed into the video) sorted by
  label, so "DeepSeek · Max" always came before "DeepSeek · Off" no matter
  in which order the operator added the rows. The row order is now stored
  (``bench_entries.position``) and every reader sorts by it.
- NICE-7: the artifact folder was derived from the label on every call, so
  renaming an entry after a run put the next recording into a new folder
  and left the old one orphaned. Once a run has written its folder, the
  entry keeps it.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
import uuid
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("app.verticals.bench_studio")

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.models.bench import BenchChallenge, BenchEntry
from app.verticals.bench_studio import orchestrator


@pytest.fixture(autouse=True)
def _no_background(monkeypatch):
    monkeypatch.setattr(orchestrator, "start_challenge", AsyncMock())


def _body(models):
    return {
        "title": "Bouncing balls",
        "prompt_text": "100 bouncing balls, one index.html",
        "mode": "side_by_side",
        "models": models,
    }


# ── MUSS-1: empty label on the auto row ───────────────────────────────────


@pytest.mark.asyncio
async def test_auto_row_with_empty_label_starts_and_gets_resolved_model(auth_client, monkeypatch):
    """Exactly what the dialog sends for the untouched default row."""
    monkeypatch.setattr(
        orchestrator, "resolve_spark_model_or_422", AsyncMock(return_value="deepseek-live")
    )
    resp = await auth_client.post(
        "/api/v1/bench/challenges",
        json=_body([{"label": "", "source_kind": "spark", "spark_model": "", "display_tag": ""}]),
    )
    assert resp.status_code == 201, resp.text
    entry = resp.json()["entries"][0]
    assert entry["model_label"] == "deepseek-live"
    assert entry["spark_model"] == "deepseek-live"


@pytest.mark.asyncio
async def test_label_may_be_omitted_entirely(auth_client, monkeypatch):
    monkeypatch.setattr(
        orchestrator, "resolve_spark_model_or_422", AsyncMock(return_value="deepseek-live")
    )
    resp = await auth_client.post(
        "/api/v1/bench/challenges", json=_body([{"source_kind": "spark"}])
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["entries"][0]["model_label"] == "deepseek-live"


@pytest.mark.asyncio
async def test_fixed_spark_model_with_empty_label_takes_the_model_name(auth_client, monkeypatch):
    resolve = AsyncMock(return_value="unused")
    monkeypatch.setattr(orchestrator, "resolve_spark_model_or_422", resolve)
    resp = await auth_client.post(
        "/api/v1/bench/challenges",
        json=_body([{"label": "  ", "source_kind": "spark", "spark_model": "qwen-fixed"}]),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["entries"][0]["model_label"] == "qwen-fixed"
    resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_long_model_name_is_cut_to_the_label_limit(auth_client, monkeypatch):
    from app.verticals.bench_studio.routers import LABEL_MAX_LEN

    long_name = "org/" + "x" * 120
    monkeypatch.setattr(
        orchestrator, "resolve_spark_model_or_422", AsyncMock(return_value=long_name)
    )
    resp = await auth_client.post(
        "/api/v1/bench/challenges", json=_body([{"label": "", "source_kind": "spark"}])
    )
    assert resp.status_code == 201, resp.text
    entry = resp.json()["entries"][0]
    assert entry["model_label"] == long_name[:LABEL_MAX_LEN]
    assert entry["spark_model"] == long_name  # the model itself is never cut


@pytest.mark.asyncio
async def test_agent_row_without_label_gets_a_readable_reason(auth_client):
    """No model name to fall back on -> 422 with a plain-text reason (not a
    Pydantic list the dialog toast cannot read)."""
    resp = await auth_client.post(
        "/api/v1/bench/challenges",
        json=_body([{"label": "", "source_kind": "agent", "agent_id": str(uuid.uuid4())}]),
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert isinstance(detail, str)
    assert "label" in detail.lower()


@pytest.mark.asyncio
async def test_label_longer_than_the_limit_is_still_refused(auth_client):
    resp = await auth_client.post(
        "/api/v1/bench/challenges",
        json=_body([{"label": "x" * 81, "source_kind": "spark", "spark_model": "m"}]),
    )
    assert resp.status_code == 422


# ── SOLL-1: the operator's row order survives ─────────────────────────────


@pytest.mark.asyncio
async def test_create_and_detail_keep_the_row_order(auth_client, monkeypatch):
    monkeypatch.setattr(orchestrator, "reconcile_challenge", AsyncMock())
    resp = await auth_client.post(
        "/api/v1/bench/challenges",
        json=_body([
            {"label": "Zeta · Off", "source_kind": "spark", "spark_model": "z"},
            {"label": "Alpha · Max", "source_kind": "spark", "spark_model": "a"},
        ]),
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert [e["model_label"] for e in created["entries"]] == ["Zeta · Off", "Alpha · Max"]
    assert [e["position"] for e in created["entries"]] == [0, 1]

    detail = await auth_client.get(f"/api/v1/bench/challenges/{created['id']}")
    assert [e["model_label"] for e in detail.json()["entries"]] == ["Zeta · Off", "Alpha · Max"]

    listing = await auth_client.get("/api/v1/bench/challenges")
    row = next(c for c in listing.json() if c["id"] == created["id"])
    assert [e["model_label"] for e in row["entries"]] == ["Zeta · Off", "Alpha · Max"]


@pytest.mark.asyncio
async def test_rename_does_not_reorder(auth_client, session, monkeypatch):
    monkeypatch.setattr(orchestrator, "reconcile_challenge", AsyncMock())
    ch = BenchChallenge(title="T", prompt_text="p", status="review")
    session.add(ch)
    await session.commit()
    await session.refresh(ch)
    first = BenchEntry(challenge_id=ch.id, model_label="B", source_kind="spark", position=0)
    second = BenchEntry(challenge_id=ch.id, model_label="C", source_kind="spark", position=1)
    session.add(first)
    session.add(second)
    await session.commit()

    resp = await auth_client.patch(
        f"/api/v1/bench/entries/{second.id}", json={"model_label": "A"}
    )
    assert resp.status_code == 200, resp.text
    detail = await auth_client.get(f"/api/v1/bench/challenges/{ch.id}")
    assert [e["model_label"] for e in detail.json()["entries"]] == ["B", "A"]


def _capture_compose(monkeypatch) -> dict:
    captured: dict = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"output_path": "/sd/out.mp4"}

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json):
            captured.update(json)
            return FakeResp()

    monkeypatch.setattr(orchestrator.httpx, "AsyncClient", FakeClient)
    return captured


@pytest.mark.asyncio
async def test_video_grid_and_outro_follow_the_row_order(session, monkeypatch):
    """Grid inputs, labels, frame models and outro rows: row order, not A-Z."""
    ch = BenchChallenge(title="T", prompt_text="p", mode="side_by_side")
    session.add(ch)
    await session.commit()
    await session.refresh(ch)
    off = BenchEntry(challenge_id=ch.id, model_label="DeepSeek · Off", source_kind="spark",
                     status="rendered", video_path="/sd/off.mp4", position=0)
    mx = BenchEntry(challenge_id=ch.id, model_label="DeepSeek · Max", source_kind="spark",
                    status="rendered", video_path="/sd/max.mp4", position=1)
    session.add(off)
    session.add(mx)
    await session.commit()

    captured = _capture_compose(monkeypatch)
    # Hand them in "wrong" (alphabetical) order on purpose.
    await orchestrator.compose_challenge(session, ch, [mx, off])

    assert captured["inputs"] == ["/sd/off.mp4", "/sd/max.mp4"]
    assert captured["labels"] == ["DeepSeek · Off", "DeepSeek · Max"]
    assert [m["label"] for m in captured["branding"]["models"]] == ["DeepSeek · Off", "DeepSeek · Max"]
    assert [r["name"] for r in captured["branding"]["outro_rows"]] == ["DeepSeek · Off", "DeepSeek · Max"]


@pytest.mark.asyncio
async def test_three_entry_grid_follows_the_row_order(session, monkeypatch):
    ch = BenchChallenge(title="T", prompt_text="p", mode="side_by_side")
    session.add(ch)
    await session.commit()
    await session.refresh(ch)
    entries = [
        BenchEntry(challenge_id=ch.id, model_label=label, source_kind="spark",
                   status="rendered", video_path=f"/sd/{label}.mp4", position=pos)
        for pos, label in enumerate(("C", "A", "B"))
    ]
    for e in entries:
        session.add(e)
    await session.commit()

    captured = _capture_compose(monkeypatch)
    await orchestrator.compose_challenge(session, ch, list(reversed(entries)))
    assert captured["labels"] == ["C", "A", "B"]


def test_order_key_falls_back_to_label_for_equal_positions():
    """Entries created before the column existed were backfilled in label
    order; equal positions (should never happen) still sort stably."""
    cid = uuid.uuid4()
    a = BenchEntry(challenge_id=cid, model_label="b", source_kind="spark", position=0)
    b = BenchEntry(challenge_id=cid, model_label="a", source_kind="spark", position=0)
    assert [e.model_label for e in sorted([a, b], key=orchestrator.entry_order_key)] == ["a", "b"]


# ── NICE-7: the folder stays after a rename ───────────────────────────────


def test_entry_dir_stays_put_after_a_rename():
    cid = uuid.uuid4()
    entry = BenchEntry(id=uuid.uuid4(), challenge_id=cid, model_label="DeepSeek", source_kind="spark")
    first = orchestrator.entry_dir(entry)
    entry.artifact_path = str(first / "index.html")

    entry.model_label = "DeepSeek V4 Flash"
    assert orchestrator.entry_dir(entry) == first


def test_entry_dir_ignores_a_legacy_label_only_folder():
    """Folders from before the id suffix were shared by same-label rows
    (round-1 data loss) — never reuse one of those."""
    cid = uuid.uuid4()
    entry = BenchEntry(id=uuid.uuid4(), challenge_id=cid, model_label="DeepSeek", source_kind="spark")
    entry.artifact_path = str(orchestrator.challenge_dir(cid) / "DeepSeek" / "index.html")
    assert orchestrator.entry_dir(entry).name == f"DeepSeek-{entry.id.hex[:8]}"


def test_entry_dir_ignores_a_folder_outside_the_challenge():
    cid = uuid.uuid4()
    entry = BenchEntry(id=uuid.uuid4(), challenge_id=cid, model_label="X", source_kind="spark")
    entry.artifact_path = f"/tmp/elsewhere/X-{entry.id.hex[:8]}/index.html"
    assert orchestrator.entry_dir(entry).parent == orchestrator.challenge_dir(cid)


# ── Migration 0221 ────────────────────────────────────────────────────────

VERSIONS_DIR = pathlib.Path(__file__).parents[1] / "alembic" / "versions"


def _load_revision():
    path = VERSIONS_DIR / "0221_bench_entry_position.py"
    if not path.is_file():
        pytest.fail(f"Migration 0221 not present at {path}")
    placeholder = types.SimpleNamespace()
    import alembic as _alembic

    _alembic.op = placeholder
    sys.modules["alembic.op"] = placeholder
    spec = importlib.util.spec_from_file_location("mig0221", str(path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RealOp:
    """Binds the two alembic ops the revision uses to a real SQLite
    connection (the backend's own ``alembic/`` folder shadows the alembic
    package on the test path, so ``alembic.operations`` is not importable
    here — same approach as test_migration_0204)."""

    def __init__(self, conn):
        self.conn = conn

    def get_bind(self):
        return self.conn

    def add_column(self, table, column):
        from sqlalchemy.schema import CreateColumn

        ddl = CreateColumn(column).compile(dialect=self.conn.dialect)
        self.conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))

    def execute(self, sql):
        self.conn.execute(text(sql) if isinstance(sql, str) else sql)


def test_revision_wiring():
    module = _load_revision()
    assert module.revision == "0221_bench_entry_position"
    assert module.down_revision == "0220_bench_probes"


@pytest.mark.asyncio
async def test_upgrade_adds_position_and_backfills_label_order(tmp_path):
    """Existing challenges keep the order they show today (A-Z by label,
    compared like Python's ``sorted``: upper case before lower case),
    per challenge, starting at 0."""
    module = _load_revision()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    async with engine.begin() as conn:
        await conn.execute(text(
            "CREATE TABLE bench_entries (id CHAR(32) PRIMARY KEY, challenge_id CHAR(32) NOT NULL, "
            "model_label VARCHAR NOT NULL)"
        ))
        await conn.execute(text(
            "INSERT INTO bench_entries VALUES "
            "('e1','c1','Zeta'),('e2','c1','Alpha'),('e3','c2','Only'),('e4','c1','Mid'),"
            "('e6','c4','alpha'),('e7','c4','Beta')"
        ))

    def _upgrade(sync_conn):
        module.op = _RealOp(sync_conn)
        module.upgrade()

    async with engine.begin() as conn:
        await conn.run_sync(_upgrade)

    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT id, position FROM bench_entries ORDER BY id"
        ))).all()
    assert dict(rows) == {"e1": 2, "e2": 0, "e3": 0, "e4": 1, "e6": 1, "e7": 0}
    assert sorted(["alpha", "Beta"]) == ["Beta", "alpha"]  # the order shown before

    # New rows without an explicit value get the server default.
    async with engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO bench_entries (id, challenge_id, model_label) VALUES ('e5','c3','New')"
        ))
        pos = (await conn.execute(text("SELECT position FROM bench_entries WHERE id='e5'"))).scalar()
    assert pos == 0
    await engine.dispose()
