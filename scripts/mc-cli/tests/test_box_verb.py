"""Tests for `mc box status` — box facts for agents, read-only (ADR-100).

GETs /api/v1/agent/boxes/status once and prints one block per box: the
verdict first, then each engine with model, running requests and switch
lock. `--json` prints the raw answer. Nothing is written.
"""
import json
import os
import sys
from unittest.mock import MagicMock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from mc_cli import commands  # noqa: E402
from mc_cli.__main__ import build_parser  # noqa: E402

PAYLOAD = {
    "boxes": [
        {
            "slug": "box-a", "display_name": "Box A", "auto_recovery": False,
            "head": None, "verdict": "not_serving",
            "reasons": ["engine_not_serving", "manual_test_possible"],
            "runtimes": [
                {"slug": "rt-a", "model": "model-a", "served_model": None,
                 "engine": "not_serving", "running_requests": 0,
                 "switch_lock": "open", "role": "head", "switching": None,
                 "load_unknown_reason": None, "checked_at": "2026-11-01T10:00:00+00:00"},
            ],
        },
        {
            "slug": "box-b", "display_name": "Box B", "auto_recovery": True,
            "head": None, "verdict": "busy", "reasons": ["requests_running"],
            "runtimes": [
                {"slug": "rt-b", "model": "model-b", "served_model": "model-b",
                 "engine": "serving", "running_requests": 3,
                 "switch_lock": "locked", "role": "head", "switching": None,
                 "load_unknown_reason": None, "checked_at": "2026-11-01T10:00:00+00:00"},
            ],
        },
    ],
    "generated_at": "2026-11-01T10:00:01+00:00",
}


def _client(payload=PAYLOAD):
    client = MagicMock()
    client.calls = []

    def request(method, path, body=None, **kw):
        client.calls.append((method, path))
        return payload

    client.request.side_effect = request
    return client


def _args(argv):
    return build_parser().parse_args(argv)


def test_box_status_reads_the_agent_route_only(capsys):
    client = _client()
    rc = commands._cmd_box(_args(["box", "status"]), client, MagicMock())
    assert rc == 0
    assert client.calls == [("GET", "/api/v1/agent/boxes/status")]


def test_box_status_prints_verdict_and_engine_facts(capsys):
    commands._cmd_box(_args(["box", "status"]), _client(), MagicMock())
    out = capsys.readouterr().out
    assert "box-a" in out and "not_serving" in out
    assert "manual_test_possible" in out
    assert "auto-recovery off" in out
    assert "box-b" in out and "busy" in out
    assert "rt-b" in out and "model-b" in out
    assert "running 3" in out and "switch lock locked" in out


def test_box_status_without_action_defaults_to_status(capsys):
    client = _client()
    rc = commands._cmd_box(_args(["box"]), client, MagicMock())
    assert rc == 0
    assert client.calls == [("GET", "/api/v1/agent/boxes/status")]


def test_box_status_json_prints_the_raw_answer(capsys):
    commands._cmd_box(_args(["box", "status", "--json"]), _client(), MagicMock())
    assert json.loads(capsys.readouterr().out) == PAYLOAD


def test_box_status_one_box_by_slug(capsys):
    commands._cmd_box(_args(["box", "status", "box-b"]), _client(), MagicMock())
    out = capsys.readouterr().out
    assert "box-b" in out
    assert "box-a" not in out


def test_box_status_unknown_slug_is_an_error(capsys):
    rc = commands._cmd_box(_args(["box", "status", "nope"]), _client(), MagicMock())
    assert rc != 0
    assert "nope" in capsys.readouterr().out


def test_box_status_no_boxes(capsys):
    rc = commands._cmd_box(_args(["box", "status"]), _client({"boxes": []}), MagicMock())
    assert rc == 0
    assert "No GPU box" in capsys.readouterr().out
