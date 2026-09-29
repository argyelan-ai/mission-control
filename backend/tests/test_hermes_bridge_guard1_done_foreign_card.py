"""Guard 1 — client-side twin of the backend's Guard 2 (erledigte/fremde
Karte incident, 14.09.2026). backend/tests/test_poll_guard_done_foreign_card.py
covers the backend fix; this file covers the bridge-side net: even if a
future regression (or an older backend revision) lets a done/foreign card
slip through as state=new_task, hermes-bridge.py must refuse to paste it.
"""
from __future__ import annotations

import json

import pytest

from tests.test_hermes_bridge import _load_bridge  # reuse the dynamic import helper


@pytest.fixture
def bridge(monkeypatch, tmp_path):
    mod = _load_bridge()
    monkeypatch.setattr(mod, "LAST_TASK_FILE", tmp_path / "last-task-id")
    return mod


AGENT_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
AGENT_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
TASK_ID = "11111111-1111-1111-1111-111111111111"


def _pure_unit_cases():
    return [
        # (task, payload, expected, label)
        ({"status": "done", "assigned_agent_id": AGENT_A}, {"my_agent_id": AGENT_A}, False, "done+self"),
        ({"status": "failed", "assigned_agent_id": AGENT_A}, {"my_agent_id": AGENT_A}, False, "failed+self"),
        ({"status": "in_progress", "assigned_agent_id": AGENT_B}, {"my_agent_id": AGENT_A}, False, "active+foreign"),
        ({"status": "in_progress", "assigned_agent_id": AGENT_A}, {"my_agent_id": AGENT_A}, True, "active+self"),
        ({"status": "inbox", "assigned_agent_id": AGENT_A}, {"my_agent_id": AGENT_A}, True, "inbox+self"),
        # Missing fields (older backend) must fail OPEN, never block a legit dispatch.
        ({"status": "in_progress"}, {"my_agent_id": AGENT_A}, True, "no-assigned-field"),
        ({"status": "in_progress", "assigned_agent_id": AGENT_A}, {}, True, "no-my_agent_id-field"),
    ]


@pytest.mark.parametrize("task,payload,expected,label", _pure_unit_cases())
def test_task_is_dispatchable_for_me(bridge, task, payload, expected, label):
    assert bridge._task_is_dispatchable_for_me(task, payload) is expected, label


class _FakeResp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _run_one_poll_iteration(bridge, monkeypatch, poll_payload: dict):
    """Drives dispatch_poll_loop() through exactly one poll, mocking
    urlopen/deliver_prompt, and returns the deliver_prompt mock so callers
    can assert whether a paste happened.
    """
    env_file = poll_payload.pop("__env_file__")
    monkeypatch.setattr(bridge, "ENV_FILE", env_file)

    call_count = {"n": 0}

    def fake_urlopen(req, timeout=10):
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise SystemExit("break-loop-after-one-iteration")
        return _FakeResp(json.dumps(poll_payload).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(bridge.time, "sleep", lambda *_: None)
    monkeypatch.setattr(bridge, "DISPATCH_POLL_INTERVAL", 0)
    monkeypatch.setattr(bridge, "agent_running", lambda: True)

    from unittest.mock import MagicMock
    deliver_mock = MagicMock(return_value=True)
    monkeypatch.setattr(bridge, "deliver_prompt", deliver_mock)
    monkeypatch.setattr(bridge, "should_reset_session", lambda *a, **kw: False)
    monkeypatch.setattr(bridge, "driver_is_acp", lambda: True)  # skip reset_tui_session path

    with pytest.raises(SystemExit):
        bridge.dispatch_poll_loop()

    return deliver_mock


def test_dispatch_poll_loop_refuses_done_card(bridge, monkeypatch, tmp_path):
    env_file = tmp_path / "agent.env"
    env_file.write_text("MC_BASE_URL=http://test\nMC_AGENT_TOKEN=abc\n")
    poll_payload = {
        "state": "new_task",
        "my_agent_id": AGENT_A,
        "task": {
            "id": TASK_ID,
            "board_id": "22222222-2222-2222-2222-222222222222",
            "title": "Already done",
            "status": "done",
            "assigned_agent_id": AGENT_A,
            "prompt": "DO X",
            "dispatch_attempt_id": "att-1",
        },
        "__env_file__": env_file,
    }
    deliver_mock = _run_one_poll_iteration(bridge, monkeypatch, poll_payload)
    deliver_mock.assert_not_called()


def test_dispatch_poll_loop_refuses_foreign_card(bridge, monkeypatch, tmp_path):
    env_file = tmp_path / "agent.env"
    env_file.write_text("MC_BASE_URL=http://test\nMC_AGENT_TOKEN=abc\n")
    poll_payload = {
        "state": "new_task",
        "my_agent_id": AGENT_A,
        "task": {
            "id": TASK_ID,
            "board_id": "22222222-2222-2222-2222-222222222222",
            "title": "Belongs to Rex",
            "status": "in_progress",
            "assigned_agent_id": AGENT_B,
            "prompt": "DO X",
            "dispatch_attempt_id": "att-1",
        },
        "__env_file__": env_file,
    }
    deliver_mock = _run_one_poll_iteration(bridge, monkeypatch, poll_payload)
    deliver_mock.assert_not_called()


def test_gegenrichtung_dispatch_poll_loop_still_dispatches_legit_own_card(
    bridge, monkeypatch, tmp_path,
):
    """Counter-check: a genuinely open, self-assigned card must still be
    pasted — Guard 1 must not become a second way to swallow legitimate
    (e.g. post-restart) dispatches."""
    env_file = tmp_path / "agent.env"
    env_file.write_text("MC_BASE_URL=http://test\nMC_AGENT_TOKEN=abc\n")
    poll_payload = {
        "state": "new_task",
        "my_agent_id": AGENT_A,
        "task": {
            "id": TASK_ID,
            "board_id": "22222222-2222-2222-2222-222222222222",
            "title": "Still open",
            "status": "in_progress",
            "assigned_agent_id": AGENT_A,
            "prompt": "DO X",
            "dispatch_attempt_id": "att-1",
        },
        "__env_file__": env_file,
    }
    deliver_mock = _run_one_poll_iteration(bridge, monkeypatch, poll_payload)
    deliver_mock.assert_called_once()
