"""The agent chat shows what the running task does (regression of #898).

Operator finding 20.10.2026: "in my chat nothing happened at all. That was not
so yesterday — even when the agent started thinking I saw something, at least
the delivery of the tasks and the thoughts." #898 pinned the agent chat to the
chat daemon's session (right: it stopped the flip between two sessions) — and
with that the ACP task session, its delivery, tools and thoughts left the chat.

``GET /agents/{id}/chat/task-activity`` brings them back as ONE separate block
(never mixed into the chat timeline, no session switch): the delivery time,
the last few timeline events of the task's own session (same parser as the
chat), the task session's live preview (a running thought included) and how
many more tasks wait in the agent's inbox. A harness whose tasks run INSIDE the
chat (Claude Code, native omp, Hermes) answers ``applies: false`` — its chat
already shows the task.

Folder layout and header shapes follow the live sessions tree of an omp agent of 20.10.2026
(ids made up).
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from httpx import AsyncClient

from app.services import omp_chat
from app.services.transcript_chat import preview_event

CHAT_SID = "01a15762-0000-7000-8000-000000000001"
TASK_SID = "01a15889-0000-7000-8000-000000000002"
OLD_SID = "01a15884-0000-7000-8000-000000000003"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _write(path: Path, lines: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return path


def _sink_header(sid: str, ts: str, task_id: str | None = None) -> dict:
    header = {"type": "session", "version": 3, "id": sid, "timestamp": ts, "cwd": "", "bridge": "acp"}
    if task_id:
        header["taskId"] = task_id
    return header


def _dispatch(ts: str, task_id: str) -> dict:
    return {"type": "message", "id": "acp00001", "parentId": None, "timestamp": ts,
            "message": {"role": "user", "content": [{"type": "text", "text":
                        f"# Task\n**Task ID:** {task_id}\nDo the thing."}]}}


def _tool(i: int, ts: str, title: str) -> list[dict]:
    call = f"call-{i}"
    return [
        {"type": "message", "id": f"acp{i:05x}", "parentId": None, "timestamp": ts,
         "message": {"role": "assistant", "model": None, "content": [
             {"type": "toolCall", "id": call, "name": "bash",
              "arguments": {"command": "ls", "i": title}}]}},
        {"type": "message", "id": f"acp{i + 1:05x}", "parentId": None, "timestamp": ts,
         "message": {"role": "toolResult", "toolCallId": call, "toolName": "bash",
                     "content": [{"type": "text", "text": "ok"}], "isError": False}},
    ]


def _thought(i: int, ts: str, text: str, ms: int) -> dict:
    return {"type": "message", "id": f"acp{i:05x}", "parentId": None, "timestamp": ts,
            "message": {"role": "assistant", "model": None,
                        "content": [{"type": "thinking", "thinking": text, "durationMs": ms}]}}


def _preview_line(ts: str, text: str, *, started: str | None = None, chars: int | None = None) -> dict:
    line = {"type": "custom_message", "customType": "acp-preview", "content": text,
            "display": True, "attribution": "agent", "id": "acp0ffff", "parentId": None,
            "timestamp": ts}
    if started is not None:
        line["data"] = {"block": "thinking", "chars": chars or len(text), "startedAt": started}
    return line


@pytest.fixture(autouse=True)
def _fresh_session_cache():
    from app.services import chat_task_activity

    chat_task_activity._session_cache.clear()
    yield
    chat_task_activity._session_cache.clear()


@pytest.fixture
def acp_driver(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "omp_driver_default", "acp", raising=False)


async def _setup(make_agent, make_board, make_task, tmp_path, monkeypatch, *, harness="omp",
                 status="in_progress", **agent_kw):
    board = await make_board(slug=f"b-{os.urandom(3).hex()}")
    agent_kw.setdefault("agent_runtime", "cli-bridge")
    agent = await make_agent(name="Alpha", slug="alpha", harness=harness, **agent_kw)
    now = datetime.now(timezone.utc)
    task = await make_task(board.id, title="Port the checklist", status=status,
                           assigned_agent_id=agent.id, dispatched_at=now - timedelta(minutes=3),
                           dispatch_attempt_id="att-1")
    from app.models.agent import Agent
    from sqlmodel.ext.asyncio.session import AsyncSession
    from tests.conftest import test_engine

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        row = await s.get(Agent, agent.id)
        row.current_task_id = task.id
        s.add(row)
        await s.commit()

    root = tmp_path / "omp-sessions"
    monkeypatch.setattr(omp_chat, "resolve_transcript_dir", lambda a: root)
    return board, agent, task, root, now


def _task_tree(root: Path, task_id: str, now: datetime) -> tuple[Path, Path]:
    ws = root / "--workspace--"
    chat = _write(ws / f"2026-10-20T03-36-53_{CHAT_SID}.jsonl",
                  [_sink_header(CHAT_SID, _iso(now - timedelta(hours=6)))])
    t0 = now - timedelta(minutes=2)
    lines = [_sink_header(TASK_SID, _iso(t0), task_id), _dispatch(_iso(t0), task_id)]
    lines += _tool(0x10, _iso(t0 + timedelta(seconds=2)), "mc ack")
    lines += _tool(0x20, _iso(t0 + timedelta(seconds=5)), "Read the spec")
    lines.append(_thought(0x30, _iso(t0 + timedelta(seconds=20)), "Plan: three steps.", 15_000))
    lines += _tool(0x40, _iso(t0 + timedelta(seconds=22)), "Run the tests")
    task_file = _write(ws / f"2026-10-20T08-59-52_{TASK_SID}.jsonl", lines)
    return chat, task_file


@pytest.mark.asyncio
async def test_running_acp_task_shows_delivery_events_and_live_thought(
    auth_client: AsyncClient, make_agent, make_board, make_task, tmp_path, monkeypatch, acp_driver
):
    board, agent, task, root, now = await _setup(make_agent, make_board, make_task, tmp_path, monkeypatch)
    await make_task(board.id, title="Next one", status="inbox", assigned_agent_id=agent.id)
    await make_task(board.id, title="Someone else's", status="inbox")
    _chat, task_file = _task_tree(root, str(task.id), now)
    started = _iso(now - timedelta(seconds=105))
    _write(task_file.parent / "previews" / f"2026-10-20T08-59-53_{TASK_SID}_c8f8b0.jsonl", [
        _preview_line(_iso(now - timedelta(seconds=100)), "older answer text"),
        _preview_line(_iso(now - timedelta(seconds=1)), "…the end of the thought",
                      started=started, chars=23_600),
    ])

    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/task-activity")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applies"] is True
    assert body["taskId"] == str(task.id)
    assert body["hasSession"] is True
    assert body["deliveredAt"].startswith(_iso(now - timedelta(minutes=2))[:19])
    kinds = [e["kind"] for e in body["events"]]
    assert kinds == ["tool", "tool", "thinking", "tool"], kinds  # the newest four, in order
    assert body["events"][2]["durationMs"] == 15_000
    assert body["preview"]["block"] == "thinking"
    assert body["preview"]["chars"] == 23_600
    assert body["preview"]["startedAt"] == started
    assert body["queued"] == 1


@pytest.mark.asyncio
async def test_a_held_card_is_not_a_waiting_task(
    auth_client: AsyncClient, make_agent, make_board, make_task, tmp_path, monkeypatch, acp_driver
):
    """Review Z4: a card a lead holds (manual_hold) or an admin stopped never
    starts by itself — the poll skips it. "1 more task waiting" must not
    promise it."""
    board, agent, task, root, now = await _setup(make_agent, make_board, make_task, tmp_path, monkeypatch)
    await make_task(board.id, title="Held", status="inbox", assigned_agent_id=agent.id,
                    run_control="manual_hold")
    await make_task(board.id, title="Stopped", status="inbox", assigned_agent_id=agent.id,
                    run_control="stopped")
    await make_task(board.id, title="Next one", status="inbox", assigned_agent_id=agent.id)
    _task_tree(root, str(task.id), now)
    body = (await auth_client.get(f"/api/v1/agents/{agent.id}/chat/task-activity")).json()
    assert body["queued"] == 1


@pytest.mark.asyncio
async def test_preview_older_than_the_last_event_is_not_live(
    auth_client: AsyncClient, make_agent, make_board, make_task, tmp_path, monkeypatch, acp_driver
):
    _board, agent, task, root, now = await _setup(make_agent, make_board, make_task, tmp_path, monkeypatch)
    _chat, task_file = _task_tree(root, str(task.id), now)
    # The thought ended with "Run the tests" (t0 + 22 s); its last snapshot is older.
    _write(task_file.parent / "previews" / f"2026-10-20T08-59-53_{TASK_SID}_c8f8b0.jsonl", [
        _preview_line(_iso(now - timedelta(minutes=2) + timedelta(seconds=19)), "thinking…",
                      started=_iso(now - timedelta(minutes=2))),
    ])
    body = (await auth_client.get(f"/api/v1/agents/{agent.id}/chat/task-activity")).json()
    assert body["applies"] is True
    assert body["preview"] is None


@pytest.mark.asyncio
async def test_delivered_but_no_session_yet_still_names_the_delivery(
    auth_client: AsyncClient, make_agent, make_board, make_task, tmp_path, monkeypatch, acp_driver
):
    """ADR-097: delivered is not working — the card is still ``inbox``
    (dispatched, not acked) while the model loads or does its prefill."""
    _board, agent, task, root, now = await _setup(make_agent, make_board, make_task, tmp_path, monkeypatch,
                                                  status="inbox")
    ws = root / "--workspace--"
    _write(ws / f"2026-10-20T03-36-53_{CHAT_SID}.jsonl", [_sink_header(CHAT_SID, _iso(now))])
    body = (await auth_client.get(f"/api/v1/agents/{agent.id}/chat/task-activity")).json()
    assert body["applies"] is True
    assert body["hasSession"] is False
    assert body["events"] == []
    assert body["preview"] is None
    assert body["deliveredAt"] is not None  # from the task's dispatch time


async def _beat(client: AsyncClient, token: str, task_id, attempt: str) -> dict:
    resp = await client.post(
        "/api/v1/agent/me/heartbeat",
        json={"status": "working", "task_id": str(task_id), "attempt_id": attempt},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _current_task_id(agent_id):
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.models.agent import Agent
    from tests.conftest import test_engine

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return (await s.get(Agent, agent_id)).current_task_id


@pytest.mark.asyncio
async def test_the_block_survives_the_bridge_heartbeat_while_the_model_loads(
    client: AsyncClient, auth_client: AsyncClient, make_agent, make_board, make_task,
    tmp_path, monkeypatch, acp_driver,
):
    """Review Z1: in the delivery phase (card inbox, dispatched, no ACK — the
    model loads or does its prefill) the bridge heartbeats every 30 s with
    the turn's task and attempt. The heartbeat's self-heal knew only
    in_progress cards and cleared ``current_task_id`` — the chat's task
    block and the "Working on <task>" row vanished after at most 30 s and
    came back with the model's first sign of life (flicker)."""
    from app.auth import generate_agent_token

    raw, token_hash = generate_agent_token()
    _board, agent, task, _root, _now = await _setup(
        make_agent, make_board, make_task, tmp_path, monkeypatch, status="inbox",
        agent_token_hash=token_hash, scopes=["heartbeat"],
    )
    url = f"/api/v1/agents/{agent.id}/chat/task-activity"
    assert (await auth_client.get(url)).json()["applies"] is True

    body = await _beat(client, raw, task.id, "att-1")
    assert "control" not in body, body  # ADR-097: live work, not withdrawn
    assert await _current_task_id(agent.id) == task.id
    after = (await auth_client.get(url)).json()
    assert after["applies"] is True and after["taskId"] == str(task.id)


@pytest.mark.asyncio
async def test_a_worker_gets_the_pointer_from_the_heartbeat_during_delivery(
    client: AsyncClient, auth_client: AsyncClient, make_agent, make_board, make_task,
    tmp_path, monkeypatch, acp_driver,
):
    """Workers in subagent mode get no pointer from the poll's claim (only
    leads do) — the heartbeat is where it comes from, in_progress or, now,
    delivered with the bridge's own attempt."""
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from tests.conftest import test_engine

    raw, token_hash = generate_agent_token()
    _board, agent, task, _root, _now = await _setup(
        make_agent, make_board, make_task, tmp_path, monkeypatch, status="inbox",
        agent_token_hash=token_hash, scopes=["heartbeat"],
    )
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        row = await s.get(Agent, agent.id)
        row.current_task_id = None
        s.add(row)
        await s.commit()
    url = f"/api/v1/agents/{agent.id}/chat/task-activity"
    assert (await auth_client.get(url)).json() == {"applies": False}

    await _beat(client, raw, task.id, "att-1")
    assert await _current_task_id(agent.id) == task.id
    assert (await auth_client.get(url)).json()["applies"] is True


@pytest.mark.asyncio
async def test_a_returned_delivery_still_clears_the_pointer(
    client: AsyncClient, make_agent, make_board, make_task, tmp_path, monkeypatch, acp_driver,
):
    """Only the SAME attempt of a delivered, unclaimed card holds it: MC
    rotated the attempt (or returned the card) — the bridge's turn is not
    this card's live work any more."""
    from app.auth import generate_agent_token

    raw, token_hash = generate_agent_token()
    _board, agent, task, _root, _now = await _setup(
        make_agent, make_board, make_task, tmp_path, monkeypatch, status="inbox",
        agent_token_hash=token_hash, scopes=["heartbeat"],
    )
    body = await _beat(client, raw, task.id, "att-OLD")
    assert body["control"]["interrupt"] == "hard", body
    assert await _current_task_id(agent.id) is None


async def _runtime(window: int):
    from app.models.runtime import Runtime
    from sqlmodel.ext.asyncio.session import AsyncSession
    from tests.conftest import test_engine

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        rt = Runtime(slug=f"rt-{os.urandom(3).hex()}", display_name="rt",
                     runtime_type="openai_compatible", endpoint="http://box.example.com:8000/v1",
                     enabled=True, max_context_len=window)
        s.add(rt)
        await s.commit()
        await s.refresh(rt)
        return rt


@pytest.mark.asyncio
async def test_the_block_carries_the_running_tasks_context_same_figure_as_the_agent_page(
    auth_client: AsyncClient, make_agent, make_board, make_task, tmp_path, monkeypatch, acp_driver
):
    """Live 30.10.2026: the operator watched a long task and the only context
    figure in the chat was the composer ring — the CHAT session's fill (idle
    for two days, 29 %). The running task's fill is the agent's heartbeat
    figure (the agent page). The task block shows THAT figure, the same
    numbers ``GET /agents/{id}`` returns — one figure, two places. A value
    stored against an older window keeps its percent (shown_context)."""
    rt = await _runtime(393_216)
    _board, agent, task, root, now = await _setup(
        make_agent, make_board, make_task, tmp_path, monkeypatch,
        runtime_id=rt.id, context_max=262_144, context_tokens=100_000,
    )
    _task_tree(root, str(task.id), now)
    body = (await auth_client.get(f"/api/v1/agents/{agent.id}/chat/task-activity")).json()
    page = (await auth_client.get(f"/api/v1/agents/{agent.id}")).json()
    assert body["context"] == {"tokens": page["context_tokens"], "max": page["context_max"]}
    assert body["context"] == {"tokens": round(100_000 / 262_144 * 393_216), "max": 393_216}


@pytest.mark.asyncio
async def test_no_reported_context_is_no_figure(
    auth_client: AsyncClient, make_agent, make_board, make_task, tmp_path, monkeypatch, acp_driver
):
    from app.models.agent import Agent
    from sqlmodel.ext.asyncio.session import AsyncSession
    from tests.conftest import test_engine

    _board, agent, task, root, now = await _setup(make_agent, make_board, make_task, tmp_path, monkeypatch)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        row = await s.get(Agent, agent.id)
        row.context_tokens = None  # the heartbeat's "unknown" after 3 misses
        s.add(row)
        await s.commit()
    _task_tree(root, str(task.id), now)
    body = (await auth_client.get(f"/api/v1/agents/{agent.id}/chat/task-activity")).json()
    assert body["applies"] is True
    assert body["context"] is None


@pytest.mark.asyncio
async def test_an_older_attempt_of_another_task_is_never_shown(
    auth_client: AsyncClient, make_agent, make_board, make_task, tmp_path, monkeypatch, acp_driver
):
    _board, agent, task, root, now = await _setup(make_agent, make_board, make_task, tmp_path, monkeypatch)
    ws = root / "--workspace--"
    other = "e6e5ca9f-0000-4000-8000-00000000000e"
    newest = _write(ws / f"2026-10-20T09-10-00_{OLD_SID}.jsonl",
                    [_sink_header(OLD_SID, _iso(now), other), _dispatch(_iso(now), other)])
    os.utime(newest, (time.time() + 5, time.time() + 5))
    body = (await auth_client.get(f"/api/v1/agents/{agent.id}/chat/task-activity")).json()
    assert body["hasSession"] is False
    assert body["events"] == []


@pytest.mark.asyncio
async def test_harness_whose_task_runs_in_the_chat_does_not_apply(
    auth_client: AsyncClient, make_agent, make_board, make_task, tmp_path, monkeypatch, acp_driver
):
    _board, agent, _task, _root, _now = await _setup(
        make_agent, make_board, make_task, tmp_path, monkeypatch, harness="claude"
    )
    body = (await auth_client.get(f"/api/v1/agents/{agent.id}/chat/task-activity")).json()
    assert body == {"applies": False}


@pytest.mark.asyncio
async def test_hermes_over_acp_runs_its_task_in_the_chat_and_does_not_apply(
    auth_client: AsyncClient, make_agent, make_board, make_task, tmp_path, monkeypatch
):
    """Review R1: the hermes-bridge hands an ACP task to the CHAT daemon
    (``deliver_prompt`` -> ``daemon.prompt``) — the operator sees the work in
    the chat itself. A task block above it was an empty box ("delivered N
    minutes ago", no steps) that polled every 2 s."""
    from app.config import settings

    monkeypatch.setattr(settings, "hermes_driver", "acp", raising=False)
    _board, agent, task, root, now = await _setup(
        make_agent, make_board, make_task, tmp_path, monkeypatch, harness="hermes",
        agent_runtime="host",
    )
    ws = root / "--workspace--"
    _write(ws / f"2026-10-20T03-36-53_{CHAT_SID}.jsonl",
           [_sink_header(CHAT_SID, _iso(now)), _dispatch(_iso(now), str(task.id))])
    body = (await auth_client.get(f"/api/v1/agents/{agent.id}/chat/task-activity")).json()
    assert body == {"applies": False}


def test_only_omp_runs_tasks_in_sessions_of_their_own(monkeypatch):
    from app.config import settings
    from app.services.transcript_adapters import adapter_for

    monkeypatch.setattr(settings, "hermes_driver", "acp", raising=False)
    monkeypatch.setattr(settings, "omp_driver_default", "acp", raising=False)

    class _A:
        def __init__(self, runtime, harness):
            self.agent_runtime, self.harness = runtime, harness

    hermes = adapter_for(_A("host", "hermes"))
    assert hermes.tasks_in_own_session() is False
    assert hermes.find_task_session is None
    omp = adapter_for(_A("cli-bridge", "omp"))
    assert omp.tasks_in_own_session() is True and callable(omp.find_task_session)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("harness", "runtime", "omp_driver", "expected"),
    [
        ("omp", "cli-bridge", "acp", True),
        ("omp", "cli-bridge", "native", False),
        ("hermes", "host", "acp", False),
        ("claude", "cli-bridge", "acp", False),
    ],
)
async def test_agent_row_says_whether_its_tasks_run_in_sessions_of_their_own(
    auth_client: AsyncClient, make_agent, monkeypatch, harness, runtime, omp_driver, expected
):
    """The chat's task steps gate on THIS flag, never on a harness name
    (PRINCIPLES §3.4, review MUST-1 on #937). List and detail both carry it,
    and it follows the adapter's rule — the omp driver switch included."""
    from app.config import settings

    monkeypatch.setattr(settings, "omp_driver_default", omp_driver, raising=False)
    monkeypatch.setattr(settings, "hermes_driver", "acp", raising=False)
    agent = await make_agent(name=f"Flag {harness} {omp_driver}", harness=harness, agent_runtime=runtime)

    detail = (await auth_client.get(f"/api/v1/agents/{agent.id}")).json()
    assert detail["tasks_in_own_session"] is expected
    listed = (await auth_client.get("/api/v1/agents")).json()
    rows = listed if isinstance(listed, list) else listed.get("items", listed.get("agents", []))
    row = next(a for a in rows if a["id"] == str(agent.id))
    assert row["tasks_in_own_session"] is expected


@pytest.mark.asyncio
async def test_native_omp_driver_does_not_apply(
    auth_client: AsyncClient, make_agent, make_board, make_task, tmp_path, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(settings, "omp_driver_default", "native", raising=False)
    _board, agent, task, root, now = await _setup(make_agent, make_board, make_task, tmp_path, monkeypatch)
    _task_tree(root, str(task.id), now)
    body = (await auth_client.get(f"/api/v1/agents/{agent.id}/chat/task-activity")).json()
    assert body == {"applies": False}


@pytest.mark.asyncio
async def test_agent_without_a_task_does_not_apply(
    auth_client: AsyncClient, make_agent, tmp_path, monkeypatch, acp_driver
):
    agent = await make_agent(name="Idle", slug="idle", agent_runtime="cli-bridge", harness="omp")
    body = (await auth_client.get(f"/api/v1/agents/{agent.id}/chat/task-activity")).json()
    assert body == {"applies": False}


@pytest.mark.asyncio
async def test_requires_auth(client: AsyncClient, make_agent):
    agent = await make_agent(name="X", slug="x", harness="omp")
    resp = await client.get(f"/api/v1/agents/{agent.id}/chat/task-activity")
    assert resp.status_code == 401


def test_find_task_session_matches_the_header_newest_attempt_wins(tmp_path):
    ws = tmp_path / "--workspace--"
    tid = "d3e397c0-0000-4000-8000-000000000001"
    first = _write(ws / f"2026-10-20T08-50-00_{OLD_SID}.jsonl", [_sink_header(OLD_SID, "x", tid)])
    second = _write(ws / f"2026-10-20T08-59-52_{TASK_SID}.jsonl", [_sink_header(TASK_SID, "x", tid)])
    os.utime(first, (1_700_000_000, 1_700_000_000))
    os.utime(second, (1_700_000_100, 1_700_000_100))
    _write(ws / f"2026-10-20T03-36-53_{CHAT_SID}.jsonl", [_sink_header(CHAT_SID, "x")])
    assert omp_chat.find_task_session(tmp_path, tid.upper()) == second
    assert omp_chat.find_task_session(tmp_path, tid, since=1_700_000_200) is None
    assert omp_chat.find_task_session(tmp_path, "nope") is None


def test_preview_event_keeps_the_end_and_marks_a_thought():
    long = "a" * 5000 + "END"
    ev = preview_event(long, "t", None)
    assert ev["text"].endswith("END") and "block" not in ev
    ev = preview_event("x", "t", {"block": "thinking", "chars": 9, "startedAt": "s"})
    assert (ev["block"], ev["chars"], ev["startedAt"]) == ("thinking", 9, "s")
    ev = preview_event("x", "t", {"block": "thinking", "chars": True, "startedAt": 5})
    assert ev["block"] == "thinking" and "chars" not in ev and "startedAt" not in ev
