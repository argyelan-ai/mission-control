"""Bench Studio models (core per ADR-044 §3 — schema identical across variants).

Core-level test: runs in stripped installations too (models are never stripped).
"""
import pytest

from app.models.bench import BenchChallenge, BenchEntry


@pytest.mark.asyncio
async def test_bench_challenge_defaults_and_roundtrip(session):
    ch = BenchChallenge(
        title="Bouncing balls",
        prompt_text="Write a single index.html with 100 bouncing balls.",
    )
    session.add(ch)
    await session.commit()
    await session.refresh(ch)

    assert ch.id is not None
    assert ch.status == "generating"
    assert ch.mode == "side_by_side"
    assert ch.series_label is None
    assert ch.composed_video_path is None
    assert ch.content_pipeline_id is None
    assert ch.error is None


@pytest.mark.asyncio
async def test_bench_entry_defaults_and_roundtrip(session):
    ch = BenchChallenge(title="T", prompt_text="p")
    session.add(ch)
    await session.commit()
    await session.refresh(ch)

    entry = BenchEntry(
        challenge_id=ch.id,
        model_label="DeepSeek-V4-Flash",
        source_kind="spark",
        spark_model="DeepSeek-V4-Flash-Spark",
    )
    session.add(entry)
    await session.commit()
    await session.refresh(entry)

    assert entry.status == "pending"
    assert entry.metrics == {}
    assert entry.task_id is None
    assert entry.artifact_path is None
