"""bench_studio orchestrator — HTML extraction + spark generation path.

Vertical test: skipped entirely when the vertical directory is stripped.
"""
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlmodel import select

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
async def test_spark_models_status_bounds_the_whole_probe(monkeypatch):
    """Review finding: _resolve_llm_model can fall through to its own
    unbounded live re-probe — a slow/hanging leg anywhere in the body must
    still resolve to "unreachable" within _SPARK_MODELS_STATUS_TOTAL_TIMEOUT_S,
    not hang the dialog indefinitely."""
    import asyncio

    async def _hangs_forever():
        await asyncio.sleep(3600)
        return {"reachable": True, "models": ["a"], "active": "a"}

    monkeypatch.setattr(orchestrator, "_probe_spark_models", _hangs_forever)
    monkeypatch.setattr(orchestrator, "_SPARK_MODELS_STATUS_TOTAL_TIMEOUT_S", 0.05)

    result = await orchestrator.spark_models_status()
    assert result == {"reachable": False, "models": [], "active": None}


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


# ── Task 5: ModelUsageEvent for vanilla (direct-API) generations ──────────


@pytest.mark.asyncio
async def test_generate_spark_entry_records_usage_event_with_cached_tokens(
    session, tmp_path, monkeypatch
):
    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)
    ch, entry = await _make_challenge_entry(session, spark_model="deepseek-x")
    monkeypatch.setattr(
        orchestrator, "_spark_generate",
        AsyncMock(return_value=(
            "<html><body>ok</body></html>",
            {
                "duration_ms": 500,
                "tokens_in": 120,
                "tokens_out": 40,
                "model": "deepseek-x",
                "cache_read_tokens": 20,
            },
        )),
    )

    await orchestrator.generate_spark_entry(session, entry, ch.prompt_text)

    from app.models.model_usage import ModelUsageEvent

    rows = (
        await session.exec(select(ModelUsageEvent).where(ModelUsageEvent.harness == "vanilla"))
    ).all()
    assert len(rows) == 1
    event = rows[0]
    # No fleet Task exists for a spark entry (only dispatch_agent_entry
    # creates one) — task_id stays NULL, see _record_spark_usage_event.
    assert event.task_id is None
    assert event.agent_id is None
    assert event.provider == "vllm"
    assert event.model == "deepseek-x"
    assert event.cache_read_tokens == 20
    assert event.input_tokens == 100  # 120 prompt tokens - 20 cached
    assert event.output_tokens == 40
    assert event.message_uuid.startswith(f"vanilla:{ch.id}:{entry.id}:")


@pytest.mark.asyncio
async def test_generate_spark_entry_records_usage_event_without_cached_tokens(
    session, tmp_path, monkeypatch
):
    """Realistic vLLM usage payload without prompt_tokens_details (most
    servers today) — cache_read_tokens stays 0, input_tokens is the raw
    prompt_tokens count."""
    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)
    ch, entry = await _make_challenge_entry(session, spark_model="deepseek-x")
    monkeypatch.setattr(
        orchestrator, "_spark_generate",
        AsyncMock(return_value=(
            "<html><body>ok</body></html>",
            {"duration_ms": 500, "tokens_in": 80, "tokens_out": 30, "model": "deepseek-x"},
        )),
    )

    await orchestrator.generate_spark_entry(session, entry, ch.prompt_text)

    from app.models.model_usage import ModelUsageEvent

    rows = (
        await session.exec(select(ModelUsageEvent).where(ModelUsageEvent.harness == "vanilla"))
    ).all()
    assert len(rows) == 1
    assert rows[0].cache_read_tokens == 0
    assert rows[0].input_tokens == 80
    assert rows[0].output_tokens == 30


@pytest.mark.asyncio
async def test_generate_spark_entry_usage_events_not_deduped_across_reruns(
    session, tmp_path, monkeypatch
):
    """A rerender/retry is a genuinely new API call — must land as a new
    row (new discriminator), never dedup onto the previous attempt."""
    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)
    ch, entry = await _make_challenge_entry(session, spark_model="deepseek-x")

    async def _mock_generate(prompt, model):
        return (
            "<html><body>ok</body></html>",
            {"duration_ms": 100, "tokens_in": 10, "tokens_out": 5, "model": model},
        )

    monkeypatch.setattr(orchestrator, "_spark_generate", _mock_generate)

    await orchestrator.generate_spark_entry(session, entry, ch.prompt_text)
    await orchestrator.generate_spark_entry(session, entry, ch.prompt_text)  # simulated rerender

    from app.models.model_usage import ModelUsageEvent

    rows = (
        await session.exec(select(ModelUsageEvent).where(ModelUsageEvent.harness == "vanilla"))
    ).all()
    assert len(rows) == 2
    assert rows[0].message_uuid != rows[1].message_uuid
    assert all(r.message_uuid.startswith(f"vanilla:{ch.id}:{entry.id}:") for r in rows)


@pytest.mark.asyncio
async def test_generate_spark_entry_usage_event_failure_does_not_break_generation(
    session, tmp_path, monkeypatch
):
    """A usage-tracking failure (e.g. pricing lookup blows up) must never
    fail the bench run — the entry still generates successfully, and simply
    ends up with no ModelUsageEvent row."""
    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)
    ch, entry = await _make_challenge_entry(session, spark_model="deepseek-x")
    monkeypatch.setattr(
        orchestrator, "_spark_generate",
        AsyncMock(return_value=(
            "<html><body>ok</body></html>",
            {"duration_ms": 100, "tokens_in": 10, "tokens_out": 5, "model": "deepseek-x"},
        )),
    )

    from app.services import token_harvester

    def _boom(*a, **k):
        raise RuntimeError("pricing lookup blew up")

    monkeypatch.setattr(token_harvester, "match_price", _boom)

    await orchestrator.generate_spark_entry(session, entry, ch.prompt_text)
    await session.refresh(entry)

    assert entry.status == "generated"
    assert entry.artifact_path is not None

    from app.models.model_usage import ModelUsageEvent

    rows = (
        await session.exec(select(ModelUsageEvent).where(ModelUsageEvent.harness == "vanilla"))
    ).all()
    assert rows == []


@pytest.mark.asyncio
async def test_generate_spark_entry_no_usage_block_records_no_event(
    session, tmp_path, monkeypatch
):
    """No `usage` in the vLLM response (some backends omit it) -> no
    ModelUsageEvent row, and generation still succeeds normally."""
    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)
    ch, entry = await _make_challenge_entry(session, spark_model="deepseek-x")
    monkeypatch.setattr(
        orchestrator, "_spark_generate",
        AsyncMock(return_value=("<html><body>ok</body></html>", {"duration_ms": 100})),
    )

    await orchestrator.generate_spark_entry(session, entry, ch.prompt_text)
    await session.refresh(entry)

    assert entry.status == "generated"

    from app.models.model_usage import ModelUsageEvent

    rows = (
        await session.exec(select(ModelUsageEvent).where(ModelUsageEvent.harness == "vanilla"))
    ).all()
    assert rows == []


# ── Task 5: outro token source for vanilla entries (no fleet Task) ────────


@pytest.mark.asyncio
async def test_build_branding_payload_reads_tokens_from_entry_metrics_for_spark(session):
    """Spark entries have no task_id, so the outro's token cell must come
    from entry.metrics (captured synchronously by _spark_generate) rather
    than the task_token_usage/model_usage_events sum agent entries use."""
    ch, entry = await _make_challenge_entry(
        session,
        status="rendered",
        video_path="/tmp/x.mp4",
        artifact_path="/tmp/x/index.html",
        metrics={"duration_ms": 1000, "tokens_in": 200, "tokens_out": 50},
    )

    payload = await orchestrator._build_branding_payload(session, ch, [entry])

    assert payload["outro_rows"][0]["tokens"] == "200 → 50"
