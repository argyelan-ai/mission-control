"""The journey tests' stub model (e2e/stub-model/server.py).

A stand-in for a local model server (Ollama, LM Studio, vLLM): the
OpenAI-compatible ``/v1/models`` and ``/v1/chat/completions`` MC's probes and
agents talk to, with fixed answers and no model. The first-run journey adds
it as the operator's AI in step S2 and stops it for the "model gone"
sabotage. Standard library only — it runs in a bare ``python:3-alpine``.
"""

from __future__ import annotations

import importlib.util
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[2] / "e2e" / "stub-model" / "server.py"


@pytest.fixture(scope="module")
def stub():
    spec = importlib.util.spec_from_file_location("stub_model_server", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    httpd = mod.make_server("127.0.0.1", 0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield mod, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _get(url: str):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, json.loads(r.read())


def _post(url: str, body: dict):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read().decode()


def test_models_lists_one_chat_model(stub):
    mod, base = stub
    for path in ("/v1/models", "/models"):
        status, body = _get(base + path)
        assert status == 200
        assert body["object"] == "list"
        assert [m["id"] for m in body["data"]] == [mod.MODEL_ID]
        assert body["data"][0]["object"] == "model"


def test_mc_probe_picks_the_stub_model(stub):
    """MC's own selection rules (the runtime check) accept the id."""
    from app.services.agent_runtime_switch import select_probed_model

    mod, base = stub
    _, body = _get(base + "/v1/models")
    assert select_probed_model([m["id"] for m in body["data"]], None) == mod.MODEL_ID


def test_chat_completion_answers_deterministically(stub):
    mod, base = stub
    body = {"model": mod.MODEL_ID, "messages": [{"role": "user", "content": "ping"}]}
    status, ctype, text = _post(base + "/v1/chat/completions", body)
    assert status == 200 and ctype.startswith("application/json")
    data = json.loads(text)
    assert data["object"] == "chat.completion"
    assert data["model"] == mod.MODEL_ID
    msg = data["choices"][0]["message"]
    assert msg["role"] == "assistant" and msg["content"] == mod.REPLY
    assert data["choices"][0]["finish_reason"] == "stop"
    assert data["usage"]["completion_tokens"] > 0
    # same question, same answer
    assert json.loads(_post(base + "/v1/chat/completions", body)[2])["choices"][0]["message"] == msg


def test_chat_completion_streams_when_asked(stub):
    mod, base = stub
    body = {"model": mod.MODEL_ID, "stream": True, "messages": [{"role": "user", "content": "hi"}]}
    status, ctype, text = _post(base + "/v1/chat/completions", body)
    assert status == 200 and ctype.startswith("text/event-stream")
    events = [line[len("data: "):] for line in text.splitlines() if line.startswith("data: ")]
    assert events[-1] == "[DONE]"
    chunks = [json.loads(e) for e in events[:-1]]
    assert "".join(c["choices"][0]["delta"].get("content", "") for c in chunks) == mod.REPLY
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


def test_unknown_path_is_404_and_health_is_ok(stub):
    _, base = stub
    assert _get(base + "/health")[0] == 200
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(base + "/v1/nothing")
    assert e.value.code == 404


def test_bad_json_is_400_not_a_crash(stub):
    _, base = stub
    req = urllib.request.Request(base + "/v1/chat/completions", data=b"{nope", method="POST")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 400
    assert _get(base + "/health")[0] == 200, "the server keeps serving"
