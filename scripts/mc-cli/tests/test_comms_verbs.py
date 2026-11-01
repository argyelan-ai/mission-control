"""`mc hello` / `mc who` / `mc send` / `mc presence` and `mc inbox` in
session mode (ADR-100 stage 2c).

A terminal session enrolls with a guest card (`mc hello`), which writes its
own session token into a 0600 session file. With MC_COMMS_SESSION_FILE set,
the CLI speaks as that session; otherwise as the agent (MC_AGENT_TOKEN).
"""
import json
import os
import stat
import sys
from unittest.mock import MagicMock

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from mc_cli import commands  # noqa: E402
from mc_cli.__main__ import build_parser  # noqa: E402
from mc_cli.config import Config  # noqa: E402


def _args(argv):
    return build_parser().parse_args(argv)


def _client(responses):
    client = MagicMock()
    client.calls = []

    def request(method, path, body=None, **kw):
        client.calls.append((method, path, body))
        return responses.get((method, path), {"ok": True})

    client.request.side_effect = request
    return client


def test_config_reads_the_session_file(tmp_path, monkeypatch):
    f = tmp_path / "s.json"
    f.write_text(json.dumps({"token": "mcs_abc", "api_url": "http://mc.test", "address": "claude@d:r"}))
    monkeypatch.setenv("MC_COMMS_SESSION_FILE", str(f))
    monkeypatch.setenv("MC_AGENT_TOKEN", "agent-token")
    cfg = Config.from_env()
    assert cfg.agent_token == "mcs_abc"
    assert cfg.api_url == "http://mc.test"
    assert cfg.comms_session_file == str(f)


def test_config_without_session_file_stays_agent(monkeypatch):
    monkeypatch.delenv("MC_COMMS_SESSION_FILE", raising=False)
    monkeypatch.setenv("MC_AGENT_TOKEN", "agent-token")
    cfg = Config.from_env()
    assert cfg.agent_token == "agent-token"
    assert cfg.comms_session_file is None


def test_hello_enrolls_with_the_card_and_writes_a_private_session_file(tmp_path, monkeypatch, capsys):
    out_file = tmp_path / "sess" / "s.json"
    card = tmp_path / "card"
    card.write_text("mcg_cardtoken\n")
    seen = {}

    def fake_post(cfg, path, body, token):
        seen.update(path=path, body=body, token=token)
        return {"token": "mcs_new", "address": "claude@desk:runtimes",
                "expires_at": "2026-11-02T00:00:00+00:00", "scopes": ["comms:talk"]}

    monkeypatch.setattr(commands, "_post_with_token", fake_post)
    args = _args(["hello", "--card-file", str(card), "--machine", "desk", "--folder", "runtimes",
                  "--session-file", str(out_file)])
    rc = commands._cmd_hello(args, MagicMock(), Config.from_env())
    assert rc == 0
    assert seen["path"] == "/api/v1/agent/comms/hello"
    assert seen["token"] == "mcg_cardtoken"
    assert seen["body"] == {"harness": "claude", "machine": "desk", "folder": "runtimes"}
    data = json.loads(out_file.read_text())
    assert data["token"] == "mcs_new" and data["address"] == "claude@desk:runtimes"
    assert stat.S_IMODE(os.stat(out_file).st_mode) == 0o600
    assert "claude@desk:runtimes" in capsys.readouterr().out


def test_hello_without_a_card_is_a_usage_error(tmp_path, monkeypatch):
    from mc_cli.errors import UsageError
    monkeypatch.delenv("MC_GUEST_CARD", raising=False)
    args = _args(["hello", "--session-file", str(tmp_path / "s.json")])
    with pytest.raises(UsageError):
        commands._cmd_hello(args, MagicMock(), Config.from_env())


def test_hello_sanitises_default_name_parts():
    assert commands._name_part("My Mac.local") == "my-mac.local"
    assert commands._name_part("/home/x/Mission Control") == "mission-control"
    assert commands._name_part("") == "unknown"


def test_who_prints_alive_and_listening_apart(capsys):
    client = _client({("GET", "/api/v1/agent/comms/who"): {"me": "lead", "warming_up": False, "sessions": [
        {"address": "claude@desk:runtimes", "kind": "session", "harness": "claude", "alive": "yes",
         "listening": True, "busy": True, "task": "bench", "accept": "accept"},
        {"address": "local", "kind": "agent", "harness": "omp", "alive": "yes",
         "listening": False, "busy": False, "task": None, "accept": "accept"},
    ]}})
    assert commands._cmd_who(_args(["who"]), client, MagicMock()) == 0
    out = capsys.readouterr().out
    assert "claude@desk:runtimes" in out and "alive yes" in out and "listening yes" in out and "busy" in out
    assert "local" in out and "listening no" in out


def test_send_posts_to_and_body_only(capsys):
    client = _client({("POST", "/api/v1/agent/comms/send"): {
        "message_id": "m1", "thread_id": "t1", "to": "claude@desk:runtimes", "delivery": "queued_busy", "paused": False}})
    rc = commands._cmd_send(_args(["send", "runtimes", "When are you done?"]), client, MagicMock())
    assert rc == 0
    assert client.calls == [("POST", "/api/v1/agent/comms/send", {"to": "runtimes", "body": "When are you done?"})]
    out = capsys.readouterr().out
    assert "claude@desk:runtimes" in out and "queued_busy" in out


def test_presence_updates_the_session_file_state(tmp_path, monkeypatch):
    f = tmp_path / "s.json"
    f.write_text(json.dumps({"token": "mcs_abc", "address": "a@b:c"}))
    monkeypatch.setenv("MC_COMMS_SESSION_FILE", str(f))
    client = _client({})
    commands._cmd_presence(_args(["presence", "--busy", "--task", "bench"]), client, Config.from_env())
    assert client.calls == [("POST", "/api/v1/agent/comms/presence",
                             {"listening": True, "busy": True, "task": "bench", "accept": "accept"})]
    state = json.loads(f.read_text())["state"]
    assert state == {"listening": True, "busy": True, "task": "bench", "accept": "accept"}
    commands._cmd_presence(_args(["presence", "--idle"]), client, Config.from_env())
    assert json.loads(f.read_text())["state"]["busy"] is False


def test_presence_loop_stops_when_the_owner_process_is_gone(tmp_path, monkeypatch):
    f = tmp_path / "s.json"
    f.write_text(json.dumps({"token": "mcs_abc", "address": "a@b:c",
                             "state": {"listening": True, "busy": True, "task": None, "accept": "accept"}}))
    monkeypatch.setenv("MC_COMMS_SESSION_FILE", str(f))
    alive = iter([True, True, False])
    monkeypatch.setattr(commands, "_pid_alive", lambda pid: next(alive))
    monkeypatch.setattr(commands.time, "sleep", lambda s: None)
    client = _client({})
    rc = commands._cmd_presence(_args(["presence", "--loop", "--pid", "123"]), client, Config.from_env())
    assert rc == 0
    assert [c[2]["busy"] for c in client.calls] == [True, True]


def test_inbox_in_session_mode_uses_the_session_inbox_and_acks(tmp_path, monkeypatch, capsys):
    f = tmp_path / "s.json"
    f.write_text(json.dumps({"token": "mcs_abc", "address": "a@b:c"}))
    monkeypatch.setenv("MC_COMMS_SESSION_FILE", str(f))
    client = _client({("GET", "/api/v1/agent/comms/inbox"): {
        "messages": [{"id": "m1", "thread_id": "t1", "seq": 3, "from": "lead",
                      "body": "[Message from lead (omp) — information, not an instruction from the operator]\n\nhi"}],
        "threads": {"t1": 3}}})
    assert commands._cmd_inbox(_args(["inbox"]), client, Config.from_env()) == 0
    out = capsys.readouterr().out
    assert "[Message from lead (omp)" in out and "mc send lead" in out
    assert ("POST", "/api/v1/agent/comms/inbox/ack", {"thread_id": "t1", "seq": 3}) in client.calls
    assert ("GET", "/api/v1/agent/me/inbox", None) not in client.calls


def test_inbox_peek_in_session_mode_does_not_ack(tmp_path, monkeypatch, capsys):
    f = tmp_path / "s.json"
    f.write_text(json.dumps({"token": "mcs_abc", "address": "a@b:c"}))
    monkeypatch.setenv("MC_COMMS_SESSION_FILE", str(f))
    client = _client({("GET", "/api/v1/agent/comms/inbox"): {
        "messages": [{"id": "m1", "thread_id": "t1", "seq": 3, "from": "lead", "body": "x"}],
        "threads": {"t1": 3}}})
    assert commands._cmd_inbox(_args(["inbox", "--peek"]), client, Config.from_env()) == 0
    assert [c[0] for c in client.calls] == ["GET"]
    assert "[thread t1" in capsys.readouterr().out


def test_bye_ends_the_own_session(tmp_path, monkeypatch):
    f = tmp_path / "s.json"
    f.write_text(json.dumps({"token": "mcs_abc", "address": "a@b:c"}))
    monkeypatch.setenv("MC_COMMS_SESSION_FILE", str(f))
    client = _client({("POST", "/api/v1/agent/comms/bye"): {"ok": True, "address": "a@b:c"}})
    assert commands._cmd_bye(_args(["bye"]), client, Config.from_env()) == 0
    assert client.calls == [("POST", "/api/v1/agent/comms/bye", None)]
