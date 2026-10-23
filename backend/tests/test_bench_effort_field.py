"""Effort is its own field, not part of the label (round-2 Q2, operator
decision 23.10.2026: "benchvideo englisch ja").

- Create stores the label as typed (or the auto model name) — the effort
  stays in ``metrics.effort_requested``. Two rows of the same model with
  different efforts are not duplicates; same model AND same effort is
  refused (the box answers deterministically, so the second row would only
  repeat the first one).
- A rename may reuse a sibling's label when the effort tells them apart.
- The video gets the effort as its own value (frame chip + outro row), in
  English ("Off", "Max", "Default"), so the renderer can show it as a pill
  next to the model name instead of a tag that wraps. A renderer that does
  not announce ``branding_effort`` keeps getting the old text form.
- Durations in the outro: "1 min 35 s", never "1.6 min".

Vertical test: skipped entirely when the vertical directory is stripped.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

pytest.importorskip("app.verticals.bench_studio")

from app.models.bench import BenchChallenge, BenchEntry, BenchProbe
from app.services import reasoning_params as rp
from app.verticals.bench_studio import effort_probe, orchestrator

LABEL = "DeepSeek-V4.1-Flash-1017"


def _probe_row(levels):
    return BenchProbe(
        served_model_id="deepseek-x", status="ok", thinking_key=rp.REASONING_EFFORT,
        effort_param=rp.REASONING_EFFORT, levels=levels, default_observed="on",
    )


@pytest.fixture
def _no_background(monkeypatch):
    monkeypatch.setattr(orchestrator, "start_challenge", AsyncMock())


@pytest.fixture
def _probe(monkeypatch):
    monkeypatch.setattr(
        effort_probe, "get_or_probe", AsyncMock(return_value=_probe_row(["off", "high", "max"]))
    )


def _spec(effort=None, label=LABEL, model="deepseek-x"):
    spec = {"label": label, "source_kind": "spark", "spark_model": model}
    if effort is not None:
        spec["effort"] = effort
    return spec


async def _challenge(session, *efforts: str, label: str = LABEL, kind: str = "spark"):
    ch = BenchChallenge(title="Pagoda", prompt_text="make a page")
    session.add(ch)
    await session.commit()
    await session.refresh(ch)
    entries = []
    for pos, effort in enumerate(efforts):
        e = BenchEntry(
            challenge_id=ch.id, model_label=label, source_kind=kind,
            spark_model="deepseek-x" if kind == "spark" else None,
            metrics={"effort_requested": effort}, position=pos,
        )
        session.add(e)
        entries.append(e)
    await session.commit()
    for e in entries:
        await session.refresh(e)
    return ch, entries


# ── create: the label stays the label ──────────────────────────────────────


@pytest.mark.asyncio
async def test_create_keeps_the_typed_label_and_stores_effort_apart(
    auth_client, _probe, _no_background
):
    resp = await auth_client.post("/api/v1/bench/challenges", json={
        "title": "Off vs Max", "prompt_text": "one page",
        "models": [_spec("off"), _spec("max")],
    })
    assert resp.status_code == 201, resp.text
    entries = resp.json()["entries"]
    assert [e["model_label"] for e in entries] == [LABEL, LABEL]
    assert [e["metrics"]["effort_requested"] for e in entries] == ["off", "max"]
    assert [e["position"] for e in entries] == [0, 1]


@pytest.mark.asyncio
async def test_two_auto_rows_get_the_plain_model_name(auth_client, monkeypatch, _probe, _no_background):
    monkeypatch.setattr(orchestrator, "resolve_spark_model_or_422", AsyncMock(return_value="deepseek-x"))
    resp = await auth_client.post("/api/v1/bench/challenges", json={
        "title": "Auto", "prompt_text": "one page",
        "models": [
            {"label": "", "source_kind": "spark", "effort": "off"},
            {"label": "", "source_kind": "spark"},
        ],
    })
    assert resp.status_code == 201, resp.text
    assert [e["model_label"] for e in resp.json()["entries"]] == ["deepseek-x", "deepseek-x"]


@pytest.mark.asyncio
async def test_same_label_and_same_effort_is_refused_with_a_reason(
    auth_client, _probe, _no_background
):
    resp = await auth_client.post("/api/v1/bench/challenges", json={
        "title": "Twice", "prompt_text": "one page",
        "models": [_spec("max"), _spec("max", label=f" {LABEL.lower()} ")],
    })
    assert resp.status_code == 422
    detail = resp.json()["detail"].lower()
    assert "effort" in detail and "label" in detail


@pytest.mark.asyncio
async def test_long_labels_are_not_cut_any_more(auth_client, monkeypatch, _no_background):
    monkeypatch.setattr(effort_probe, "get_or_probe", AsyncMock(return_value=_probe_row(["off", "medium"])))
    long_label = "L" * 80
    resp = await auth_client.post("/api/v1/bench/challenges", json={
        "title": "Long", "prompt_text": "one page",
        "models": [_spec("off", label=long_label), _spec("medium", label=long_label)],
    })
    assert resp.status_code == 201, resp.text
    assert [e["model_label"] for e in resp.json()["entries"]] == [long_label, long_label]


# ── rename ─────────────────────────────────────────────────────────────────


async def _finished(session, *efforts):
    ch, entries = await _challenge(session, *efforts)
    for e, name in zip(entries, ["Alpha", "Beta"]):
        e.model_label = name
        e.status = "rendered"
        session.add(e)
    ch.status = "review"
    session.add(ch)
    await session.commit()
    return ch, entries


@pytest.mark.asyncio
async def test_rename_to_a_sibling_label_with_other_effort_is_allowed(auth_client, session):
    _, (_, b) = await _finished(session, "off", "max")
    resp = await auth_client.patch(f"/api/v1/bench/entries/{b.id}", json={"model_label": "alpha"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["model_label"] == "alpha"


@pytest.mark.asyncio
async def test_rename_to_a_sibling_label_with_same_effort_is_refused(auth_client, session):
    _, (_, b) = await _finished(session, "max", "max")
    resp = await auth_client.patch(f"/api/v1/bench/entries/{b.id}", json={"model_label": "alpha"})
    assert resp.status_code == 409, resp.text


# ── video: effort as its own value ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_branding_sends_effort_apart_for_a_renderer_that_knows_it(session):
    ch, (off, mx) = await _challenge(session, "off", "max")
    payload = await orchestrator._build_branding_payload(session, ch, [off, mx], effort_field=True)

    assert [m["label"] for m in payload["models"]] == [LABEL, LABEL]
    assert [m["tag"] for m in payload["models"]] == ["LOCAL · DIRECT API"] * 2
    assert [m["effort"] for m in payload["models"]] == ["Off", "Max"]
    assert [r["name"] for r in payload["outro_rows"]] == [LABEL, LABEL]
    assert [r["effort"] for r in payload["outro_rows"]] == ["Off", "Max"]


@pytest.mark.asyncio
async def test_a_default_row_next_to_a_chosen_level_says_default(session):
    ch, (plain, mx) = await _challenge(session, "default", "max")
    payload = await orchestrator._build_branding_payload(session, ch, [plain, mx], effort_field=True)
    assert [m["effort"] for m in payload["models"]] == ["Default", "Max"]


@pytest.mark.asyncio
async def test_no_effort_anywhere_when_no_row_chose_one(session):
    ch, (a, b) = await _challenge(session, "default", "default")
    payload = await orchestrator._build_branding_payload(session, ch, [a, b], effort_field=True)
    assert [m["effort"] for m in payload["models"]] == ["", ""]
    assert [r["effort"] for r in payload["outro_rows"]] == ["", ""]


@pytest.mark.asyncio
async def test_agent_rows_carry_no_chosen_effort(session):
    ch, (agent,) = await _challenge(session, "default", kind="agent")
    payload = await orchestrator._build_branding_payload(session, ch, [agent], effort_field=True)
    assert payload["models"][0]["effort"] == ""


@pytest.mark.asyncio
async def test_old_renderer_keeps_the_text_form(session):
    """A renderer image without ``branding_effort`` ignores the new field —
    it must still show the effort, in the old tag/name text."""
    ch, (off, mx) = await _challenge(session, "off", "max")
    payload = await orchestrator._build_branding_payload(session, ch, [off, mx], effort_field=False)
    assert [m["tag"] for m in payload["models"]] == [
        "LOCAL · DIRECT API · EFFORT OFF", "LOCAL · DIRECT API · EFFORT MAX",
    ]
    assert [r["name"] for r in payload["outro_rows"]] == [f"{LABEL} · effort off", f"{LABEL} · effort max"]
    assert "effort" not in payload["models"][0]


@pytest.mark.asyncio
async def test_compose_asks_the_renderer_and_uses_the_effort_field(session, monkeypatch):
    ch, (off, mx) = await _challenge(session, "off", "max")
    for e, path in ((off, "/sd/off.mp4"), (mx, "/sd/max.mp4")):
        e.video_path = path
        e.status = "rendered"
        session.add(e)
    await session.commit()
    monkeypatch.setattr(orchestrator, "renderer_features", AsyncMock(return_value={"branding_effort"}))
    captured: dict = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"output_path": "/sd/grid.mp4"}

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
    await orchestrator.compose_challenge(session, ch, [off, mx])
    # Plain-grid labels (3+ entries, speed variant): one English word.
    assert captured["labels"] == [f"{LABEL} · Off", f"{LABEL} · Max"]
    assert [m["effort"] for m in captured["branding"]["models"]] == ["Off", "Max"]


@pytest.mark.asyncio
async def test_renderer_features_reads_health_and_fails_closed(monkeypatch):
    class Resp:
        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            pass

        def json(self):
            return self.body

    def client_returning(body=None, exc=None):
        class C:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *e):
                return False

            async def get(self, url):
                if exc:
                    raise exc
                return Resp(body)

        return C

    monkeypatch.setattr(orchestrator.httpx, "AsyncClient",
                        client_returning({"ok": True, "features": ["branding_effort"]}))
    assert await orchestrator.renderer_features() == {"branding_effort"}
    monkeypatch.setattr(orchestrator.httpx, "AsyncClient", client_returning({"ok": True}))
    assert await orchestrator.renderer_features() == set()
    monkeypatch.setattr(orchestrator.httpx, "AsyncClient", client_returning(exc=RuntimeError("down")))
    assert await orchestrator.renderer_features() == set()


# ── outro durations ────────────────────────────────────────────────────────


@pytest.mark.parametrize("ms, shown", [
    (None, "—"),
    (0, "—"),
    (45_000, "45 s"),
    (95_000, "1 min 35 s"),
    (120_000, "2 min"),
    (455_400, "7 min 35 s"),
    (3_840_000, "1 h 4 min"),
])
def test_outro_time_names_minutes_and_seconds(ms, shown):
    assert orchestrator._format_outro_time(ms) == shown
