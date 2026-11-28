"""MCP tools `ext_request` / `ext_status` (scripts/mc-mcp.py) — same
semantics as `mc ext request` / `mc ext status`: skills only, the agent's own
token, a request that waits for the operator (never an install)."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parents[2]
MCMCP_PATH = REPO / "scripts" / "mc-mcp.py"

ME = "11111111-1111-1111-1111-111111111111"
APPROVAL = "66666666-6666-6666-6666-666666666666"
CHAT = "44444444-4444-4444-4444-444444444444"


@pytest.fixture
def mod(monkeypatch):
    monkeypatch.setenv("MC_AGENT_TOKEN", "tok")
    monkeypatch.delenv("MC_CHAT_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    spec = importlib.util.spec_from_file_location("mc_mcp_ext", MCMCP_PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _fn(tool):
    return getattr(tool, "fn", tool)


def _fake(calls, answers=None):
    answers = answers or {}

    def call(method, path, **kw):
        calls.append((method, path, kw.get("json"), kw.get("params")))
        if (method, path) in answers:
            return answers[(method, path)]
        if path == "/agent/me":
            return {"id": ME, "board_id": "b"}
        if method == "POST":
            return {"approval_id": APPROVAL, "status": "pending", "existing": False}
        return {}

    return call


def test_ext_request_posts_a_skill_request_for_myself(mod):
    calls = []
    with patch.object(mod, "_agent_call", side_effect=_fake(calls)):
        out = _fn(mod.ext_request)(
            name="paperless-filing", source="github:anthropic/paperless-filing",
            reason="operator asked for it", ref="v1", chat_id=CHAT,
        )
    posts = [c for c in calls if c[0] == "POST"]
    assert len(posts) == 1
    method, path, body, _ = posts[0]
    assert path == "/agent/install-requests"
    assert body == {
        "type": "skill", "operation": "install", "name": "paperless-filing",
        "source": "github:anthropic/paperless-filing", "target_agent_id": ME,
        "reason": "operator asked for it", "ref": "v1", "chat_id": CHAT,
    }
    assert APPROVAL in out and "operator" in out


def test_ext_request_ignores_a_phantom_chat_env(mod, monkeypatch):
    """Nothing in MC sets MC_CHAT_ID; the tool must not pretend it does."""
    monkeypatch.setenv("MC_CHAT_ID", CHAT)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    calls = []
    with patch.object(mod, "_agent_call", side_effect=_fake(calls)):
        _fn(mod.ext_request)(name="x", source="~/.mc/skills/x", reason="needed now")
    assert "chat_id" not in [c for c in calls if c[0] == "POST"][0][2]


def test_ext_request_reports_backend_errors_plainly(mod):
    calls = []
    answers = {("POST", "/agent/install-requests"): {"error": "HTTP 400: only plain-text skills"}}
    with patch.object(mod, "_agent_call", side_effect=_fake(calls, answers)):
        out = _fn(mod.ext_request)(name="x", source="github:obra/x", reason="needed now")
    assert out.startswith("Error") and "plain-text" in out


def test_ext_status_reads_one_or_all(mod):
    calls = []
    answers = {
        ("GET", f"/agent/install-requests/{APPROVAL}"): {
            "approval_id": APPROVAL, "status": "approved", "action_type": "install_skill",
            "name": "x", "commit": "0123456789abcdef", "failure_reason": None},
        ("GET", "/agent/install-requests"): {"requests": []},
    }
    with patch.object(mod, "_agent_call", side_effect=_fake(calls, answers)):
        one = _fn(mod.ext_status)(request_id=APPROVAL)
        none = _fn(mod.ext_status)()
    assert "approved" in one and "0123456789ab" in one
    assert "No install requests" in none
    assert [c[1] for c in calls] == [f"/agent/install-requests/{APPROVAL}", "/agent/install-requests"]


def test_ext_tools_are_offered_in_agent_mode(mod):
    import asyncio

    names = {t.name for t in asyncio.run(mod.mcp.list_tools())} if hasattr(mod.mcp, "list_tools") else None
    if names is None:  # older fastmcp
        names = set(asyncio.run(mod.mcp.get_tools()).keys())
    assert {"ext_request", "ext_status"} <= names
