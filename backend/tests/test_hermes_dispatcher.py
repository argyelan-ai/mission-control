"""Unit tests for hermes-bridge dispatch poll loop (Phase 25, Plan 25-06).

Covers prompt construction, idempotency, error swallowing.
Bridge module is loaded via importlib from scripts/hermes-bridge.py
(hyphen in filename + outside backend package).
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BRIDGE_PATH = REPO_ROOT / "scripts" / "hermes-bridge.py"


@pytest.fixture
def bridge_module():
    spec = importlib.util.spec_from_file_location("hermes_bridge", BRIDGE_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    # Reset module-level idempotency cache between tests
    mod._last_dispatched_task_id = None
    return mod


def test_build_dispatch_prompt_includes_task_metadata(bridge_module):
    """Prompt MUST contain task_id, board_id, title, body and ACK protocol hints."""
    task = {
        "id": "task-123",
        "board_id": "board-abc",
        "title": "Smoke Test Title",
        "description": "Do the smoke thing.",
    }
    prompt = bridge_module._build_dispatch_prompt(task)
    assert "task-123" in prompt
    assert "board-abc" in prompt
    assert "Smoke Test Title" in prompt
    assert "Do the smoke thing." in prompt
    assert "ACK" in prompt or "in_progress" in prompt
    assert "Update" in prompt and "Evidence" in prompt and "Next" in prompt
    # Plan 25-07 T5: prompt MUST reference mc_patch_task (MCP-first)
    assert "mc_patch_task" in prompt


def test_dispatch_prompt_inlines_args_in_mcp_calls(bridge_module):
    """Plan 25-08 T2: prompt MUST inline task_id + board_id as explicit args
    to mc_patch_task. Shell `export` lines are no-ops in Hermes' LLM context
    (tmux send-keys delivers to TUI input, not a shell), so env-var-style
    propagation does not work — IDs come from the [MC DISPATCH] header and
    must be passed as keyword args."""
    task = {"id": "task-XYZ", "board_id": "board-789", "title": "T", "description": "D"}
    prompt = bridge_module._build_dispatch_prompt(task)
    # No shell exports
    assert "export MC_TASK_ID" not in prompt
    assert "export MC_BOARD_ID" not in prompt
    # Inline args in MCP examples
    assert 'task_id="task-XYZ"' in prompt
    assert 'board_id="board-789"' in prompt
    # Header still canonical source for parsing
    assert "[MC DISPATCH] task_id=task-XYZ" in prompt
    assert "board_id=board-789" in prompt


def test_dispatch_prompt_includes_attempt_id_when_present(bridge_module):
    """Bug fix 2026-05-18: dispatch_attempt_id MUST be surfaced in the prompt
    header AND in the protocol hint. Previously Hermes learned the ID from
    the first 409 — that produced `task.missing_dispatch_attempt_id` events
    on every dispatch (Discord noise).
    """
    task = {
        "id": "task-XYZ",
        "board_id": "board-789",
        "dispatch_attempt_id": "c366d934-f209-4a77-9833-8099f018b1e6",
        "title": "T",
        "description": "D",
    }
    prompt = bridge_module._build_dispatch_prompt(task)
    assert "attempt_id=c366d934-f209-4a77-9833-8099f018b1e6" in prompt
    assert "X-Dispatch-Attempt-Id: c366d934-f209-4a77-9833-8099f018b1e6" in prompt


def test_dispatch_prompt_handles_missing_attempt_id(bridge_module):
    """Missing dispatch_attempt_id MUST NOT crash; renders empty slot."""
    task = {"id": "t1", "board_id": "b1", "title": "x", "description": "y"}
    prompt = bridge_module._build_dispatch_prompt(task)
    assert "attempt_id=" in prompt  # slot exists, value empty


def test_build_dispatch_prompt_never_leaks_literal_token(bridge_module):
    """SECURITY: prompt must NEVER materialize a literal token value.

    Plan 25-07 T5: MCP-first prompt no longer contains curl/Bearer references,
    but the no-leak guard remains as regression protection for T-25-17.
    """
    task = {"id": "t1", "board_id": "b1", "title": "x", "description": "y"}
    prompt = bridge_module._build_dispatch_prompt(task)
    # Token value must NEVER appear (we never set one in this test)
    assert "secret-token-abc-123" not in prompt
    assert "Bearer secret" not in prompt
    assert "Bearer pbkdf2" not in prompt


def test_build_dispatch_prompt_handles_missing_fields(bridge_module):
    """Missing/None fields must not crash — defaults to empty string slot."""
    task = {"id": "t1", "board_id": "b1"}  # no title, no description
    prompt = bridge_module._build_dispatch_prompt(task)
    assert "t1" in prompt
    assert "b1" in prompt
    # Should not raise; result must be a non-empty string
    assert isinstance(prompt, str) and len(prompt) > 50


def test_send_to_tmux_calls_send_keys_twice(bridge_module):
    """tmux dispatch = literal-paste + Enter (two separate send-keys calls)."""
    with patch.object(bridge_module._sp, "run") as mock_run:
        bridge_module._send_to_tmux("hello world")
    assert mock_run.call_count == 2
    first_args = mock_run.call_args_list[0][0][0]
    second_args = mock_run.call_args_list[1][0][0]
    assert "send-keys" in first_args
    assert "-l" in first_args  # literal mode (avoids tmux key-name interpretation)
    assert "hello world" in first_args
    assert "Enter" in second_args


def test_idempotency_cache_prevents_double_dispatch(bridge_module, tmp_path, monkeypatch):
    """Same task id seen twice in successive polls → tmux send fires only once.

    Mirrors dispatch_poll_loop's inner branch: poll → check id != cache → send + cache.
    """
    # Fake env file (loop reads MC_BASE_URL + MC_AGENT_TOKEN from ENV_FILE)
    env_file = tmp_path / "agent.env"
    env_file.write_text("MC_BASE_URL=http://localhost\nMC_AGENT_TOKEN=secret\n")
    monkeypatch.setattr(bridge_module, "ENV_FILE", env_file)

    # /me/poll new_task response shape
    poll_resp = json.dumps({
        "state": "new_task",
        "task": {
            "id": "task-X",
            "board_id": "b1",
            "title": "T",
            "prompt": "full backend prompt",
        },
    }).encode()

    poll_count = {"n": 0}
    send_count = {"n": 0}

    def fake_urlopen(req, timeout=10):
        poll_count["n"] += 1
        m = MagicMock()
        m.read.return_value = poll_resp
        m.__enter__ = lambda self: m
        m.__exit__ = lambda self, *a: None
        return m

    def fake_send(prompt):
        send_count["n"] += 1

    monkeypatch.setattr(bridge_module, "_send_to_tmux", fake_send)
    monkeypatch.setattr(bridge_module, "is_session_running", lambda: True)

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    # Inline 2 iterations of the loop body (don't start the infinite while-True)
    for _ in range(2):
        try:
            req = urllib.request.Request(
                "http://localhost/api/v1/agent/me/poll",
                headers={"Authorization": "Bearer secret"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8")
            payload = json.loads(body) if body.strip() else None
            task = None
            if payload and payload.get("state") == "new_task":
                task = payload.get("task")
            if task and task.get("id") and task["id"] != bridge_module._last_dispatched_task_id:
                bridge_module._send_to_tmux(bridge_module._build_dispatch_prompt(task))
                bridge_module._last_dispatched_task_id = task["id"]
        except Exception:
            pass

    assert poll_count["n"] == 2
    assert send_count["n"] == 1  # Idempotency: dispatched only once


def test_dispatch_poll_loop_function_exists_and_is_callable(bridge_module):
    """Smoke: dispatch_poll_loop is a top-level callable (started via threading.Thread)."""
    assert callable(bridge_module.dispatch_poll_loop)
    assert bridge_module.dispatch_poll_loop.__name__ == "dispatch_poll_loop"
