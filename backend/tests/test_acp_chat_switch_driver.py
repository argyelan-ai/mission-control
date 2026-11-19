"""The omp side of a chat switch, backend half (S3 item C, ADR-105).

``acp_chat_chats.OmpChatDriver`` is ``chat_switch``'s driver for harness
``omp``: it talks to the agent's chat daemon through the ctl shim
(``DockerCtlTransport``, replaced here by a recording fake) — the daemon half
is ``docker/omp-bridge/tests/test_acp_chat_switch.py``.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.chat_record import Chat
from app.services import acp_chat_chats, acp_chat_transport, chat_switch
from tests.conftest import test_engine
from tests.test_chat_record import flag_on  # noqa: F401 — fixture used by name
from tests.test_chat_switch import _switch, _switch_rows, _world


class FakeDaemon:
    """One chat daemon: every ctl call of every transport, in order."""

    def __init__(self, *, busy=False, handed=(), switch_answer=None, config_answer=None):
        self.busy = busy
        self.handed = list(handed)
        self.switch_answer = switch_answer
        self.config_answer = config_answer
        self.calls: list[tuple] = []

    def transport(self, slug, chat_id=None):
        daemon = self

        class _T:
            async def state(self):
                daemon.calls.append(("state", chat_id))
                return {"ok": True, "busy": daemon.busy}

            async def stop_for_switch(self):
                daemon.calls.append(("stop_for_switch", chat_id))
                daemon.busy = False
                handed, daemon.handed = daemon.handed, []
                return {"ok": True, "handedOver": handed, "stopped": True}

            async def switch(self, payload):
                daemon.calls.append(("switch", chat_id, payload))
                return daemon.switch_answer or {"ok": True, "switching": True, "handedOver": []}

            async def config(self, id, value):
                daemon.calls.append(("config", chat_id, id, value))
                return daemon.config_answer or {"ok": True}

            async def prompt(self, text, mode=None, origin=None, context=None):
                daemon.calls.append(("prompt", chat_id, text, mode, origin, context))
                return {"ok": True, "queued": True}

        return _T()


@pytest.fixture
def daemon(monkeypatch, flag_on):  # noqa: F811 — the fixture by name
    fake = FakeDaemon()
    monkeypatch.setattr(acp_chat_transport, "DockerCtlTransport", fake.transport)
    # the real driver, fresh per test (no held queue from another test)
    monkeypatch.setattr(chat_switch, "_DRIVERS", {"omp": acp_chat_chats.OmpChatDriver()})

    async def _no_lock(runtime):
        return None

    monkeypatch.setattr(chat_switch, "box_lock_for", _no_lock)
    return fake


async def _drift(chat_id, runtime_id):
    """The chat's pair names another runtime than its agent runs on now."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        chat = await s.get(Chat, chat_id)
        chat.runtime_id = runtime_id
        s.add(chat)
        await s.commit()


def test_the_omp_driver_is_registered_at_import():
    assert isinstance(acp_chat_chats.SWITCH_DRIVER, acp_chat_chats.OmpChatDriver)
    assert chat_switch._DRIVERS.get("omp") is acp_chat_chats.SWITCH_DRIVER


async def test_stop_and_switch_hands_the_queue_back_after_the_switch(daemon, make_agent):
    """F1 + F8 + load first: a running turn is stopped through the daemon,
    the held queue comes back to MC and goes out again AFTER the switch
    op, in order; the switch tries ``session/load`` of the pinned id and
    carries the package and the new model."""
    agent, chat, rts = await _world(make_agent)
    await _drift(chat.id, rts["cloud"].id)
    daemon.busy = True
    daemon.handed = [{"text": "one", "origin": "operator"},
                     {"text": "two", "origin": "operator", "context": "RUN"}]

    out = await _switch(chat.id, harness="omp", runtime_id=rts["local"].id, model="qwen-b",
                        mode="stop_and_switch")

    assert out["status"] == "pending" and out["path"] == "restart"
    ops = [c[0] for c in daemon.calls]
    assert ops == ["state", "stop_for_switch", "switch", "prompt", "prompt"]
    assert {c[1] for c in daemon.calls} == {str(chat.id)}        # always this chat
    payload = daemon.calls[2][2]
    assert payload["switchEventId"] == out["eventId"]
    assert payload["action"] == "resume" and payload["nativeSessionId"] == "s-old"
    assert payload["tryNativeLoad"] is True and payload["model"] == "qwen-b"
    assert "rename loader.py" in payload["package"]
    assert daemon.calls[3][2:] == ("one", "queue", "operator", None)
    assert daemon.calls[4][2:] == ("two", "queue", "operator", "RUN")


async def test_switch_while_a_turn_runs_is_refused_without_stop(daemon, make_agent):
    agent, chat, rts = await _world(make_agent)
    await _drift(chat.id, rts["cloud"].id)
    daemon.busy = True
    with pytest.raises(chat_switch.SwitchError) as err:
        await _switch(chat.id, harness="omp", runtime_id=rts["local"].id, model=None)
    assert err.value.status == 409 and err.value.code == "turn_running"
    assert [c[0] for c in daemon.calls] == ["state"]
    assert await _switch_rows(chat.id) == []


async def test_another_runtime_than_the_agents_is_refused_honestly(daemon, make_agent):
    """One models.yml per agent: the daemon cannot serve one chat on another
    runtime — the switch ends ``failed`` with the reason, nothing is sent."""
    agent, chat, rts = await _world(make_agent)
    out = await _switch(chat.id, harness="omp", runtime_id=rts["cloud"].id, model="gpt-x")
    assert out["status"] == "failed"
    assert out["error"] == {"code": "process_start_failed", "detail": "other_runtime"}
    assert [c[0] for c in daemon.calls] == ["state"]
    async with AsyncSession(test_engine) as s:
        assert (await s.get(Chat, chat.id)).runtime_id == rts["local"].id   # F2: pair unchanged


async def test_a_daemon_refusal_fails_the_switch_and_sends_the_queue_back(daemon, make_agent):
    agent, chat, rts = await _world(make_agent)
    await _drift(chat.id, rts["cloud"].id)
    daemon.busy = True
    daemon.handed = [{"text": "one", "origin": "operator"}]
    daemon.switch_answer = {"ok": False, "error": "busy"}
    out = await _switch(chat.id, harness="omp", runtime_id=rts["local"].id, model=None,
                        mode="stop_and_switch")
    assert out["status"] == "failed" and out["error"]["detail"] == "busy"
    assert [c[0] for c in daemon.calls] == ["state", "stop_for_switch", "switch", "prompt"]
    assert daemon.calls[-1][2] == "one"


async def test_model_only_switch_of_a_further_chat_goes_to_its_own_session(daemon, make_agent):
    agent, chat, rts = await _world(make_agent)
    out = await _switch(chat.id, harness="omp", runtime_id=chat.runtime_id, model="qwen-b")
    assert out["path"] == "model_only" and out["status"] == "pending"
    assert daemon.calls[-1] == ("config", str(chat.id), "model", "qwen-b")


async def test_a_refused_model_is_model_not_offered(daemon, make_agent):
    agent, chat, rts = await _world(make_agent)
    daemon.config_answer = {"ok": False, "error": "rpc_error", "detail": "no such model"}
    out = await _switch(chat.id, harness="omp", runtime_id=chat.runtime_id, model="qwen-z")
    assert out["status"] == "failed" and out["error"]["code"] == "model_not_offered"


async def test_a_chat_of_an_agent_without_chat_daemon_is_refused(daemon, make_agent, monkeypatch):
    """A Claude chat moving to omp has no omp daemon of its own (open
    point: who owns a chat after a harness switch)."""
    agent, chat, rts = await _world(make_agent)
    driver = chat_switch._DRIVERS["omp"]
    monkeypatch.setattr(acp_chat_chats, "further_chats_supported", lambda a: False)
    with pytest.raises(chat_switch.DriverUnavailable, match="no_chat_daemon"):
        await driver.start(agent, chat, {"switchEventId": str(uuid.uuid4()), "action": "start",
                                         "to": {"harness": "omp"}})
    assert await driver.is_busy(agent, chat) is False


async def test_the_default_chat_keeps_the_agent_driver(daemon, make_agent, monkeypatch):
    agent, chat, rts = await _world(make_agent)
    seen = []

    class _Default:
        async def set_model(self, a, c, model):
            seen.append(("default", model))

    monkeypatch.setattr(chat_switch, "_DEFAULT_DRIVER", _Default())
    default_chat = Chat(id=uuid.uuid4(), agent_default=True, harness="omp")
    await chat_switch._DRIVERS["omp"].set_model(agent, default_chat, "qwen-b")
    assert seen == [("default", "qwen-b")] and daemon.calls == []


# ── the ctl transport of a switch: the package goes over stdin ─────────────

#: Linux caps ONE exec argument at MAX_ARG_STRLEN = 32 pages = 131 072 bytes.
MAX_ARG_STRLEN = 131_072


class _StdinProc:
    def __init__(self, seen: dict):
        self.returncode = 0
        self._seen = seen

    async def communicate(self, input=None):
        self._seen["input"] = input
        return b'{"ok": true, "switching": true, "handedOver": []}', b""

    def kill(self):  # pragma: no cover — timeout path only
        pass


async def test_a_full_budget_package_never_rides_in_one_exec_argument(monkeypatch):
    """Blocker (critic, #1047): 32k tokens x 4 chars of German chat text is
    ~160 KB as escaped JSON — over MAX_ARG_STRLEN, docker exec would fail
    with E2BIG and the switch would read ``chat_daemon_unreachable``. The
    switch payload goes over stdin (``docker exec -i … --json -``)."""
    seen: dict = {}

    async def _exec(*argv, **kwargs):
        seen["argv"] = list(argv)
        seen["kwargs"] = kwargs
        return _StdinProc(seen)

    monkeypatch.setattr(acp_chat_transport.asyncio, "create_subprocess_exec", _exec)
    package = ("Grüße — Änderung in loader.py; " * 4200)[:128_000]
    payload = {"switchEventId": str(uuid.uuid4()), "action": "start", "package": package}
    transport = acp_chat_transport.DockerCtlTransport("agent-x", chat_id=str(uuid.uuid4()))

    answer = await transport.switch(payload)

    assert answer["ok"] is True
    assert max(len(a.encode()) for a in seen["argv"]) < MAX_ARG_STRLEN
    assert seen["argv"][:3] == ["docker", "exec", "-i"]
    assert seen["argv"][-2:] == ["--json", "-"]
    assert seen["kwargs"].get("stdin") is not None
    sent = json.loads(seen["input"].decode("utf-8"))
    assert sent["package"] == package and len(seen["input"]) > MAX_ARG_STRLEN


async def test_any_payload_too_big_for_one_argument_goes_over_stdin(monkeypatch):
    """Not only ``switch``: whatever op carries more than fits in one exec
    argument goes over stdin; small ones keep their argv shape."""
    seen: dict = {}

    async def _exec(*argv, **kwargs):
        seen["argv"] = list(argv)
        return _StdinProc(seen)

    monkeypatch.setattr(acp_chat_transport.asyncio, "create_subprocess_exec", _exec)
    transport = acp_chat_transport.DockerCtlTransport("agent-x")
    await transport.prompt("x" * 200_000)
    assert seen["argv"][-2:] == ["--json", "-"] and json.loads(seen["input"])["text"] == "x" * 200_000
    seen.clear()
    await transport.prompt("small")
    assert seen["argv"][-3:-1] == ["prompt", "--json"] and seen.get("input") is None
