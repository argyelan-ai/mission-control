"""bench_studio orchestrator — HTML extraction + spark generation path.

Vertical test: skipped entirely when the vertical directory is stripped.
"""
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest

pytest.importorskip("app.verticals.bench_studio")

from app.models.bench import BenchChallenge, BenchEntry
from app.verticals.bench_studio import orchestrator


# ── extract_html ──────────────────────────────────────────────────────────


def test_extract_html_strips_html_fence():
    raw = "Here you go:\n```html\n<!DOCTYPE html><html><body>hi</body></html>\n```\nEnjoy!"
    assert orchestrator.extract_html(raw) == "<!DOCTYPE html><html><body>hi</body></html>"


def test_extract_html_strips_bare_fence():
    raw = "```\n<html><body>x</body></html>\n```"
    assert orchestrator.extract_html(raw) == "<html><body>x</body></html>"


def test_extract_html_cuts_leading_prose_before_doctype():
    raw = "Sure! Here is the page.\n<!doctype html>\n<html></html>"
    assert orchestrator.extract_html(raw).startswith("<!doctype html>")


def test_extract_html_passthrough_plain_document():
    raw = "<html><head></head><body>ok</body></html>"
    assert orchestrator.extract_html(raw) == raw


def test_extract_html_empty_input():
    assert orchestrator.extract_html("") == ""
    assert orchestrator.extract_html(None) == ""


def test_extract_html_fence_with_leading_prose_inside():
    """Prose inside a fence (before <!DOCTYPE) must also be trimmed (Task 4 fix)."""
    raw = "```html\nsome prose\n<!DOCTYPE html><html></html>\n```"
    result = orchestrator.extract_html(raw)
    assert result.startswith("<!DOCTYPE html")


# ── generate_spark_entry ──────────────────────────────────────────────────


async def _make_challenge_entry(session, **entry_kwargs):
    ch = BenchChallenge(title="T", prompt_text="make a page")
    session.add(ch)
    await session.commit()
    await session.refresh(ch)
    entry = BenchEntry(
        challenge_id=ch.id,
        model_label="DeepSeek",
        source_kind="spark",
        **entry_kwargs,
    )
    session.add(entry)
    await session.commit()
    await session.refresh(entry)
    return ch, entry


@pytest.mark.asyncio
async def test_generate_spark_entry_writes_artifact_and_metrics(
    session, tmp_path, monkeypatch
):
    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)
    ch, entry = await _make_challenge_entry(session, spark_model="deepseek-x")

    monkeypatch.setattr(
        orchestrator,
        "_spark_generate",
        AsyncMock(
            return_value=(
                "```html\n<html><body>ball</body></html>\n```",
                {"duration_ms": 1234, "tokens_in": 40, "tokens_out": 900, "tok_per_s": 72.9},
            )
        ),
    )

    await orchestrator.generate_spark_entry(session, entry, ch.prompt_text)
    await session.refresh(entry)

    assert entry.status == "generated"
    assert entry.error is None
    assert entry.artifact_path is not None
    expected = tmp_path / f"bench-{ch.id}" / "DeepSeek" / "index.html"
    assert str(expected) == entry.artifact_path
    assert expected.read_text() == "<html><body>ball</body></html>"
    assert entry.metrics["duration_ms"] == 1234
    assert entry.metrics["tok_per_s"] == 72.9
    # The model override must reach the spark call:
    orchestrator._spark_generate.assert_awaited_once_with("make a page", "deepseek-x")


@pytest.mark.asyncio
async def test_generate_spark_entry_failure_sets_failed_with_error(
    session, tmp_path, monkeypatch
):
    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)
    ch, entry = await _make_challenge_entry(session)

    monkeypatch.setattr(
        orchestrator,
        "_spark_generate",
        AsyncMock(side_effect=RuntimeError("vLLM timeout")),
    )

    await orchestrator.generate_spark_entry(session, entry, ch.prompt_text)
    await session.refresh(entry)

    assert entry.status == "failed"
    assert "vLLM timeout" in entry.error


@pytest.mark.asyncio
async def test_generate_spark_entry_empty_html_fails(session, tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)
    ch, entry = await _make_challenge_entry(session)
    monkeypatch.setattr(
        orchestrator, "_spark_generate", AsyncMock(return_value=("   ", {"duration_ms": 5}))
    )

    await orchestrator.generate_spark_entry(session, entry, ch.prompt_text)
    await session.refresh(entry)

    assert entry.status == "failed"
    assert "no HTML" in entry.error


# ── spark_models_status / resolve_spark_model_or_422 (Bench #21 vanilla) ──


class _FakeModelsResponse:
    def __init__(self, models: list[str]):
        self.status_code = 200
        self._models = models

    def raise_for_status(self):
        pass

    def json(self):
        return {"data": [{"id": m} for m in self._models]}


def _patch_models_probe(monkeypatch, *, reachable: bool, models=None, active=None):
    """Patches the two things spark_models_status touches: the raw GET
    /v1/models probe (via httpx.AsyncClient, module-global — restored by
    monkeypatch after the test) and SparkClient._resolve_llm_model (DB-backed,
    out of scope for this unit test)."""
    from app.services.spark_client import SparkClient

    class _FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            if not reachable:
                raise httpx.ConnectError("no route to host", request=httpx.Request("GET", url))
            return _FakeModelsResponse(models or [])

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(SparkClient, "_resolve_llm_model", AsyncMock(return_value=active))


@pytest.mark.asyncio
async def test_spark_models_status_reachable(monkeypatch):
    _patch_models_probe(monkeypatch, reachable=True, models=["a", "b"], active="a")
    assert await orchestrator.spark_models_status() == {
        "reachable": True, "models": ["a", "b"], "active": "a",
    }


@pytest.mark.asyncio
async def test_spark_models_status_unreachable(monkeypatch):
    _patch_models_probe(monkeypatch, reachable=False)
    assert await orchestrator.spark_models_status() == {
        "reachable": False, "models": [], "active": None,
    }


@pytest.mark.asyncio
async def test_resolve_spark_model_or_422_happy_path(monkeypatch):
    _patch_models_probe(monkeypatch, reachable=True, models=["a"], active="a")
    assert await orchestrator.resolve_spark_model_or_422() == "a"


@pytest.mark.asyncio
async def test_resolve_spark_model_or_422_unreachable_raises_422(monkeypatch):
    from fastapi import HTTPException

    _patch_models_probe(monkeypatch, reachable=False)
    with pytest.raises(HTTPException) as exc_info:
        await orchestrator.resolve_spark_model_or_422()
    assert exc_info.value.status_code == 422
