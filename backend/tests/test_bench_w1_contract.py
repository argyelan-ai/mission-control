"""Bench W1 contract (24.10.2026): schemas, fixtures, route stubs, schema PR.

- Every fixture JSON is exactly what its pydantic model dumps (a schema
  change without rebuilding the fixtures turns this red).
- Every W1 route exists, answers with its contract model and status code.
- The W1 columns + bench_receipts exist on the models, and migration 0224
  backfills old rows as v0 with the W0 metrics moved into columns.
- The frontend mirror (fixtures.ts) is generated from the same JSON —
  compared by frontend-v2/src/verticals/bench_studio/w1/__tests__/.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import uuid

import pytest
import sqlalchemy as sa

pytest.importorskip("app.verticals.bench_studio")

from app.models.bench import BenchChallenge, BenchEntry, BenchReceipt  # noqa: E402
from app.verticals.bench_studio import w1_schemas as s  # noqa: E402
from app.verticals.bench_studio.w1_fixtures import build  # noqa: E402

FIXTURE_DIR = pathlib.Path(build.__file__).resolve().parent
MIGRATION = (
    pathlib.Path(__file__).parents[1] / "alembic" / "versions" / "0224_bench_w1_schema.py"
)


@pytest.mark.parametrize("name", sorted(build.FIXTURES))
def test_fixture_json_is_the_schema_dump(name):
    builder, _ = build.FIXTURES[name]
    on_disk = json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))
    assert on_disk == build.dump(builder()), (
        f"{name}.json is stale — run `python -m app.verticals.bench_studio.w1_fixtures.build`"
    )


def test_every_fixture_file_is_registered():
    files = {p.stem for p in FIXTURE_DIR.glob("*.json")}
    assert files == set(build.FIXTURES)


def test_fixture_rules_hold():
    """The concept rules the fixtures must show (so the UI is built on them)."""
    green = build.test_done_green()
    a = green.blocks[0].contestants[0]
    # R2: the name is the served id verbatim.
    assert a.display_name == a.served_model_id == green.verdict.served_model_id
    # Decode ≥ wall average (6 607 tok in 65.8 s = 100.4 tok/s) — critic 32 N3.
    assert a.summary.decode_tok_s.median >= 100.4
    # Local runs never carry spent money (R3) — a comparison price only.
    assert a.summary.cost.cost_kind == "local" and a.summary.cost.usd is None
    # Class B has no tempo row.
    red = build.test_needs_look_red()
    b = red.blocks[0].contestants[0]
    assert red.blocks[0].run_class == "B"
    assert b.summary.decode_tok_s is None and b.summary.ttft_ms is None
    # Pre-W1 rows are v0 with unknowns spelled out (critic 32 N2).
    assert red.protocol_version == "v0" and b.receipt.protocol_version == "v0"
    assert "time_limit_min" in b.receipt.unknown
    # Truncation is a private warning with a re-run offer, not a gate red.
    trunc = build.test_done_truncated()
    assert trunc.truncation.suggested_max_tokens > trunc.truncation.max_tokens
    assert trunc.gate.status != "red"
    # A paid run needs confirmation, over $1 twice (D4).
    paid = build.contestants().cloud[1].money
    assert paid.cost_kind == "api_price" and paid.needs_confirmation and paid.second_confirmation


# ── Route stubs ───────────────────────────────────────────────────────────

GREEN = "a0000000-0000-4000-8000-000000000002"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path,body,code,model",
    [
        ("get", "/api/v1/bench/plan", None, 200, s.BenchPlan),
        ("get", "/api/v1/bench/contestants", None, 200, s.ContestantList),
        ("post", "/api/v1/bench/tests",
         {"picks": [{"runtime_id": str(uuid.uuid4())}], "trigger": "runtimes"}, 201,
         s.BenchTestDetail),
        ("get", "/api/v1/bench/tests", None, 200, s.BenchTestList),
        ("get", f"/api/v1/bench/tests/{GREEN}", None, 200, s.BenchTestDetail),
        ("post", f"/api/v1/bench/tests/{GREEN}/rerun", {"budget": "larger"}, 201,
         s.BenchTestDetail),
        ("post", f"/api/v1/bench/tests/{GREEN}/stop", None, 200, s.BenchTestDetail),
        ("post", f"/api/v1/bench/tests/{GREEN}/harvest", None, 200, s.BenchTestDetail),
        ("get", f"/api/v1/bench/tests/{GREEN}/kit", None, 200, s.XKit),
        ("post", f"/api/v1/bench/tests/{GREEN}/kit/download-token", None, 200, s.DownloadToken),
        ("get", "/api/v1/bench/runtime-summary", None, 200, None),
        ("post", "/api/v1/bench/auto-tests",
         {"host_id": str(uuid.uuid4()), "recipe_slug": "glm53-flash"}, 201, s.AutoTestResponse),
    ],
)
async def test_w1_route_answers_with_its_contract_model(auth_client, method, path, body, code, model):
    call = getattr(auth_client, method)
    resp = await (call(path, json=body) if body is not None else call(path))
    assert resp.status_code == code, resp.text
    if model is not None:
        model.model_validate(resp.json())
    else:
        [s.RuntimeTestSummary.model_validate(r) for r in resp.json()]


@pytest.mark.asyncio
async def test_unknown_test_is_404(auth_client):
    resp = await auth_client.get(f"/api/v1/bench/tests/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_test_validates_the_request(auth_client):
    # No picks → 422; more than two contestants per video → 422.
    assert (await auth_client.post("/api/v1/bench/tests", json={"picks": []})).status_code == 422
    picks = [{"runtime_id": str(uuid.uuid4())} for _ in range(3)]
    assert (await auth_client.post("/api/v1/bench/tests", json={"picks": picks})).status_code == 422


@pytest.mark.asyncio
async def test_w1_routes_need_a_session(client):
    assert (await client.get("/api/v1/bench/plan")).status_code in (401, 403)


# ── Schema PR (models + migration 0224) ───────────────────────────────────


def test_models_carry_the_w1_columns():
    entry_cols = set(BenchEntry.__table__.columns.keys())
    assert {
        "runtime_id", "harness", "run_class", "effort_requested", "repeat_idx",
        "attempt_no", "head_run_id", "receipt", "gate",
    } <= entry_cols
    chal_cols = set(BenchChallenge.__table__.columns.keys())
    assert {
        "preset", "prompt_sha256", "protocol_version", "episode_no", "trigger",
        "rerun_of", "plan", "wait_reason",
    } <= chal_cols
    assert BenchReceipt.__tablename__ == "bench_receipts"
    # A new challenge is v0 unless the W1 create path says v1.
    assert BenchChallenge(title="t", prompt_text="p").protocol_version == "v0"


class _OpShim:
    """Just enough of ``alembic.op`` to run 0224 against a real SQLite
    connection (``backend/alembic`` shadows the alembic package inside the
    test run — same reason test_migration_0162 uses a shim)."""

    def __init__(self, conn=None):
        self.conn = conn

    def get_bind(self):
        return self.conn

    def execute(self, stmt):
        return self.conn.execute(sa.text(stmt) if isinstance(stmt, str) else stmt)

    def add_column(self, table, column):
        ddl = sa.schema.CreateColumn(column).compile(dialect=self.conn.dialect)
        self.conn.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))

    def create_index(self, name, table, cols, **_):
        self.conn.execute(sa.text(f"CREATE INDEX {name} ON {table} ({', '.join(cols)})"))

    def create_table(self, name, *items, **_):
        meta = sa.MetaData()
        meta.reflect(self.conn)
        sa.Table(name, meta, *items).create(self.conn)


def _load_migration(conn=None):
    import alembic as _alembic

    _alembic.op = _OpShim(conn)
    spec = importlib.util.spec_from_file_location("mig0224", str(MIGRATION))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_chain_metadata():
    module = _load_migration()
    assert module.revision == "0224_bench_w1_schema"
    assert module.down_revision == "0223_message_from_chat"
    assert len(module.revision) <= 32


def test_migration_backfills_old_rows_as_v0():
    """Run upgrade() for real (SQLite) on a pre-W1 schema with two rows."""
    engine = sa.create_engine("sqlite://")
    meta = sa.MetaData()
    sa.Table("runtimes", meta, sa.Column("id", sa.Uuid(), primary_key=True))
    sa.Table(
        "bench_challenges", meta,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("prompt_text", sa.Text()),
    )
    sa.Table(
        "bench_entries", meta,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("source_kind", sa.String()),
        sa.Column("metrics", sa.JSON()),
    )
    meta.create_all(engine)
    cid, e_api, e_agent = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(meta.tables["bench_challenges"].insert(), [{"id": cid, "prompt_text": "abc"}])
        conn.execute(meta.tables["bench_entries"].insert(), [
            {"id": e_api, "source_kind": "spark", "metrics": {"effort_requested": "max"}},
            {"id": e_agent, "source_kind": "agent", "metrics": {}},
        ])
        _load_migration(conn).upgrade()

    with engine.connect() as conn:
        chal = conn.execute(sa.text(
            "SELECT protocol_version, prompt_sha256 FROM bench_challenges")).one()
        rows = {
            r.id: r for r in conn.execute(sa.text(
                "SELECT id, harness, run_class, effort_requested, repeat_idx, attempt_no "
                "FROM bench_entries"))
        }
        tables = sa.inspect(conn).get_table_names()
    import hashlib

    assert chal.protocol_version == "v0"
    assert chal.prompt_sha256 == hashlib.sha256(b"abc").hexdigest()
    api = rows[e_api.hex] if e_api.hex in rows else rows[str(e_api)]
    agent = rows[e_agent.hex] if e_agent.hex in rows else rows[str(e_agent)]
    assert (api.harness, api.run_class, api.effort_requested) == ("api", "A", "max")
    assert (agent.harness, agent.run_class, agent.effort_requested) == (None, "B", None)
    assert api.repeat_idx == 1 and api.attempt_no == 1
    assert "bench_receipts" in tables
