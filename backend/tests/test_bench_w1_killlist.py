"""Bench W1 kill list (W1.8): no route posts to X from the bench, no legacy
create / 3-6 grid / Spark-only model list; the legacy result buttons hand W1
tests to the W1 runner."""
from __future__ import annotations

import pathlib
import uuid

import pytest

pytest.importorskip("app.verticals.bench_studio")

from app.models.bench import BenchChallenge, BenchEntry  # noqa: E402
from app.verticals.bench_studio import orchestrator, routers  # noqa: E402

VERTICAL = pathlib.Path(orchestrator.__file__).resolve().parent


def test_no_x_path_left_in_the_vertical():
    for path in VERTICAL.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for needle in ("x_publisher", "x_post_resolved_hooks", "create_draft", "grid-speeds",
                       "spark_models_status", "speed_labels"):
            if needle in text and path.name != "routers.py":
                pytest.fail(f"{needle} still in {path.name}")
    assert not (VERTICAL / "drafts.py").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [
    ("post", "/api/v1/bench/challenges"),
    ("get", "/api/v1/bench/spark-models"),
    ("get", "/api/v1/bench/effort-levels"),
    ("post", "/api/v1/bench/effort-levels/check"),
    ("post", f"/api/v1/bench/challenges/{uuid.uuid4()}/draft"),
])
async def test_removed_routes_are_gone(auth_client, method, path):
    resp = await getattr(auth_client, method)(path, json={}) if method == "post" else await auth_client.get(path)
    assert resp.status_code in (404, 405), resp.text


@pytest.mark.asyncio
async def test_compose_refuses_more_than_two_contestants(session):
    ch = BenchChallenge(title="t", prompt_text="p", status="review")
    session.add(ch)
    await session.flush()
    rows = [BenchEntry(challenge_id=ch.id, model_label=f"m{i}", source_kind="spark", video_path=f"/x/{i}.mp4")
            for i in range(3)]
    with pytest.raises(ValueError):
        await orchestrator.compose_challenge(session, ch, rows)


async def _v1(session, status="review"):
    ch = BenchChallenge(title="t", prompt_text="p", status=status, protocol_version="v1",
                        plan={"contestants": []})
    session.add(ch)
    await session.flush()
    e = BenchEntry(challenge_id=ch.id, model_label="m", source_kind="api", status="failed",
                   video_path="/x/1.mp4", artifact_path="/x/index.html", run_class="A")
    session.add(e)
    await session.commit()
    return ch, e


@pytest.mark.asyncio
async def test_legacy_rerender_and_recompose_hand_v1_to_the_w1_runner(auth_client, session, monkeypatch):
    ch, _e = await _v1(session)
    started = []
    monkeypatch.setattr(routers, "create_tracked_task", lambda coro, name=None: (started.append(name), coro.close()))
    assert (await auth_client.post(f"/api/v1/bench/challenges/{ch.id}/rerender")).status_code == 200
    assert started[-1] == f"bench_w1_rerender({ch.id})"
    from app.redis_client import RedisKeys, get_redis

    await (await get_redis()).delete(RedisKeys.bench_challenge_run_claim(str(ch.id)))
    assert (await auth_client.post(f"/api/v1/bench/challenges/{ch.id}/recompose")).status_code == 200
    assert started[-1] == f"bench_w1_recompose({ch.id})"


@pytest.mark.asyncio
async def test_legacy_entry_retry_and_rerender_refuse_v1(auth_client, session):
    _ch, e = await _v1(session)
    assert (await auth_client.post(f"/api/v1/bench/entries/{e.id}/retry")).status_code == 409
    assert (await auth_client.post(f"/api/v1/bench/entries/{e.id}/rerender")).status_code == 409
