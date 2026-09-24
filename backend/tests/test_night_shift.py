"""Night shift — marking API, the worker tick (window, box, order, cloud
share), blocked notices and the morning report."""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.app_setting import AppSetting
from app.models.host import Host
from app.models.repo import Repo
from app.models.runtime import Runtime
from app.models.task import Task, TaskEvent
from app.services import runtime_protocols as rp
from app.services.heads import engine, night, night_store
from app.services.heads.night_shift import tick
from app.services.heads.night_store import HOLD_REASON, Mark
from tests.conftest import test_engine
from tests.heads_backend_helpers import BOX, heads_root, make_run  # noqa: F401

EP = "http://192.0.2.10:8000/v1"


@pytest.fixture(autouse=True)
def _probes(monkeypatch):
    rp.clear_cache()
    engine.clear_cache()
    state = {"served": frozenset({"glm"})}

    async def served(endpoint, now=None):
        return state["served"]

    async def running(endpoint):
        return None

    async def anth(endpoint):
        return True

    monkeypatch.setattr(engine, "served_models", served)
    monkeypatch.setattr(engine, "running_requests", running)
    monkeypatch.setattr(rp, "probe_anthropic_route", anth)
    return state


class Sent:
    def __init__(self, fail: bool = False):
        self.texts: list[str] = []
        self.fail = fail

    async def __call__(self, text: str) -> bool:
        if self.fail:
            raise RuntimeError("channel down")
        self.texts.append(text)
        return True


async def _world(make_board, make_task, *, titles=("First job",), box_id=None):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        box = Host(id=uuid.UUID(box_id or BOX), slug=f"box-{uuid.uuid4().hex[:4]}", display_name="BOX",
                   kind="ssh", ssh_host="192.0.2.10")
        s.add(box)
        await s.commit()
        s.add(Runtime(slug="box-slot", display_name="Local slot", runtime_type="openai_compatible",
                      endpoint=EP, model_identifier="glm", host_id=box.id, is_slot=True))
        s.add(Runtime(slug="ollama-cloud", display_name="Cloud", runtime_type="openai_compatible",
                      endpoint="https://ollama.example/v1", model_identifier="big"))
        repo = Repo(full_name="owner/demo", url="https://github.com/owner/demo", default_branch="main")
        s.add(repo)
        await s.commit()
        await s.refresh(repo)
    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    tasks = [await make_task(board.id, title=t, status="inbox", repo_id=repo.id) for t in titles]
    return tasks


async def _cfg(**over) -> None:
    values = {"enabled": True, "timezone": "UTC", **over}
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        await night_store.save_config(s, values)


def _hhmm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def _window_around_now(now: datetime) -> dict:
    """A window that contains ``now`` (start 1 h before, end 2 h after)."""
    return {"start": _hhmm(now - timedelta(hours=1)), "end": _hhmm(now + timedelta(hours=2))}


def _mark(task: Task, *, order: float, harness="omp", runtime="box-slot", locality="local", **over) -> Mark:
    m = Mark(task_id=str(task.id), harness=harness, runtime_slug=runtime, locality=locality, marked_at=order, **over)
    night_store.save_mark(m)
    return m


async def _tick(now: datetime | None = None, send=None):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return await tick(s, now or datetime.now(UTC), send=send or Sent())


async def _task(task_id) -> Task:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return await s.get(Task, task_id)


# ── API: marking ────────────────────────────────────────────────────────────


async def test_disabled_is_404(auth_client, monkeypatch):
    monkeypatch.setattr(settings, "heads_enabled", False)
    for path in ("/api/v1/night-shift/config", "/api/v1/night-shift/tonight"):
        resp = await auth_client.get(path)
        assert resp.status_code == 404 and resp.json()["detail"]["code"] == "heads_disabled"


async def test_mark_holds_the_inbox_card_and_unmark_releases_it(auth_client, heads_root, make_board, make_task):
    (task,) = await _world(make_board, make_task)
    resp = await auth_client.put(f"/api/v1/night-shift/tasks/{task.id}",
                                 json={"harness": "omp", "runtime_slug": "box-slot"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["mark"]["state"] == "queued"
    stored = json.loads((heads_root / "night" / "marks" / f"{task.id}.json").read_text())
    assert stored["harness"] == "omp" and stored["locality"] == "local" and stored["held"] is True
    fresh = await _task(task.id)
    assert (fresh.status, fresh.run_control, fresh.hold_reason) == ("inbox", "manual_hold", HOLD_REASON)

    listing = (await auth_client.get("/api/v1/night-shift/tonight")).json()
    assert [e["title"] for e in listing["entries"]] == ["First job"]
    assert listing["config"]["start"] == "22:00" and listing["config"]["enabled"] is False

    resp = await auth_client.delete(f"/api/v1/night-shift/tasks/{task.id}")
    assert resp.status_code == 200 and resp.json() == {"mark": None}
    fresh = await _task(task.id)
    assert (fresh.run_control, fresh.hold_reason) == (None, None)
    assert (await auth_client.get(f"/api/v1/night-shift/tasks/{task.id}")).json() == {"mark": None}


async def test_unmark_leaves_a_foreign_hold_alone(auth_client, heads_root, make_board, make_task):
    (task,) = await _world(make_board, make_task)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task.id)
        t.run_control, t.hold_reason = "manual_hold", "operator said so"
        s.add(t)
        await s.commit()
    await auth_client.put(f"/api/v1/night-shift/tasks/{task.id}", json={"harness": "omp", "runtime_slug": "box-slot"})
    await auth_client.delete(f"/api/v1/night-shift/tasks/{task.id}")
    fresh = await _task(task.id)
    assert (fresh.run_control, fresh.hold_reason) == ("manual_hold", "operator said so")


async def test_unmark_keeps_a_hold_the_operator_changed_meanwhile(auth_client, heads_root, make_board, make_task):
    (task,) = await _world(make_board, make_task)
    await auth_client.put(f"/api/v1/night-shift/tasks/{task.id}", json={"harness": "omp", "runtime_slug": "box-slot"})
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task.id)
        t.hold_reason = "waiting for the customer"
        s.add(t)
        await s.commit()
    await auth_client.delete(f"/api/v1/night-shift/tasks/{task.id}")
    fresh = await _task(task.id)
    assert (fresh.run_control, fresh.hold_reason) == ("manual_hold", "waiting for the customer")


async def test_engine_down_by_day_can_still_be_marked(auth_client, heads_root, make_board, make_task, _probes):
    (task,) = await _world(make_board, make_task)
    _probes["served"] = None
    resp = await auth_client.put(f"/api/v1/night-shift/tasks/{task.id}",
                                 json={"harness": "omp", "runtime_slug": "box-slot"})
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize(
    "body,status,code",
    [
        ({"harness": "omp", "runtime_slug": "ollama-cloud"}, 422, "pair_blocked"),  # cloud pair: unproven in v1
        ({"harness": "kimi", "runtime_slug": "box-slot"}, 422, "pair_blocked"),
        ({"harness": "omp", "runtime_slug": "nope"}, 422, "pair_blocked"),
    ],
)
async def test_mark_refuses_pairs_that_cannot_run(auth_client, heads_root, make_board, make_task, body, status, code):
    (task,) = await _world(make_board, make_task)
    resp = await auth_client.put(f"/api/v1/night-shift/tasks/{task.id}", json=body)
    assert resp.status_code == status and resp.json()["detail"]["code"] == code
    assert not (heads_root / "night" / "marks" / f"{task.id}.json").exists()


async def test_mark_needs_repo_and_open_task(auth_client, heads_root, make_board, make_task):
    (task,) = await _world(make_board, make_task)
    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    no_repo = await make_task(board.id, title="No repo", status="inbox")
    done = await make_task(board.id, title="Done", status="done", repo_id=task.repo_id)
    body = {"harness": "omp", "runtime_slug": "box-slot"}
    r1 = await auth_client.put(f"/api/v1/night-shift/tasks/{no_repo.id}", json=body)
    r2 = await auth_client.put(f"/api/v1/night-shift/tasks/{done.id}", json=body)
    assert (r1.status_code, r1.json()["detail"]["code"]) == (422, "repo_required")
    assert (r2.status_code, r2.json()["detail"]["code"]) == (409, "task_finished")


async def test_started_mark_cannot_be_removed(auth_client, heads_root, make_board, make_task):
    (task,) = await _world(make_board, make_task)
    _mark(task, order=1, run_id=str(uuid.uuid4()), night="2026-09-24")
    resp = await auth_client.delete(f"/api/v1/night-shift/tasks/{task.id}")
    assert (resp.status_code, resp.json()["detail"]["code"]) == (409, "night_started")


# ── API: settings ──────────────────────────────────────────────────────────


async def test_config_roundtrip_and_validation(auth_client, heads_root):
    resp = await auth_client.put("/api/v1/night-shift/config",
                                 json={"enabled": True, "start": "23:30", "end": "05:00",
                                       "timezone": "Europe/Berlin", "cloud_share": 50})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["enabled"], body["start"], body["end"], body["timezone"], body["cloud_share"]) == (
        True, "23:30", "05:00", "Europe/Berlin", 50)
    assert set(body["window"]) == {"night", "starts_at", "ends_at"}

    bad = await auth_client.put("/api/v1/night-shift/config", json={"start": "25:00", "timezone": "Nowhere/City"})
    assert bad.status_code == 422
    assert bad.json()["detail"] == {"code": "invalid_config", "fields": ["start", "timezone"]}
    # nothing of the bad request was written
    assert (await auth_client.get("/api/v1/night-shift/config")).json()["start"] == "23:30"


async def test_config_is_read_from_the_db_not_the_process(heads_root):
    """The worker must see what the API process saved (no singleton patch)."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(AppSetting(key="night_shift_start", value="21:15"))
        s.add(AppSetting(key="night_shift_cloud_share", value="not-a-number"))
        await s.commit()
        cfg = await night_store.load_config(s)
    assert cfg.start == "21:15" and cfg.cloud_share == 30


async def test_an_invalid_stored_window_falls_back_to_the_defaults(heads_root):
    """Never a half-valid window: a broken zone row means the env defaults."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(AppSetting(key="night_shift_timezone", value="Nowhere/City"))
        s.add(AppSetting(key="night_shift_start", value="21:15"))
        await s.commit()
        cfg = await night_store.load_config(s)
    assert (cfg.start, cfg.timezone) == ("22:00", "UTC")


async def test_settings_page_rows_do_not_trip_the_channel_warning(heads_root, caplog):
    from app.services.channel_config import stored_overrides

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(AppSetting(key="night_shift_end", value="05:00"))
        await s.commit()
        with caplog.at_level("WARNING"):
            await stored_overrides(s)
    assert "night_shift_end" not in caplog.text


# ── Tick: window + start ───────────────────────────────────────────────────


async def test_inside_the_window_starts_the_first_mark_through_the_head_launcher(heads_root, make_board, make_task):
    first, second = await _world(make_board, make_task, titles=("First job", "Second job"))
    now = datetime.now(UTC)
    await _cfg(**_window_around_now(now))
    _mark(second, order=2)
    _mark(first, order=1)

    result = await _tick(now)
    assert result.started == str(first.id)
    mark = night_store.load_mark(str(first.id))
    assert mark.run_id and mark.night == night.current_window(now, await _load_cfg()).night
    spool = json.loads((heads_root / "spool" / f"{mark.run_id}.start.json").read_text())
    assert spool == {"action": "start", "run_id": mark.run_id}
    fresh = await _task(first.id)
    assert (fresh.status, fresh.run_control) == ("in_progress", "manual_hold")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        events = (await s.exec(select(TaskEvent).where(TaskEvent.task_id == first.id))).all()
    assert [e.reason for e in events] == ["night_shift_start"]

    # the same box is now busy with the first head: the second one waits
    result = await _tick(now + timedelta(seconds=60))
    assert result.started is None
    assert night_store.load_mark(str(second.id)).last_error == "lane_busy"
    assert (await _task(second.id)).status == "inbox"


async def _load_cfg():
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return await night_store.load_config(s)


async def test_outside_the_window_nothing_starts(heads_root, make_board, make_task):
    (task,) = await _world(make_board, make_task)
    now = datetime.now(UTC)
    await _cfg(start=_hhmm(now + timedelta(hours=1)), end=_hhmm(now + timedelta(hours=3)))
    _mark(task, order=1)
    assert (await _tick(now)).started is None
    assert night_store.load_mark(str(task.id)).night is None
    assert not (heads_root / "spool").exists()


async def test_switched_off_nothing_starts(heads_root, make_board, make_task):
    (task,) = await _world(make_board, make_task)
    now = datetime.now(UTC)
    await _cfg(enabled=False, **_window_around_now(now))
    _mark(task, order=1)
    assert (await _tick(now)).started is None
    assert (await _task(task.id)).status == "inbox"


async def test_heads_disabled_nothing_happens(heads_root, make_board, make_task, monkeypatch):
    (task,) = await _world(make_board, make_task)
    now = datetime.now(UTC)
    await _cfg(**_window_around_now(now))
    _mark(task, order=1)
    monkeypatch.setattr(settings, "heads_enabled", False)
    assert (await _tick(now)).started is None


async def test_box_busy_with_a_day_head_waits(heads_root, make_board, make_task):
    (task,) = await _world(make_board, make_task)
    now = datetime.now(UTC)
    await _cfg(**_window_around_now(now))
    make_run(heads_root, task_id=str(uuid.uuid4()), status={"phase": "running", "started_at": "x"},
             heartbeat_age=5)
    _mark(task, order=1)
    assert (await _tick(now)).started is None
    mark = night_store.load_mark(str(task.id))
    assert mark.last_error == "lane_busy" and mark.run_id is None


async def test_engine_down_at_night_retries_next_tick(heads_root, make_board, make_task, _probes):
    (task,) = await _world(make_board, make_task)
    now = datetime.now(UTC)
    await _cfg(**_window_around_now(now))
    _mark(task, order=1)
    _probes["served"] = None
    assert (await _tick(now)).started is None
    mark = night_store.load_mark(str(task.id))
    assert (mark.last_error, mark.gave_up) == ("engine_not_ready", None)
    _probes["served"] = frozenset({"glm"})
    engine.clear_cache()
    assert (await _tick(now + timedelta(seconds=60))).started == str(task.id)


async def test_cloud_share_blocks_a_lone_cloud_mark(heads_root, make_board, make_task):
    (task,) = await _world(make_board, make_task)
    now = datetime.now(UTC)
    await _cfg(cloud_share=30, **_window_around_now(now))
    # A cloud mark (e.g. a pair that becomes startable later): floor(1 × 30 %) = 0
    _mark(task, order=1, runtime="ollama-cloud", locality="cloud")
    assert (await _tick(now)).started is None
    assert night_store.load_mark(str(task.id)).last_error == "cloud_share"


async def test_deleted_task_drops_its_mark(heads_root, make_board, make_task):
    (task,) = await _world(make_board, make_task)
    _mark(task, order=1)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        await s.delete(await s.get(Task, task.id))
        await s.commit()
    result = await _tick()
    assert result.dropped == [str(task.id)]
    assert night_store.load_mark(str(task.id)) is None


# ── Blocked notices ────────────────────────────────────────────────────────


async def test_question_is_reported_once_and_nothing_restarts(heads_root, make_board, make_task):
    (task,) = await _world(make_board, make_task)
    run_id = make_run(heads_root, task_id=str(task.id),
                      status={"phase": "exited", "exit_code": 0, "started_at": "2026-09-24T22:00:00Z"},
                      question="Remove the endpoint? Two callers still use it.")
    _mark(task, order=1, run_id=run_id, night="2099-01-01")
    sent = Sent()
    result = await _tick(send=sent)
    assert result.notices == [f"{task.id}:needs_you"]
    assert sent.texts[0].startswith("Night shift: head needs you — First job")
    assert f"/tasks?task={task.id}" in sent.texts[0]
    again = Sent()
    assert (await _tick(send=again)).notices == [] and again.texts == []
    assert not (heads_root / "spool").exists()  # no restart, no stop


async def test_head_without_heartbeat_for_15_min_is_reported(heads_root, make_board, make_task):
    (task,) = await _world(make_board, make_task)
    run_id = make_run(heads_root, task_id=str(task.id), status={"phase": "running", "started_at": "x"},
                      heartbeat_age=16 * 60)
    _mark(task, order=1, run_id=run_id, night="2099-01-01")
    sent = Sent()
    assert (await _tick(send=sent)).notices == [f"{task.id}:silent"]
    assert sent.texts[0].startswith("Night shift: head silent for 16 min — First job")


async def test_healthy_head_is_not_reported(heads_root, make_board, make_task):
    (task,) = await _world(make_board, make_task)
    run_id = make_run(heads_root, task_id=str(task.id), status={"phase": "running", "started_at": "x"},
                      heartbeat_age=10)
    _mark(task, order=1, run_id=run_id, night="2099-01-01")
    assert (await _tick()).notices == []


# ── Morning report ─────────────────────────────────────────────────────────


async def _ended_night(now: datetime) -> str:
    await _cfg(start=_hhmm(now - timedelta(hours=4)), end=_hhmm(now - timedelta(hours=1)))
    return night.current_window(now - timedelta(hours=2), await _load_cfg()).night


async def test_morning_report_once_with_every_outcome(heads_root, make_board, make_task, monkeypatch):
    monkeypatch.setattr(settings, "mc_base_url", "https://mc.example")
    passed, asked, waiting = await _world(make_board, make_task, titles=("Passed job", "Asking job", "Never ran"))
    now = datetime.now(UTC)
    key = await _ended_night(now)
    ok_run = make_run(heads_root, task_id=str(passed.id),
                      status={"phase": "exited", "exit_code": 0, "pr_url": "https://github.com/owner/demo/pull/9",
                              "started_at": "x"})
    q_run = make_run(heads_root, task_id=str(asked.id), status={"phase": "exited", "exit_code": 0, "started_at": "x"},
                     question="Which one?")
    _mark(passed, order=1, run_id=ok_run, night=key)
    _mark(asked, order=2, run_id=q_run, night=key, notified=["needs_you"])
    _mark(waiting, order=3, night=key, last_error="engine_not_ready")
    # the PR counts only with a valid run record → without one: failed
    sent = Sent()
    result = await _tick(now, send=sent)
    assert result.reported == key
    report = [t for t in sent.texts if t.startswith(f"Night shift {key}")]
    assert len(report) == 1
    text = report[0]
    assert text.splitlines()[0] == f"Night shift {key}: 0 passed · 1 failed · 1 needs you · 1 blocked"
    assert f"- Passed job (run_record_missing) https://mc.example/tasks?task={passed.id}" in text
    assert "PR: https://github.com/owner/demo/pull/9" in text
    assert f"- Never ran (not started, engine_not_ready) https://mc.example/tasks?task={waiting.id}" in text

    stored = json.loads((heads_root / "night" / "reports" / f"{key}.json").read_text())
    assert stored["night"] == key and stored["delivered"] is True and len(stored["entries"]) == 3
    # started marks are archived, the never-started one waits for the next night
    assert night_store.load_mark(str(passed.id)) is None and night_store.load_mark(str(asked.id)) is None
    left = night_store.load_mark(str(waiting.id))
    assert left is not None and left.night is None

    again = Sent()
    assert (await _tick(now + timedelta(minutes=1), send=again)).reported is None
    assert again.texts == []


async def test_passed_needs_pr_and_run_record(heads_root, make_board, make_task):
    from tests.heads_backend_helpers import write_run_record

    (task,) = await _world(make_board, make_task)
    now = datetime.now(UTC)
    key = await _ended_night(now)
    started = (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    run_id = make_run(heads_root, task_id=str(task.id), status={"phase": "exited", "exit_code": 0,
                                                              "pr_url": "https://github.com/owner/demo/pull/3",
                                                              "started_at": started})
    rr = write_run_record(heads_root, run_id, passed=True)
    status = json.loads((heads_root / run_id / ".wrapper" / "status.json").read_text())
    status["run_record_path"] = str(rr)
    (heads_root / run_id / ".wrapper" / "status.json").write_text(json.dumps(status))
    _mark(task, order=1, run_id=run_id, night=key)
    sent = Sent()
    await _tick(now, send=sent)
    assert sent.texts[-1].splitlines()[0] == f"Night shift {key}: 1 passed · 0 failed · 0 needs you · 0 blocked"


async def test_report_failure_is_retried(heads_root, make_board, make_task):
    (task,) = await _world(make_board, make_task)
    now = datetime.now(UTC)
    key = await _ended_night(now)
    _mark(task, order=1, night=key)
    assert (await _tick(now, send=Sent(fail=True))).reported is None
    assert not (heads_root / "night" / "reports" / f"{key}.json").exists()
    assert night_store.load_mark(str(task.id)).night == key
    sent = Sent()
    assert (await _tick(now, send=sent)).reported == key
    assert len(sent.texts) == 1


async def test_report_language_follows_the_lead_agent(heads_root, make_board, make_task, make_agent):
    (task,) = await _world(make_board, make_task)
    await make_agent(name="Lead", is_board_lead=True, operator_language="de")
    now = datetime.now(UTC)
    key = await _ended_night(now)
    _mark(task, order=1, night=key)
    sent = Sent()
    await _tick(now, send=sent)
    assert sent.texts[0].startswith(f"Nachtschicht {key}:")


async def test_report_file_survives_restart_without_second_message(heads_root, make_board, make_task):
    """Crash after sending but before tidying: the next tick tidies, sends nothing."""
    (task,) = await _world(make_board, make_task)
    now = datetime.now(UTC)
    key = await _ended_night(now)
    _mark(task, order=1, night=key, run_id=str(uuid.uuid4()))
    assert night_store.reserve_report(key)
    sent = Sent()
    await _tick(now, send=sent)
    assert sent.texts == []
    assert night_store.load_mark(str(task.id)) is None


def test_no_home_lookup_in_night_modules():
    import app.services.heads.night as a
    import app.services.heads.night_shift as b
    import app.services.heads.night_store as c

    for mod in (a, b, c):
        with open(mod.__file__) as fh:
            assert "Path.home" not in fh.read()
    assert night_store.night_dir() == settings.heads_root / "night"
