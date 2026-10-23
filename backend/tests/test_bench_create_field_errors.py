"""Create-challenge refusals name their field (round-5 r1 N2, round-6 L4).

The router's own 422s used to be one English sentence (``detail: str``).
The dialog cannot place a sentence at a field, so it reached the operator
as an English toast, also in the German UI. They now come in FastAPI's own
validation shape — ``[{loc, msg, type, ctx}]`` — with a stable ``type``
the dialog turns into a message in the UI language, at the row's field.
``msg`` keeps the English reason for any other reader (logs, curl).
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.models.bench import BenchProbe
from app.services import reasoning_params as rp
from app.verticals.bench_studio import effort_probe, orchestrator


@pytest.fixture
def _no_background(monkeypatch):
    monkeypatch.setattr(orchestrator, "start_challenge", AsyncMock())


def _probe(levels):
    return BenchProbe(
        served_model_id="deepseek-x", status="ok", thinking_key=rp.REASONING_EFFORT,
        effort_param=rp.REASONING_EFFORT, levels=levels, default_observed="on",
    )


def _spark(effort=None, label="DeepSeek", model="deepseek-x"):
    spec = {"label": label, "source_kind": "spark", "spark_model": model}
    if effort is not None:
        spec["effort"] = effort
    return spec


def _agent(label="", effort=None):
    spec = {"label": label, "source_kind": "agent", "agent_id": str(uuid.uuid4())}
    if effort is not None:
        spec["effort"] = effort
    return spec


async def _create(client, *models):
    return await client.post(
        "/api/v1/bench/challenges",
        json={"title": "Field errors", "prompt_text": "one page", "models": list(models)},
    )


def _only_item(resp):
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert isinstance(detail, list) and len(detail) == 1, detail
    item = detail[0]
    assert item["msg"].strip(), "the English reason stays for other readers"
    return item


@pytest.mark.asyncio
async def test_duplicate_entry_names_the_later_rows_label(auth_client, monkeypatch, _no_background):
    monkeypatch.setattr(effort_probe, "get_or_probe", AsyncMock(return_value=_probe(["max"])))
    resp = await _create(auth_client, _spark("max"), _spark(), _spark("max", label=" deepseek "))
    item = _only_item(resp)
    assert item["loc"] == ["body", "models", 2, "label"]
    assert item["type"] == "bench_duplicate_entry"
    assert item["ctx"] == {"row": 3, "other_row": 1}


@pytest.mark.asyncio
async def test_agent_row_without_label_names_its_label(auth_client, _no_background):
    resp = await _create(auth_client, _spark(), _agent(label="  "))
    item = _only_item(resp)
    assert item["loc"] == ["body", "models", 1, "label"]
    assert item["type"] == "bench_agent_label_required"


@pytest.mark.asyncio
async def test_effort_the_model_does_not_offer_names_the_rows_effort(auth_client, monkeypatch, _no_background):
    monkeypatch.setattr(effort_probe, "get_or_probe", AsyncMock(return_value=_probe(["off"])))
    resp = await _create(auth_client, _spark(), _spark("max", label="Other"))
    item = _only_item(resp)
    assert item["loc"] == ["body", "models", 1, "effort"]
    assert item["type"] == "bench_effort_not_offered"
    assert item["ctx"]["level"] == "max"
    assert "not available" in item["msg"]


@pytest.mark.asyncio
async def test_effort_on_an_agent_row_names_the_rows_effort(auth_client, _no_background):
    resp = await _create(auth_client, _agent(label="Worker", effort="high"))
    item = _only_item(resp)
    assert item["loc"] == ["body", "models", 0, "effort"]
    assert item["type"] == "bench_effort_agent_path"


@pytest.mark.asyncio
async def test_spark_unreachable_names_the_auto_rows_model(auth_client, monkeypatch, _no_background):
    monkeypatch.setattr(
        orchestrator, "resolve_spark_model_or_422",
        AsyncMock(side_effect=HTTPException(422, "Spark is not reachable, so the active model cannot be resolved right now.")),
    )
    resp = await _create(auth_client, _agent(label="Worker"), _spark(model=None))
    item = _only_item(resp)
    assert item["loc"] == ["body", "models", 1, "spark_model"]
    assert item["type"] == "bench_spark_unreachable"
    assert item["msg"].startswith("Spark is not reachable")
