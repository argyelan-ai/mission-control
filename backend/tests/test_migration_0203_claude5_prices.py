"""Migration 0203: Claude 5 model ids get a real (non-zero) price.

Before 0203, `claude-opus-5`, `claude-opus-5-5` and `claude-sonnet-5` only
matched the `*` fallback (priority 0, all prices 0) and were billed at 0 USD.

Same shim pattern as test_migration_0162: load the migration as a plain
module with a stubbed alembic.op, capture the rows handed to bulk_insert,
turn them into ModelPrice objects and run them through the REAL matcher
(token_harvester.match_price) together with the price rows that already
exist before 0203 (0127 seed + 0128 fixes).
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
import uuid
from datetime import datetime, timezone

import pytest

from app.models.model_usage import ModelPrice
from app.services.token_harvester import _compute_cost_usd, match_price

REVISION_PATH = (
    pathlib.Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "0203_claude5_model_prices.py"
)

_EPOCH = datetime(2020, 1, 1, tzinfo=timezone.utc)
_TS = datetime(2026, 9, 22, tzinfo=timezone.utc)


def _load_migration():
    calls: dict[str, list] = {"bulk_insert": [], "execute": []}
    op_shim = types.SimpleNamespace(
        bulk_insert=lambda table, rows, **k: calls["bulk_insert"].append((table, rows)),
        execute=lambda sql, *a, **k: calls["execute"].append(str(sql)),
    )
    import alembic as _alembic
    _alembic.op = op_shim
    sys.modules["alembic.op"] = op_shim

    spec = importlib.util.spec_from_file_location("mig0203", str(REVISION_PATH))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, calls


def _row(pattern, inp, out, cr, cw, prio, valid_from=_EPOCH):
    return ModelPrice(
        id=uuid.uuid4(),
        model_pattern=pattern,
        input_per_mtok=inp,
        output_per_mtok=out,
        cache_read_per_mtok=cr,
        cache_write_per_mtok=cw,
        priority=prio,
        valid_from=valid_from,
        currency="USD",
    )


def _baseline() -> list[ModelPrice]:
    """Price rows present before 0203 (0127 seed after the 0128 fixes)."""
    return [
        _row("claude-haiku-4-5-*", 1.0, 5.0, 0.1, 1.25, 85),
        _row("claude-sonnet-4-*", 3.0, 15.0, 0.3, 3.75, 80),
        _row("claude-opus-4-*", 5.0, 25.0, 0.5, 6.25, 80),
        _row("claude-fable-*", 10.0, 50.0, 1.0, 12.5, 75),
        _row("glm-*", 0.0, 0.0, 0.0, 0.0, 60),
        _row("qwen2.5-coder*", 0.0, 0.0, 0.0, 0.0, 60),
        _row("*PrismaQuant*", 0.0, 0.0, 0.0, 0.0, 60),
        _row("*Qwen*", 0.0, 0.0, 0.0, 0.0, 50),
        _row("qwen*", 0.0, 0.0, 0.0, 0.0, 50),
        _row("*", 0.0, 0.0, 0.0, 0.0, 0),
    ]


def _migrated_rows() -> list[ModelPrice]:
    module, calls = _load_migration()
    module.upgrade()
    assert len(calls["bulk_insert"]) == 1
    table, rows = calls["bulk_insert"][0]
    assert table.name == "model_prices"
    return [
        _row(
            r["model_pattern"],
            r["input_per_mtok"],
            r["output_per_mtok"],
            r["cache_read_per_mtok"],
            r["cache_write_per_mtok"],
            r["priority"],
            r["valid_from"],
        )
        for r in rows
    ]


def _price(model: str, prices: list[ModelPrice]) -> tuple[float, float, float, float]:
    info = match_price(model, _TS, prices)
    assert info is not None
    return (
        info["input_per_mtok"],
        info["output_per_mtok"],
        info["cache_read_per_mtok"],
        info["cache_write_per_mtok"],
    )


def test_revision_chain():
    module, _ = _load_migration()
    assert module.revision == "0203_claude5_model_prices"
    assert module.down_revision == "0202_task_event_actor"


@pytest.mark.parametrize("model", ["claude-opus-5", "claude-opus-5-5", "claude-sonnet-5"])
def test_before_migration_claude5_is_billed_zero(model):
    """Documents the bug: without 0203 these ids only hit the `*` fallback."""
    assert _price(model, _baseline()) == (0.0, 0.0, 0.0, 0.0)


@pytest.mark.parametrize(
    "model, expected",
    [
        ("claude-opus-5-5", (4.0, 20.0, 0.20, 5.0)),
        ("claude-opus-5", (5.0, 25.0, 0.50, 6.25)),
        ("claude-sonnet-5", (2.0, 10.0, 0.20, 2.5)),
        ("claude-fable-5-1", (10.0, 50.0, 0.25, 12.5)),
        # Unchanged neighbours: Fable 5 keeps its row, 4.x ids stay on theirs.
        ("claude-fable-5", (10.0, 50.0, 1.0, 12.5)),
        ("claude-opus-4-8", (5.0, 25.0, 0.5, 6.25)),
        ("claude-sonnet-4-6", (3.0, 15.0, 0.3, 3.75)),
        ("claude-haiku-4-5-20251001", (1.0, 5.0, 0.1, 1.25)),
        # Local models stay free.
        ("glm-5.1", (0.0, 0.0, 0.0, 0.0)),
    ],
)
def test_after_migration_prices(model, expected):
    prices = _baseline() + _migrated_rows()
    assert _price(model, prices) == expected


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-sonnet-5", "claude-opus-5"])
def test_after_migration_event_cost_is_positive(model):
    """End-to-end on the cost formula: a real event costs more than 0 USD."""
    prices = _baseline() + _migrated_rows()
    info = match_price(model, _TS, prices)
    cost = _compute_cost_usd(info, 10_000, 2_000, 50_000, 5_000)
    assert cost > 0


def test_downgrade_removes_exactly_the_new_rows():
    module, calls = _load_migration()
    module.downgrade()
    assert len(calls["execute"]) == 1
    sql = calls["execute"][0]
    for pattern in ("claude-opus-5-5*", "claude-opus-5*", "claude-sonnet-5*", "claude-fable-5-1*"):
        assert f"'{pattern}'" in sql
    assert "'claude-fable-*'" not in sql
