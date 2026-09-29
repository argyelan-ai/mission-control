"""Night shift — MC is the primary channel.

The morning report and the blocked notices live in MC ("Last night" card on
Home, ``GET /api/v1/night-shift/last-night``). Slack / Telegram get them only
when the operator switched ``send_to_channels`` on (default off)."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.services.heads import night, night_store
from tests.conftest import test_engine
from tests.heads_backend_helpers import heads_root, make_run  # noqa: F401
from tests.test_night_shift import (  # noqa: F401
    Sent,
    _cfg,
    _ended_night,
    _load_cfg,
    _mark,
    _probes,
    _tick,
    _world,
)

URL = "/api/v1/night-shift/last-night"


@pytest.fixture(autouse=True)
def _channels_off(monkeypatch):
    """The shipped default: MC only."""
    monkeypatch.setattr(settings, "night_shift_send_to_channels", False)


def test_channels_are_off_by_default():
    from app.config import Settings

    assert Settings.model_fields["night_shift_send_to_channels"].default is False
    assert night.NightConfig().send_to_channels is False


# ── Delivery switch ─────────────────────────────────────────────────────────


async def test_report_is_kept_in_mc_and_not_sent_when_channels_are_off(heads_root, make_board, make_task):
    (task,) = await _world(make_board, make_task)
    now = datetime.now(UTC)
    key = await _ended_night(now)
    run_id = make_run(heads_root, task_id=str(task.id), status={"phase": "exited", "exit_code": 1, "started_at": "x"})
    _mark(task, order=1, night=key, run_id=run_id)
    sent = Sent()
    result = await _tick(now, send=sent)
    assert sent.texts == []
    assert result.reported is None
    stored = night_store.load_report(key)
    assert stored["state"] == "stored" and stored["delivered"] is False and len(stored["entries"]) == 1
    # archived like a sent report, and never sent later
    assert night_store.load_mark(str(task.id)) is None
    await _tick(now + timedelta(hours=1), send=sent)
    assert sent.texts == []


async def test_report_is_sent_when_channels_are_on(heads_root, make_board, make_task):
    (task,) = await _world(make_board, make_task)
    now = datetime.now(UTC)
    key = await _ended_night(now)
    await _cfg(send_to_channels=True)
    _mark(task, order=1, night=key, run_id=str(uuid.uuid4()))
    sent = Sent()
    assert (await _tick(now, send=sent)).reported == key
    assert len(sent.texts) == 1 and sent.texts[0].startswith(f"Night shift {key}")
    assert night_store.load_report(key)["delivered"] is True


async def test_notice_is_not_sent_when_channels_are_off(heads_root, make_board, make_task):
    (task,) = await _world(make_board, make_task)
    run_id = make_run(heads_root, task_id=str(task.id), status={"phase": "exited", "exit_code": 0, "started_at": "x"},
                      question="Which one?")
    _mark(task, order=1, run_id=run_id, night="2099-01-01")
    sent = Sent()
    assert (await _tick(send=sent)).notices == []
    assert sent.texts == []
    # switched on later: the open notice still goes out once
    await _cfg(send_to_channels=True)
    assert (await _tick(send=sent)).notices == [f"{task.id}:needs_you"]


async def test_undelivered_report_is_not_retried_after_channels_were_switched_off(heads_root, make_board, make_task):
    (task,) = await _world(make_board, make_task)
    now = datetime.now(UTC)
    key = await _ended_night(now)
    await _cfg(send_to_channels=True)
    _mark(task, order=1, night=key, run_id=str(uuid.uuid4()))
    await _tick(now, send=Sent(fail=True))
    assert night_store.load_report(key)["state"] == "undelivered"
    await _cfg(send_to_channels=False)
    sent = Sent()
    await _tick(now + timedelta(hours=1), send=sent)
    assert sent.texts == []


async def test_config_carries_the_switch_and_the_configured_channels(auth_client, heads_root):
    body = (await auth_client.get("/api/v1/night-shift/config")).json()
    assert body["send_to_channels"] is False
    assert isinstance(body["channels"], list)
    resp = await auth_client.put("/api/v1/night-shift/config", json={"send_to_channels": True})
    assert resp.status_code == 200 and resp.json()["send_to_channels"] is True


# ── Last night card ─────────────────────────────────────────────────────────


async def test_nothing_ran_means_no_card(auth_client, heads_root):
    body = (await auth_client.get(URL)).json()
    assert body == {"report": None, "notices": []}


async def test_card_shows_last_nights_report_with_the_live_run(auth_client, heads_root, make_board, make_task):
    asked, failed = await _world(make_board, make_task, titles=("Asking job", "Broken job"))
    now = datetime.now(UTC)
    key = await _ended_night(now)
    q_run = make_run(heads_root, task_id=str(asked.id), status={"phase": "exited", "exit_code": 0, "started_at": "x"},
                     question="Keep the old endpoint?")
    f_run = make_run(heads_root, task_id=str(failed.id), status={"phase": "exited", "exit_code": 1, "started_at": "x"})
    _mark(asked, order=1, night=key, run_id=q_run)
    _mark(failed, order=2, night=key, run_id=f_run)
    await _tick(now, send=Sent())

    body = (await auth_client.get(URL)).json()
    report = body["report"]
    assert report["night"] == key and report["dismissed"] is False
    by_title = {e["title"]: e for e in report["entries"]}
    assert by_title["Asking job"]["category"] == "needs_you"
    assert by_title["Asking job"]["run"]["question"] == "Keep the old endpoint?"
    assert by_title["Asking job"]["run"]["state"] == "needs_you"
    assert by_title["Broken job"]["category"] == "failed"
    assert "text" not in report  # the channel text stays in the file


async def test_dismiss_hides_the_card(auth_client, heads_root, make_board, make_task):
    (task,) = await _world(make_board, make_task)
    now = datetime.now(UTC)
    key = await _ended_night(now)
    _mark(task, order=1, night=key, run_id=str(uuid.uuid4()))
    await _tick(now, send=Sent())
    resp = await auth_client.post(f"{URL}/{key}/dismiss")
    assert resp.status_code == 200
    assert (await auth_client.get(URL)).json()["report"] is None
    assert (await auth_client.post(f"{URL}/not-a-night/dismiss")).status_code == 422


async def test_card_goes_away_when_the_next_night_starts(heads_root, make_board, make_task):
    from app.routers.night_shift import last_night_view

    (task,) = await _world(make_board, make_task)
    now = datetime.now(UTC)
    key = await _ended_night(now)
    _mark(task, order=1, night=key, run_id=str(uuid.uuid4()))
    await _tick(now, send=Sent())
    cfg = await _load_cfg()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        assert (await last_night_view(s, now))["report"]["night"] == key
        # the next window of the same config has begun: yesterday's card is gone
        nxt = night.window_of_night(
            (datetime.fromisoformat(key) + timedelta(days=1)).date().isoformat(), cfg).starts_at
        assert (await last_night_view(s, nxt + timedelta(minutes=1)))["report"] is None


async def test_blocked_notices_show_in_mc_during_the_night(auth_client, heads_root, make_board, make_task):
    silent, fine = await _world(make_board, make_task, titles=("Silent job", "Fine job"))
    s_run = make_run(heads_root, task_id=str(silent.id), status={"phase": "running", "started_at": "x"},
                     heartbeat_age=16 * 60)
    f_run = make_run(heads_root, task_id=str(fine.id), status={"phase": "running", "started_at": "x"},
                     heartbeat_age=10)
    _mark(silent, order=1, run_id=s_run, night="2099-01-01")
    _mark(fine, order=2, run_id=f_run, night="2099-01-01")
    body = (await auth_client.get(URL)).json()
    assert [(n["title"], n["kind"]) for n in body["notices"]] == [("Silent job", "silent")]
    assert body["notices"][0]["task_id"] == str(silent.id)
    assert body["notices"][0]["run"]["run_id"] == s_run
