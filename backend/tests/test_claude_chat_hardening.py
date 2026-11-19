"""Hardening of the per-chat Claude process (chats slice S3, item A2 —
follow-up to the review of item A).

1. ``ClaudeChatRunnerError`` reaches the composer as a clear answer, never a
   500: 404 ``chat_not_found``, 409 ``chat_process_refused`` (by content),
   502 ``chat_process_unreachable`` (container / tmux / disk) — each with
   the runner's code, never its detail.
2. In a further Claude chat, slash commands that move claude to another
   session or touch the shared login (``/clear``, ``/new``, ``/resume``,
   ``/login``, ``/logout``) are refused with 409 ``session_command_blocked``
   — nothing is typed.
3. A chat window without a turn for 15 min is closed; the next prompt
   resumes it. At most 4 chat processes warm and 2 working (process
   measurement 19.11.2026).
4. Template guard: every turn-signal path the agent's settings template
   writes is one ``start-claude.sh --chat`` knows how to rewrite — else a
   chat's turns would land in the agent's own turn signal (window 0's state).

Everything behind ``CHAT_RECORD_ENABLED``. The docker side is a double; no
container is touched.
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import time
import uuid
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

from app.config import settings
from app.services import claude_chat_runner as runner
from app.services.claude_chat_runner import ClaudeChatRunnerError
from tests.test_claude_chat_runner import (  # noqa: F401 — box is a fixture
    START_SCRIPT,
    _Agent,
    _Docker,
    _script,
    box,
)

REPO = Path(__file__).resolve().parents[2]
TEMPLATES = REPO / "backend" / "templates"


@pytest.fixture(autouse=True)
def _clean_registry():
    runner._LAST_TURN.clear()
    yield
    runner._LAST_TURN.clear()


@pytest.fixture
def flag(monkeypatch):
    monkeypatch.setattr(settings, "chat_record_enabled", True, raising=False)


# ══════════════════════════════════════════════════════════════════════════
# (1) error codes → HTTP answers
# ══════════════════════════════════════════════════════════════════════════


def _raised_codes() -> set[str]:
    src = Path(runner.__file__).read_text(encoding="utf-8")
    return set(re.findall(r'ClaudeChatRunnerError\(\s*"([a-z_]+)"', src))


def test_every_runner_code_has_a_deliberate_answer():
    """A new code in the runner must be sorted into 404 / 409 / 502 on
    purpose — the guard goes red otherwise."""
    codes = _raised_codes()
    assert {"chat_not_found", "agent_unreachable", "process_start_failed", "chats_busy"} <= codes
    sorted_codes = runner.NOT_FOUND_CODES | runner.REFUSED_CODES | runner.UNREACHABLE_CODES
    assert codes - sorted_codes == set(), f"unsorted codes: {codes - sorted_codes}"
    assert not (runner.NOT_FOUND_CODES & runner.REFUSED_CODES)
    assert not (runner.REFUSED_CODES & runner.UNREACHABLE_CODES)


async def _claude(make_agent, slug="claude-hard"):
    return await make_agent(name="Claude Hard", slug=slug, agent_runtime="cli-bridge", harness="claude")


@pytest.fixture
def routed(monkeypatch, flag):
    """The router resolves a further chat id for a Claude agent (the real
    resolver admits only omp until the router wiring lands) and records or
    fails the send."""
    from app.routers import agent_chat

    chat_id = str(uuid.uuid4())
    sent: list[tuple[str, str | None]] = []
    state: dict = {"raise": None}

    async def _resolve(session, user, agent, cid):
        return chat_id if cid is not None else None

    async def _send(agent, text, **kw):
        if state["raise"] is not None:
            raise state["raise"]
        sent.append((text, kw.get("chat_id")))
        return None

    monkeypatch.setattr(agent_chat.acp_chat_chats, "resolve_chat", _resolve)
    monkeypatch.setattr(agent_chat, "send_text", _send)
    return {"chat_id": chat_id, "sent": sent, "state": state}


@pytest.mark.parametrize(
    "code,status,reason",
    [
        ("chat_not_found", 404, "chat_not_found"),
        ("agent_unreachable", 502, "chat_process_unreachable"),
        ("process_start_failed", 502, "chat_process_unreachable"),
        ("docker_timeout", 502, "chat_process_unreachable"),
        ("prompt_write_failed", 502, "chat_process_unreachable"),
        ("chats_busy", 409, "chat_process_refused"),
        ("prompt_too_large", 409, "chat_process_refused"),
        ("default_chat", 409, "chat_process_refused"),
        ("something_new", 502, "chat_process_unreachable"),
    ],
)
async def test_runner_errors_map_to_clear_answers(auth_client, make_agent, routed, code, status, reason):
    agent = await _claude(make_agent)
    routed["state"]["raise"] = ClaudeChatRunnerError(code, "/secret/path tmux said no")
    resp = await auth_client.post(f"/api/v1/agents/{agent.id}/chat/input",
                                  json={"text": "hello", "chatId": str(uuid.uuid4())})
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert body["reason"] == reason
    if status != 404:
        assert body["error"] == code
    # The detail is for the log only.
    assert "secret" not in resp.text and "tmux" not in resp.text


# ══════════════════════════════════════════════════════════════════════════
# (2) session-switching commands in a further Claude chat
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("text,command", [
    ("/clear", "/clear"), ("/new", "/new"), (" /RESUME ", "/resume"),
    ("/resume 1234abcd", "/resume"), ("/login", "/login"), ("/logout", "/logout"),
    ("/clear den Cache", "/clear"),
])
async def test_session_switch_commands_are_refused_in_a_further_chat(
        auth_client, make_agent, routed, text, command):
    agent = await _claude(make_agent)
    resp = await auth_client.post(f"/api/v1/agents/{agent.id}/chat/input",
                                  json={"text": text, "chatId": str(uuid.uuid4())})
    assert resp.status_code == 409, resp.text
    assert resp.json() == {"reason": "session_command_blocked", "command": command}
    assert routed["sent"] == []


@pytest.mark.parametrize("text", ["hello", "/compact", "/model sonnet", "/resumee", "please /clear it"])
async def test_other_text_in_a_further_chat_goes_through(auth_client, make_agent, routed, text):
    agent = await _claude(make_agent)
    resp = await auth_client.post(f"/api/v1/agents/{agent.id}/chat/input",
                                  json={"text": text, "chatId": str(uuid.uuid4())})
    assert resp.status_code == 204, resp.text
    assert routed["sent"] == [(text, routed["chat_id"])]


async def test_default_chat_and_other_harnesses_keep_their_commands(auth_client, make_agent, routed):
    """The agent's own chat (no chat id) and an omp chat: unchanged — the
    gate is only for a further Claude chat."""
    agent = await _claude(make_agent)
    resp = await auth_client.post(f"/api/v1/agents/{agent.id}/chat/input", json={"text": "/resume"})
    assert resp.status_code == 204, resp.text
    omp = await make_agent(name="Omp Hard", slug="omp-hard", agent_runtime="cli-bridge", harness="omp")
    resp = await auth_client.post(f"/api/v1/agents/{omp.id}/chat/input",
                                  json={"text": "/login", "chatId": str(uuid.uuid4())})
    assert resp.status_code == 204, resp.text
    assert [t for t, _ in routed["sent"]] == ["/resume", "/login"]


async def test_gate_is_off_with_the_flag_off(auth_client, make_agent, routed, monkeypatch):
    monkeypatch.setattr(settings, "chat_record_enabled", False, raising=False)
    agent = await _claude(make_agent)
    resp = await auth_client.post(f"/api/v1/agents/{agent.id}/chat/input",
                                  json={"text": "/logout", "chatId": str(uuid.uuid4())})
    assert resp.status_code == 204, resp.text


def test_session_switch_command_rule():
    assert runner.session_switch_command("/logout") == "/logout"
    assert runner.session_switch_command("/clear\nmore") == "/clear"
    assert runner.session_switch_command("/exit") is None
    assert runner.session_switch_command("") is None
    assert runner.session_switch_command(None) is None
    assert runner.SESSION_SWITCH_COMMANDS == {"clear", "new", "resume", "login", "logout"}


# ══════════════════════════════════════════════════════════════════════════
# (3) idle close, warm / working bounds
# ══════════════════════════════════════════════════════════════════════════


def test_bounds_are_the_measured_ones():
    """prozess-messung 19.11.2026 §4: 15 min idle, 4 warm, 2 working."""
    assert runner.IDLE_CLOSE_SECONDS == 15 * 60
    assert runner.MAX_WARM_CHATS == 4
    assert runner.MAX_WORKING_CHATS == 2


def _age(box, chat: str, seconds: float, *, signal: str | None = "stop", native: str | None = None,
         slug: str = "rex") -> None:
    """Gives a chat a folder whose last sign of life is ``seconds`` ago."""
    home = box["tmp"] / ".mc" / "agents" / slug / "claude-config"
    chat_dir = home / "chats" / chat
    chat_dir.mkdir(parents=True, exist_ok=True)
    when = time.time() - seconds
    sig = chat_dir / runner.TURN_SIGNAL_FILENAME
    sig.write_text(f"{int(when)} submit\n" + (f"{int(when)} {signal}\n" if signal == "stop" else ""))
    os.utime(sig, (when, when))
    if native:
        folder = chat_dir / "projects" / "-home-agent"
        folder.mkdir(parents=True, exist_ok=True)
        f = folder / f"{native}.jsonl"
        f.write_text('{"type": "user"}\n')
        os.utime(f, (when, when))


def _open(docker, slug="rex"):
    return sorted(n for s, n in docker.windows if s == f"chats-{slug}")


async def test_idle_window_closes_busy_one_stays(box, monkeypatch, flag):
    old, fresh = str(uuid.uuid4()), str(uuid.uuid4())
    docker = _Docker(windows=(("rex", "0"), ("chats-rex", f"chat-{old}"), ("chats-rex", f"chat-{fresh}")))
    monkeypatch.setattr(runner, "_docker", docker)
    _age(box, old, 16 * 60, native=str(uuid.uuid4()))
    _age(box, fresh, 60)

    closed = await runner.close_idle_chats()
    assert closed == [f"chats-rex:chat-{old}"]
    assert _open(docker) == [f"chat-{fresh}"]
    # The agent's own window is never a candidate.
    assert ("rex", "0") in docker.windows


async def test_a_long_running_turn_is_not_idle(box, monkeypatch, flag):
    """The session file grows during a turn: that counts as life even if the
    last signal is 20 min old."""
    chat, native = str(uuid.uuid4()), str(uuid.uuid4())
    docker = _Docker(windows=(("chats-rex", f"chat-{chat}"),))
    monkeypatch.setattr(runner, "_docker", docker)
    _age(box, chat, 20 * 60, signal=None, native=native)
    os.utime(runner.transcript_path("rex", chat, native))  # written just now
    assert await runner.close_idle_chats() == []
    assert runner.chat_is_working("rex", chat)


async def test_idle_close_waits_for_the_flag(box, monkeypatch):
    monkeypatch.setattr(settings, "chat_record_enabled", False, raising=False)
    chat = str(uuid.uuid4())
    docker = _Docker(windows=(("chats-rex", f"chat-{chat}"),))
    monkeypatch.setattr(runner, "_docker", docker)
    _age(box, chat, 60 * 60)
    assert await runner.close_idle_chats() == []
    assert _open(docker) == [f"chat-{chat}"]


async def test_closed_chat_resumes_on_the_next_prompt(box, monkeypatch, flag):
    chat, native = str(uuid.uuid4()), str(uuid.uuid4())
    docker = _Docker(windows=(("rex", "0"), ("chats-rex", f"chat-{chat}")))
    monkeypatch.setattr(runner, "_docker", docker)
    _age(box, chat, 30 * 60, native=native)
    assert await runner.close_idle_chats() == [f"chats-rex:chat-{chat}"]

    async def _plan(agent, chat_id):
        return {"nativeSessionId": native, "action": "resume"}

    monkeypatch.setattr(runner, "load_chat_plan", _plan)
    monkeypatch.setattr(runner, "ensure_idle_sweeper", lambda: None)
    target = await runner.open_chat(_Agent(), chat)
    assert target == f"chats-rex:chat-{chat}"
    reopen = docker.opens()[-1]
    assert reopen[reopen.index("--resume") + 1] == native
    assert "--session-id" not in reopen
    # A turn just went out: the sweep leaves it alone now.
    assert await runner.close_idle_chats() == []


async def test_a_send_after_the_check_keeps_its_window(box, monkeypatch, flag):
    """The sweep re-checks under the agent's lock: a prompt that came in
    between its look and its close keeps the window."""
    chat = str(uuid.uuid4())

    class _Late(_Docker):
        async def __call__(self, argv):
            answer = await super().__call__(argv)
            if "list-windows" in argv:
                runner.note_turn("rex", chat)
            return answer

    docker = _Late(windows=(("chats-rex", f"chat-{chat}"),))
    monkeypatch.setattr(runner, "_docker", docker)
    _age(box, chat, 20 * 60)
    assert await runner.close_idle_chats() == []
    assert not [c for c in docker.calls if "kill-window" in c]


def test_working_signal(box):
    chat = str(uuid.uuid4())
    _age(box, chat, 30, signal=None)
    assert runner.chat_is_working("rex", chat)
    _age(box, chat, 30, signal="stop")
    assert not runner.chat_is_working("rex", chat)
    # An interrupted turn (no stop) older than the idle time is not working.
    _age(box, chat, 20 * 60, signal=None)
    assert not runner.chat_is_working("rex", chat)
    assert not runner.chat_is_working("rex", str(uuid.uuid4()))


async def test_fifth_chat_closes_the_least_recently_used(box, monkeypatch, flag):
    chats = [str(uuid.uuid4()) for _ in range(4)]
    docker = _Docker(windows=[("rex", "0")] + [("chats-rex", f"chat-{c}") for c in chats])
    monkeypatch.setattr(runner, "_docker", docker)
    for i, c in enumerate(chats):
        _age(box, c, 600 - i * 60)  # chats[0] is the oldest
    new = str(uuid.uuid4())
    await runner.ensure_window(_Agent(), new, str(uuid.uuid4()))
    assert _open(docker) == sorted(f"chat-{c}" for c in chats[1:] + [new])
    assert len(_open(docker)) == runner.MAX_WARM_CHATS


async def test_warm_bound_counts_every_agent(box, monkeypatch, flag):
    """The bound is over the Docker VM, not per agent: three chats of one
    agent and one of another fill it."""
    rex = [str(uuid.uuid4()) for _ in range(3)]
    other = str(uuid.uuid4())
    docker = _Docker(windows=[("chats-rex", f"chat-{c}") for c in rex] + [("chats-ivy", f"chat-{other}")])
    monkeypatch.setattr(runner, "_docker", docker)
    for c in rex:
        _age(box, c, 120)
    _age(box, other, 900, slug="ivy")
    await runner.ensure_window(_Agent(), str(uuid.uuid4()), str(uuid.uuid4()))
    assert _open(docker, "ivy") == []
    assert len(_open(docker)) == 4


async def test_full_and_all_recent_refuses_instead_of_closing(box, monkeypatch, flag):
    chats = [str(uuid.uuid4()) for _ in range(4)]
    docker = _Docker(windows=[("chats-rex", f"chat-{c}") for c in chats])
    monkeypatch.setattr(runner, "_docker", docker)
    for c in chats:
        _age(box, c, 5)  # touched within the grace time
    with pytest.raises(ClaudeChatRunnerError) as e:
        await runner.ensure_window(_Agent(), str(uuid.uuid4()), str(uuid.uuid4()))
    assert e.value.code == "chats_busy"
    assert not [c for c in docker.calls if "kill-window" in c]
    assert len(docker.opens()) == 0


async def test_third_working_chat_is_refused(box, monkeypatch, flag):
    a, b, c = (str(uuid.uuid4()) for _ in range(3))
    docker = _Docker()
    monkeypatch.setattr(runner, "_docker", docker)
    monkeypatch.setattr(runner, "ensure_idle_sweeper", lambda: None)

    async def _plan(agent, chat_id):
        return {"nativeSessionId": str(uuid.uuid4()), "action": "start"}

    monkeypatch.setattr(runner, "load_chat_plan", _plan)
    _age(box, a, 10, signal=None)
    _age(box, b, 10, signal=None)
    with pytest.raises(ClaudeChatRunnerError) as e:
        await runner.open_chat(_Agent(), c)
    assert e.value.code == "chats_busy"
    assert docker.opens() == []
    # A follow-up to a chat that is itself mid-turn still goes (claude queues it).
    await runner.open_chat(_Agent(), a)
    # Once one finishes, the third may start.
    _age(box, b, 10, signal="stop")
    await runner.open_chat(_Agent(), c)


async def test_sweeper_starts_once_and_only_with_the_flag(monkeypatch):
    monkeypatch.setattr(settings, "chat_record_enabled", False, raising=False)
    assert runner.ensure_idle_sweeper() is None
    monkeypatch.setattr(settings, "chat_record_enabled", True, raising=False)
    task = runner.ensure_idle_sweeper()
    try:
        assert task is not None and runner.ensure_idle_sweeper() is task
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ══════════════════════════════════════════════════════════════════════════
# (4) template guard: the agent's turn signal vs start-claude.sh
# ══════════════════════════════════════════════════════════════════════════


def _agent_signals() -> tuple[str, ...]:
    text = START_SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"^AGENT_SIGNALS = (\(.*\))$", text, re.M)
    assert match, "AGENT_SIGNALS not found in start-claude.sh"
    return ast.literal_eval(match.group(1))


def _rendered_settings() -> str:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)))
    return env.get_template("cli_agent_settings.json.j2").render(
        system_prompt="card", model="opus", enabled_plugins={}, extra_marketplaces={},
        turn_signal_hooks=True, status_line=True,
    )


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)


def test_template_turn_signal_paths_are_ones_start_claude_rewrites():
    """Every ``…/.turn-signal`` the template writes is spelled exactly as one
    of start-claude.sh's AGENT_SIGNALS. Another spelling (``$HOME/./``,
    ``/home/agent//``, a new file name) would slip past the rewrite: a
    further chat's turns would land in window 0's turn state."""
    data = json.loads(_rendered_settings())
    paths = [p for s in _strings(data) for p in re.findall(r"[^\s'\"]*turn-signal[^\s'\"]*", s)]
    assert paths, "template writes no turn signal — the guard has nothing to check"
    known = set(_agent_signals())
    assert set(paths) <= known, f"unknown turn-signal spelling(s): {set(paths) - known}"


def test_rendered_settings_in_a_chat_never_write_the_agents_signal(box):
    """End to end with the REAL template and the REAL start-claude.sh: in the
    chat's settings no hook names the agent's file any more."""
    (box["home"] / "settings.json").write_text(_rendered_settings(), encoding="utf-8")
    chat = str(uuid.uuid4())
    _script(box, "--chat", chat, "--resume", "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
    chat_dir = box["home"] / "chats" / chat
    data = json.loads((chat_dir / "settings.json").read_text())
    own = f"{chat_dir}/.turn-signal"
    hooks = [s for s in _strings(data.get("hooks", {})) if "turn-signal" in s]
    assert len(hooks) == 2
    for command in hooks:
        assert command.replace(own, "").count("turn-signal") == 0, command
