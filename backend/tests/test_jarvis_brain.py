"""Tests for jarvis_core.brain.JarvisBrain (ADR-061).

The OpenAI HTTP client is a fake that returns scripted chat-completion
responses, so the function-calling loop, tool dispatch, and history are
exercised without any network or API key.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from jarvis_core.brain import JarvisBrain, transcribe_audio
from jarvis_core.channels import TELEGRAM


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeHTTP:
    """Returns scripted responses in order; records posted bodies."""

    def __init__(self, responses: list[dict]):
        self._responses = list(responses)
        self.is_closed = False
        self.calls: list[dict] = []

    async def post(self, url, headers=None, json=None, **kwargs):
        self.calls.append({"url": url, "json": json, "kwargs": kwargs})
        return _FakeResponse(self._responses.pop(0))


def _assistant_tool_call(name: str, args: dict) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                    ],
                }
            }
        ]
    }


def _assistant_text(text: str) -> dict:
    return {"choices": [{"message": {"content": text}}]}


@pytest.mark.asyncio
async def test_brain_plain_text_no_tools():
    http = _FakeHTTP([_assistant_text("Hallo Operator, alles ruhig.")])
    client = AsyncMock()
    brain = JarvisBrain(
        api_key="sk-test", model="gpt-4o-mini", client=client, channel=TELEGRAM,
        system_prompt="SYS", http_client=http,
    )
    result = await brain.respond("wie gehts?")
    assert result.text == "Hallo Operator, alles ruhig."
    assert result.actions == []
    # History = the single visible user+assistant turn.
    assert result.new_turns == [
        {"role": "user", "content": "wie gehts?"},
        {"role": "assistant", "content": "Hallo Operator, alles ruhig."},
    ]
    # No tool ever dispatched → mc_client untouched.
    client.create_task.assert_not_called()


@pytest.mark.asyncio
async def test_brain_executes_tool_then_answers():
    http = _FakeHTTP([
        _assistant_tool_call("create_task", {"title": "Deploy prod", "priority": "high"}),
        _assistant_text("Task „Deploy prod“ für Boss angelegt."),
    ])
    client = AsyncMock()
    client.create_task = AsyncMock(return_value={"ok": True, "task_id": "t42", "assigned_to": "Boss"})

    brain = JarvisBrain(
        api_key="sk-test", model="gpt-4o-mini", client=client, channel=TELEGRAM,
        system_prompt="SYS", http_client=http,
    )
    result = await brain.respond("leg mir einen deploy task an")

    assert "Deploy prod" in result.text
    assert len(result.actions) == 1
    assert result.actions[0]["name"] == "create_task"
    assert result.actions[0]["result"]["task_id"] == "t42"
    client.create_task.assert_awaited_once_with("Deploy prod", "", None, "high")

    # Second OpenAI call must include the tool result message (role=tool).
    second_body = http.calls[1]["json"]
    roles = [m["role"] for m in second_body["messages"]]
    assert "tool" in roles
    assert "assistant" in roles


@pytest.mark.asyncio
async def test_brain_passes_history():
    http = _FakeHTTP([_assistant_text("ok")])
    brain = JarvisBrain(
        api_key="sk-test", model="gpt-4o-mini", client=AsyncMock(), channel=TELEGRAM,
        system_prompt="SYS", http_client=http,
    )
    history = [
        {"role": "user", "content": "frueher"},
        {"role": "assistant", "content": "verstanden"},
    ]
    await brain.respond("jetzt", history=history)
    sent = http.calls[0]["json"]["messages"]
    # system + 2 history + 1 user
    assert sent[0]["role"] == "system"
    assert sent[1]["content"] == "frueher"
    assert sent[-1] == {"role": "user", "content": "jetzt"}


@pytest.mark.asyncio
async def test_brain_telegram_schema_excludes_highlight_graph():
    http = _FakeHTTP([_assistant_text("ok")])
    brain = JarvisBrain(
        api_key="sk-test", model="gpt-4o-mini", client=AsyncMock(), channel=TELEGRAM,
        system_prompt="SYS", http_client=http,
    )
    await brain.respond("hi")
    tools = http.calls[0]["json"]["tools"]
    names = {t["function"]["name"] for t in tools}
    assert "highlight_graph" not in names


@pytest.mark.asyncio
async def test_transcribe_audio_returns_text():
    http = _FakeHTTP([{"text": "  erstelle einen task  "}])
    text = await transcribe_audio(
        b"\x00\x01ogg", filename="voice.ogg", api_key="sk", model="stt", http_client=http,
    )
    assert text == "erstelle einen task"
    assert http.calls[0]["url"].endswith("/audio/transcriptions")
