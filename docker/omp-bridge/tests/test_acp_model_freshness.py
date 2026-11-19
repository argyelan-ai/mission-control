#!/usr/bin/env python3
"""Incident 19.10.2026 — a runtime switch must reach long-lived ACP processes.

An omp agent's runtime moved GLM -> DeepSeek. MC re-rendered models.yml + omp.env in
the running container, but the chat daemon (acp_chat.py) and the task bridge
(bridge.py) kept the OMP_MODEL_SELECTOR of their boot environment (six days
old). Every new chat session and every task session was pinned to the old
model, omp answered "Unknown ACP model: mc-openai/GLM-5.3-Flash-EXL3", the
bench task never started, and the chat showed a red protocol error under a
"GPT-5.5" chip (omp's built-in default, since the pin failed).

These tests pin the two layers of the fix (acp_model.py): the selector is
read from the CURRENT omp.env at use time, and a selector the session does
not offer is never sent — the offered model of the same provider is pinned
and the chat says so. Model names here are deliberately neutral.

  pytest docker/omp-bridge/tests/test_acp_model_freshness.py -v
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import acp_chat  # noqa: E402
import acp_client  # noqa: E402
import acp_model  # noqa: E402
import bridge  # noqa: E402
from test_acp_chat import FakeClient, entries_of_type, make_session, read_lines  # noqa: E402

OLD = "mc-openai/Old-Model-1"
NEW = "mc-openai/New-Model-2"


def session_result(*offered: str, current: str = "openai/gpt-5.5", sid: str = "sid-1") -> dict:
    """What omp's session/new returns: the model option lists what models.yml
    (plus omp's built-in catalog) offers RIGHT NOW."""
    return {
        "sessionId": sid,
        "configOptions": [
            {"id": "thinking", "category": "thought_level", "currentValue": "high",
             "options": [{"value": "high", "name": "High"}]},
            {"id": "model", "category": "model", "currentValue": current,
             "options": [{"value": "openai/gpt-5.5", "name": "GPT-5.5"}]
             + [{"value": v, "name": "MC model"} for v in offered]},
        ],
    }


class StrictClient(FakeClient):
    """Like omp: a model that is not offered is refused."""

    def set_config_option(self, session_id, key, value, timeout=30.0):
        self.calls.append(("set_config_option", session_id, key, value))
        if key == "model":
            offered = acp_model.offered_models(self.last_session_result.get("configOptions"))
            if offered and value not in offered:
                raise acp_client.ACPError(
                    f"session/set_config_option failed: Internal error (Unknown ACP model: {value})"
                )
        return {}


def write_env(path: Path, selector: str) -> None:
    model = selector.split("/", 1)[1]
    path.write_text(
        f"OPENAI_BASE_URL=http://engine:8000/v1\nOPENAI_MODEL={model}\n"
        f"OMP_MODEL_SELECTOR={selector}\nOMP_PROFILE=mc-agent\n",
        encoding="utf-8",
    )


def model_pins(client) -> list[str]:
    return [c[3] for c in client.calls if c[0] == "set_config_option" and c[2] == "model"]


# ── layer 1: the selector is read at use time ───────────────────────────────


def test_current_selector_prefers_the_rendered_file_over_the_boot_env(tmp_path):
    env_file = tmp_path / "omp.env"
    write_env(env_file, NEW)
    boot_env = {"OMP_ENV_FILE": str(env_file), "OMP_MODEL_SELECTOR": OLD,
                "OPENAI_MODEL": "Old-Model-1"}
    assert acp_model.current_selector(boot_env) == NEW


def test_current_selector_keeps_an_explicit_override(tmp_path):
    env_file = tmp_path / "omp.env"
    write_env(env_file, NEW)
    env = {"OMP_ENV_FILE": str(env_file), "OMP_ACP_MODEL": "x/pinned-by-hand"}
    assert acp_model.current_selector(env) == "x/pinned-by-hand"


def test_current_selector_falls_back_to_the_env_without_a_file(tmp_path):
    env = {"OMP_ENV_FILE": str(tmp_path / "missing.env"), "OMP_MODEL_SELECTOR": OLD}
    assert acp_model.current_selector(env) == OLD
    assert acp_model.current_selector({"OMP_HOME": str(tmp_path)}) is None


def test_bridge_task_selector_reads_the_current_file(tmp_path, monkeypatch):
    """bridge.py pins each task session from _current_omp_env(), not from
    its boot env (the serve loop's acp_model line)."""
    env_file = tmp_path / "omp.env"
    write_env(env_file, NEW)
    monkeypatch.setenv("OMP_ENV_FILE", str(env_file))
    monkeypatch.setenv("OMP_MODEL_SELECTOR", OLD)
    monkeypatch.setenv("OPENAI_MODEL", "Old-Model-1")
    monkeypatch.delenv("OMP_ACP_MODEL", raising=False)
    assert bridge._acp_model_selector(bridge._current_omp_env()) == NEW


def test_bridge_serve_loop_uses_the_fresh_env():
    """Tripwire: the ACP branch of serve_loop must resolve through the
    CURRENT omp.env (acp_model.current_selector) — the boot-env call was
    the bug."""
    src = (ROOT / "bridge.py").read_text(encoding="utf-8")
    assert "acp_model = _acp_model_mod.current_selector(required=True)" in src
    assert "acp_model = _acp_model_selector(os.environ)" not in src


# ── layer 2: never send a model the session does not offer ──────────────────


def test_choose_model_keeps_an_offered_selector():
    assert acp_model.choose_model(NEW, session_result(NEW)["configOptions"]) == (NEW, None)


def test_choose_model_falls_back_to_the_offered_sibling():
    assert acp_model.choose_model(OLD, session_result(NEW)["configOptions"]) == (NEW, OLD)


def test_choose_model_without_a_sibling_sends_as_is():
    """No better choice: omp decides, its refusal becomes a chat error."""
    opts = session_result()["configOptions"]  # only omp's built-in openai/*
    assert acp_model.choose_model(OLD, opts) == (OLD, None)


def test_choose_model_without_an_option_list_sends_as_is():
    assert acp_model.choose_model(OLD, []) == (OLD, None)


# ── the chat daemon ─────────────────────────────────────────────────────────


def test_daemon_pins_the_current_model_after_a_runtime_switch(tmp_path):
    """The live incident: daemon booted on OLD, omp.env now says NEW, the
    session offers only NEW. The pin must be NEW — no protocol error."""
    env_file = tmp_path / "omp.env"
    write_env(env_file, NEW)
    client = StrictClient(session_result=session_result(NEW))
    sess = make_session(
        tmp_path, client, model=OLD,
        model_resolver=lambda: acp_model.current_selector({"OMP_ENV_FILE": str(env_file)}),
    )
    sess.start()
    try:
        assert model_pins(client) == [NEW]
        lines = read_lines(sess.transcript_path)
        assert not entries_of_type(lines, "custom_message", "chat_error")
        notices = entries_of_type(lines, "custom_message", "chat_notice")
        assert notices and notices[0]["data"] == {
            "code": "model_fallback", "previous": "Old-Model-1", "current": "New-Model-2",
        }
        model = [o for o in sess.state()["configOptions"] if o["id"] == "model"][0]
        assert model["currentValue"] == NEW
    finally:
        sess.close()


def test_daemon_follows_the_file_even_when_the_old_model_is_still_offered(tmp_path):
    """Layer 1 on its own: while both models are offered, only the re-read
    omp.env can tell the daemon that the runtime moved on — the offered
    check cannot (sabotage probe S2 survived without this test)."""
    env_file = tmp_path / "omp.env"
    write_env(env_file, NEW)
    client = StrictClient(session_result=session_result(OLD, NEW))
    sess = make_session(
        tmp_path, client, model=OLD,
        model_resolver=lambda: acp_model.current_selector({"OMP_ENV_FILE": str(env_file)}),
    )
    sess.start()
    try:
        assert model_pins(client) == [NEW]
    finally:
        sess.close()


def test_daemon_never_sends_a_stale_selector_even_if_the_file_is_stale(tmp_path):
    """Generic guard: even when the resolver still says OLD (file not yet
    re-rendered), a session that only offers NEW gets NEW + a notice."""
    client = StrictClient(session_result=session_result(NEW))
    sess = make_session(tmp_path, client, model=OLD, model_resolver=lambda: OLD)
    sess.start()
    try:
        assert OLD not in model_pins(client)
        assert model_pins(client) == [NEW]
        notices = entries_of_type(read_lines(sess.transcript_path), "custom_message", "chat_notice")
        assert [n["data"]["previous"] for n in notices] == ["Old-Model-1"]
    finally:
        sess.close()


def test_daemon_new_session_follows_a_switch_that_happened_while_it_ran(tmp_path):
    """/new after the switch: the daemon started on OLD (offered then), the
    runtime moved to NEW, the operator opens a fresh session."""
    env_file = tmp_path / "omp.env"
    write_env(env_file, OLD)
    client = StrictClient(session_result=session_result(OLD), new_ids=["sid-1", "sid-2"])
    sess = make_session(
        tmp_path, client, model=OLD,
        model_resolver=lambda: acp_model.current_selector({"OMP_ENV_FILE": str(env_file)}),
    )
    sess.start()
    try:
        assert model_pins(client) == [OLD]
        # runtime switch: MC re-renders the file, omp re-reads models.yml
        write_env(env_file, NEW)
        client._session_result = session_result(NEW, sid="sid-2")
        assert sess.new_session()["ok"]
        assert sess.wait_idle(timeout=5)
        assert model_pins(client) == [OLD, NEW]
        lines = read_lines(sess.transcript_path)
        assert not entries_of_type(lines, "custom_message", "chat_error")
        notices = entries_of_type(lines, "custom_message", "chat_notice")
        # The new conversation's own card comes first (incident 21.10.2026).
        fallback = [n for n in notices if n["data"]["code"] == "model_fallback"]
        assert fallback and fallback[0]["data"]["current"] == "New-Model-2"
    finally:
        sess.close()


def test_daemon_same_model_again_is_silent(tmp_path):
    """No switch, no notice — a session/load re-pin must not spam the chat."""
    client = StrictClient(session_result=session_result(NEW))
    sess = make_session(tmp_path, client, model=NEW, model_resolver=lambda: NEW)
    sess.start()
    try:
        assert model_pins(client) == [NEW]
        assert not entries_of_type(read_lines(sess.transcript_path), "custom_message", "chat_notice")
    finally:
        sess.close()


def test_daemon_nothing_offered_keeps_the_refusal_visible(tmp_path):
    client = StrictClient(session_result=session_result())
    sess = make_session(tmp_path, client, model=OLD, model_resolver=lambda: OLD)
    sess.start()
    try:
        assert model_pins(client) == [OLD]
        errs = entries_of_type(read_lines(sess.transcript_path), "custom_message", "chat_error")
        assert errs and "Unknown ACP model" in errs[0]["data"]["detail"]
        assert not entries_of_type(read_lines(sess.transcript_path), "custom_message", "chat_notice")
    finally:
        sess.close()


def test_build_session_wires_the_resolver(tmp_path, monkeypatch):
    env_file = tmp_path / "omp" / "omp.env"
    env_file.parent.mkdir(parents=True)
    write_env(env_file, NEW)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "agent"))
    monkeypatch.setenv("OMP_HOME", str(tmp_path / "omp"))
    monkeypatch.setenv("OMP_ENV_FILE", str(env_file))
    monkeypatch.setenv("OMP_MODEL_SELECTOR", OLD)
    monkeypatch.delenv("OMP_ACP_MODEL", raising=False)
    sess = acp_chat.build_session()
    assert sess._model == NEW
    write_env(env_file, "mc-openai/Third-3")
    assert sess._model_resolver() == "mc-openai/Third-3"


# ── the task bridge (run_acp_once) ──────────────────────────────────────────


class _AliveReader:
    def is_alive(self):
        return True


class TaskClient(StrictClient):
    """FakeClient with the bits run_acp_once touches on top of the chat API."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self._reader = _AliveReader()

    def on_elicitation(self, cb):
        pass


TASK_PROMPT = (
    "# Operating Card — Agent\n\nRole line\n\n---\n\n"
    "# New Task: [Bench] Pagoda — New-Model-2\n\nBuild one index.html.\n"
)


def run_task(tmp_path, client, model):
    lines: list[str] = []
    outcome = bridge.run_acp_once(
        TASK_PROMPT, cwd=str(tmp_path), model=model, max_time=10,
        permission_policy="yolo", task_id="T-bench",
        client_factory=lambda: client,
        transcript_sink=lambda out: lines.extend(out),
    )
    return outcome, [json.loads(l) for l in lines]


def test_task_session_pins_the_offered_model_and_runs_the_prompt(tmp_path):
    client = TaskClient(session_result=session_result(NEW), turns=[{"chunks": ["ok"]}])
    outcome, entries = run_task(tmp_path, client, OLD)
    assert model_pins(client) == [NEW]
    assert any(c[0] == "prompt" for c in client.calls), "the agent never got its task"
    assert not outcome.error_message
    notices = [e for e in entries if e.get("customType") == "chat_notice"]
    assert notices and notices[0]["data"]["current"] == "New-Model-2"


def test_task_session_marks_the_dispatch_prompt_as_from_mc(tmp_path):
    client = TaskClient(session_result=session_result(NEW), turns=[{"chunks": ["ok"]}])
    _, entries = run_task(tmp_path, client, NEW)
    users = [e for e in entries if (e.get("message") or {}).get("role") == "user"]
    assert users, "no prompt line"
    msg = users[0]["message"]
    assert msg["attribution"] == "mc"
    assert msg["origin"] == {"kind": "task", "taskId": "T-bench",
                             "title": "[Bench] Pagoda — New-Model-2"}


def test_task_session_pin_failure_lands_in_the_task_chat(tmp_path):
    """A refused pin used to end the attempt with only the prompt in the
    chat — the reason lived in a tmux pane nobody sees."""

    class Refusing(TaskClient):
        def set_config_option(self, session_id, key, value, timeout=30.0):
            raise acp_client.ACPError("session/set_config_option failed: Internal error (boom)")

    client = Refusing(session_result=session_result(NEW))
    outcome, entries = run_task(tmp_path, client, NEW)
    assert outcome.error_message
    errs = [e for e in entries if e.get("customType") == "chat_error"]
    assert errs and "boom" in errs[0]["data"]["detail"]


def test_task_session_nothing_offered_reports_the_refusal_in_the_task_chat(tmp_path):
    client = TaskClient(session_result=session_result())
    outcome, entries = run_task(tmp_path, client, OLD)
    assert model_pins(client) == [OLD]
    assert not any(c[0] == "prompt" for c in client.calls)
    assert "Unknown ACP model" in (outcome.error_message or "")
    errs = [e for e in entries if e.get("customType") == "chat_error"]
    assert errs and "Unknown ACP model" in errs[0]["data"]["detail"]


if __name__ == "__main__":  # pragma: no cover
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
