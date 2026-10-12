"""scripts/mc-mcp.py has two kinds of users (ADR-085 §4):

* the operator (Claude Code sessions in the MC folder, no agent token) — the
  admin helper `_api()` is legitimate there, every tool is offered;
* agents (host agents that pass MC_AGENT_TOKEN / MC_AGENT_ENV_FILE) — they
  must only ever act AS the agent: agent-scoped routes, never the operator
  `.env`, never the self-minted admin JWT, and the operator-only tools
  (task creation, phase completion, host power) are not offered at all.

These tests fake httpx at the transport level, so ANY admin call — through
`_api()` or a new direct request — shows up as a request that does not carry
the agent token or does not hit `/api/v1/agent/…`.
"""
from __future__ import annotations

import asyncio
import importlib.util
import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MCMCP_PATH = REPO / "scripts" / "mc-mcp.py"

AGENT_TOKEN = "tok-agent-test"
BOARD = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
TASK = "11111111-2222-3333-4444-555555555555"
OTHER_TASK = "99999999-2222-3333-4444-555555555555"
ITEM = "77777777-8888-9999-aaaa-bbbbbbbbbbbb"
PROJECT = "cccccccc-dddd-eeee-ffff-000000000000"

OPERATOR_ONLY = {
    "mc_create_task",
    "mc_complete_phase",
    "mc_restart_worker",
    "mc_logs",
    "mc_system_status",
}


def _load(tmp_path: Path):
    """Load a copy of mc-mcp.py whose project dir holds an operator `.env`
    with an admin secret — so a test can see whether it gets read."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(MCMCP_PATH, scripts / "mc-mcp.py")
    (tmp_path / ".env").write_text("JWT_SECRET_KEY=operator-admin-secret\n")
    spec = importlib.util.spec_from_file_location("mc_mcp_mode_test", scripts / "mc-mcp.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def agent_mod(monkeypatch, tmp_path):
    monkeypatch.setenv("MC_AGENT_TOKEN", AGENT_TOKEN)
    monkeypatch.delenv("MC_AGENT_ENV_FILE", raising=False)
    return _load(tmp_path)


@pytest.fixture
def agent_mod_via_env_file(monkeypatch, tmp_path):
    env_file = tmp_path / "agent.env"
    env_file.write_text(f"MC_AGENT_TOKEN={AGENT_TOKEN}\n")
    monkeypatch.delenv("MC_AGENT_TOKEN", raising=False)
    monkeypatch.setenv("MC_AGENT_ENV_FILE", str(env_file))
    return _load(tmp_path)


@pytest.fixture
def operator_mod(monkeypatch, tmp_path):
    monkeypatch.delenv("MC_AGENT_TOKEN", raising=False)
    monkeypatch.delenv("MC_AGENT_ENV_FILE", raising=False)
    return _load(tmp_path)


def _unwrap(tool):
    return getattr(tool, "fn", tool)


def _tool_names(mod) -> set[str]:
    return {t.name for t in asyncio.run(mod.mcp.list_tools())}


class _Resp:
    def __init__(self, status: int, payload):
        self.status_code = status
        self._payload = payload
        self.text = str(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            req = httpx.Request("GET", "http://test")
            raise httpx.HTTPStatusError(
                "err", request=req,
                response=httpx.Response(self.status_code, text=self.text, request=req),
            )

    def json(self):
        return self._payload


class FakeMC:
    """Records every HTTP request; answers agent-scoped routes of one board."""

    def __init__(self, overrides: dict | None = None):
        self.calls: list[dict] = []
        self.overrides = overrides or {}

    def route(self, method: str, path: str):
        key = (method, path)
        if key in self.overrides:
            return self.overrides[key]
        a = "/api/v1/agent"
        table = {
            ("GET", f"{a}/me"): (200, {"id": "agent-1", "board_id": BOARD}),
            ("GET", f"{a}/boards/{BOARD}/agents"): (200, [
                {"id": "agent-1-uuid", "name": "Builder", "role": "developer",
                 "provision_status": "ready", "is_board_lead": False},
            ]),
            ("GET", f"{a}/boards/{BOARD}/tasks"): (200, [
                {"id": TASK, "title": "Card one", "status": "in_progress",
                 "assigned_agent_name": "Builder"},
                {"id": OTHER_TASK, "title": "Card two", "status": "review",
                 "assigned_agent_name": None},
            ]),
            ("GET", f"{a}/boards/{BOARD}/tasks/{TASK}/detail"): (200, {
                "id": TASK, "title": "Card one", "status": "in_progress",
                "assigned_agent_name": "Builder", "priority": "high",
                "created_at": "2026-10-01", "description": "DESC-MARKER",
            }),
            ("GET", f"{a}/boards/{BOARD}/tasks/{TASK}/comments"): (200, [
                {"content": "COMMENT-MARKER", "author_agent_name": "Builder"},
            ]),
            ("GET", f"{a}/boards/{BOARD}/tasks/{TASK}/checklist"): (200, [
                {"id": ITEM, "title": "Step", "status": "pending"},
            ]),
            ("POST", f"{a}/boards/{BOARD}/tasks/{TASK}/checklist"): (201, [
                {"id": ITEM, "title": "Step", "status": "pending"},
            ]),
            ("PATCH", f"{a}/boards/{BOARD}/tasks/{TASK}/checklist/{ITEM}"): (200, {"ok": True}),
            ("GET", f"{a}/boards/{BOARD}/tasks/{TASK}/deliverables"): (200, [
                {"title": "Report", "deliverable_type": "document", "scope": "task"},
            ]),
            ("POST", f"{a}/boards/{BOARD}/tasks/{TASK}/deliverables"): (201, {
                "id": "deliv-1", "title": "Report",
            }),
            ("PATCH", f"{a}/boards/{BOARD}/tasks/{TASK}"): (200, {"id": TASK, "status": "review"}),
            ("POST", f"{a}/boards/{BOARD}/tasks/{TASK}/comments"): (201, {"id": "c1"}),
            ("GET", f"{a}/projects/{PROJECT}"): (200, {
                "id": PROJECT, "name": "Proj", "status": "active",
                "briefing_doc": "BRIEF-MARKER",
                "phases": [{"title": "P1", "status": "active", "order": 0}],
            }),
        }
        return table.get(key, (404, {"detail": "not found"}))

    def install(self, mod, monkeypatch):
        fake = self

        class _Client:
            def __init__(self, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def request(self, method, url, headers=None, params=None, json=None, **kw):
                path = url.split("://", 1)[-1]
                path = path[path.index("/"):] if "/" in path else path
                fake.calls.append({
                    "method": method, "path": path, "headers": dict(headers or {}),
                    "params": params, "json": json,
                })
                status, payload = fake.route(method, path)
                return _Resp(status, payload)

            def get(self, url, **kw):
                return self.request("GET", url, **kw)

        monkeypatch.setattr(mod.httpx, "Client", _Client)
        return self

    # ── assertions ──
    def assert_agent_only(self):
        assert self.calls, "no request was made at all"
        for c in self.calls:
            assert c["headers"].get("Authorization") == f"Bearer {AGENT_TOKEN}", (
                f"request without the agent token (admin path?): {c['method']} {c['path']}"
            )
            assert c["path"].startswith("/api/v1/agent/"), (
                f"non agent-scoped route in agent mode: {c['method']} {c['path']}"
            )

    def paths(self, method: str | None = None) -> list[str]:
        return [c["path"] for c in self.calls if method is None or c["method"] == method]


# ── Mode detection, tool registration, no operator secret ───────────────


def test_agent_mode_hides_operator_only_tools(agent_mod):
    names = _tool_names(agent_mod)
    assert not (names & OPERATOR_ONLY), f"operator-only tools offered to an agent: {names & OPERATOR_ONLY}"
    for expected in ("mc_tasks", "mc_task_detail", "mc_patch_task", "mc_checklist",
                     "mc_register_deliverable", "mc_get_deliverables", "mc_agents",
                     "mc_get_project", "mc_inbox", "mc_msg"):
        assert expected in names


def test_agent_mode_via_env_file_hides_operator_only_tools(agent_mod_via_env_file):
    assert not (_tool_names(agent_mod_via_env_file) & OPERATOR_ONLY)


def test_operator_mode_offers_every_tool(operator_mod):
    assert OPERATOR_ONLY <= _tool_names(operator_mod)


def _assert_no_operator_secret(mod):
    assert "JWT_SECRET_KEY" not in mod.ENV
    with pytest.raises(RuntimeError):
        mod._get_token()


def test_agent_mode_never_reads_operator_env(agent_mod):
    _assert_no_operator_secret(agent_mod)


def test_agent_mode_via_env_file_never_reads_operator_env(agent_mod_via_env_file):
    _assert_no_operator_secret(agent_mod_via_env_file)


def test_operator_mode_still_reads_env(operator_mod):
    assert operator_mod.ENV.get("JWT_SECRET_KEY") == "operator-admin-secret"


def test_admin_helper_refuses_in_agent_mode(agent_mod, monkeypatch):
    fake = FakeMC().install(agent_mod, monkeypatch)
    r = agent_mod._api("GET", "/boards")
    assert "error" in r
    assert fake.calls == [], "admin helper sent a request in agent mode"


# ── Every tool goes agent-scoped with the agent token ───────────────────


def test_agent_mc_agents_uses_own_board(agent_mod, monkeypatch):
    fake = FakeMC().install(agent_mod, monkeypatch)
    out = _unwrap(agent_mod.mc_agents)()
    fake.assert_agent_only()
    assert f"/api/v1/agent/boards/{BOARD}/agents" in fake.paths("GET")
    assert "Builder" in out


def test_agent_mc_tasks_lists_own_board(agent_mod, monkeypatch):
    fake = FakeMC().install(agent_mod, monkeypatch)
    out = _unwrap(agent_mod.mc_tasks)(status="in_progress")
    fake.assert_agent_only()
    lists = [c for c in fake.calls if c["path"] == f"/api/v1/agent/boards/{BOARD}/tasks"]
    assert lists and lists[0]["params"].get("status") == "in_progress"
    assert "Card one" in out


def test_agent_mc_task_detail_resolves_short_id(agent_mod, monkeypatch):
    fake = FakeMC().install(agent_mod, monkeypatch)
    out = _unwrap(agent_mod.mc_task_detail)(TASK[:8])
    fake.assert_agent_only()
    assert f"/api/v1/agent/boards/{BOARD}/tasks/{TASK}/detail" in fake.paths("GET")
    assert "DESC-MARKER" in out
    assert "COMMENT-MARKER" in out


def test_agent_mc_checklist_short_id(agent_mod, monkeypatch):
    fake = FakeMC().install(agent_mod, monkeypatch)
    out = _unwrap(agent_mod.mc_checklist)(TASK[:8])
    fake.assert_agent_only()
    assert "Step" in out


def test_agent_mc_checklist_add_and_done_short_ids(agent_mod, monkeypatch):
    fake = FakeMC().install(agent_mod, monkeypatch)
    _unwrap(agent_mod.mc_checklist_add)(TASK[:8], ["Step"])
    out = _unwrap(agent_mod.mc_checklist_done)(TASK[:8], ITEM[:8])
    fake.assert_agent_only()
    assert f"/api/v1/agent/boards/{BOARD}/tasks/{TASK}/checklist/{ITEM}" in fake.paths("PATCH")
    assert "done" in out


def test_agent_mc_get_deliverables(agent_mod, monkeypatch):
    fake = FakeMC().install(agent_mod, monkeypatch)
    out = _unwrap(agent_mod.mc_get_deliverables)(TASK, BOARD)
    fake.assert_agent_only()
    assert "Report" in out


def test_agent_mc_register_deliverable_goes_agent_scoped(agent_mod, monkeypatch):
    fake = FakeMC().install(agent_mod, monkeypatch)
    out = _unwrap(agent_mod.mc_register_deliverable)(
        task_id=TASK, board_id=BOARD, title="Report",
        deliverable_type="document", content="# Body", tags="a,b",
    )
    fake.assert_agent_only()
    posts = [c for c in fake.calls if c["method"] == "POST"]
    assert [p["path"] for p in posts] == [f"/api/v1/agent/boards/{BOARD}/tasks/{TASK}/deliverables"]
    assert posts[0]["json"]["content"] == "# Body"
    assert posts[0]["json"]["tags"] == ["a", "b"]
    assert "Deliverable registriert" in out


def test_agent_mc_register_deliverable_resolves_board_and_short_id(agent_mod, monkeypatch):
    fake = FakeMC().install(agent_mod, monkeypatch)
    _unwrap(agent_mod.mc_register_deliverable)(
        task_id=TASK[:8], board_id="", title="Report", content="x",
    )
    fake.assert_agent_only()
    assert f"/api/v1/agent/boards/{BOARD}/tasks/{TASK}/deliverables" in fake.paths("POST")


def test_agent_mc_get_project_agent_route(agent_mod, monkeypatch):
    fake = FakeMC().install(agent_mod, monkeypatch)
    out = _unwrap(agent_mod.mc_get_project)(PROJECT)
    fake.assert_agent_only()
    assert f"/api/v1/agent/projects/{PROJECT}" in fake.paths("GET")
    assert "BRIEF-MARKER" in out
    assert "P1" in out


def test_agent_mc_patch_task_short_id_status_and_comment(agent_mod, monkeypatch):
    fake = FakeMC().install(agent_mod, monkeypatch)
    out = _unwrap(agent_mod.mc_patch_task)(TASK[:8], status="review", comment="Update: x")
    fake.assert_agent_only()
    assert "Status → review" in out
    assert "Kommentar hinzugefügt" in out


def test_agent_mc_patch_task_reflection_comment_goes_before_status(agent_mod, monkeypatch):
    """The reflection gate checks for a reflection comment when the card
    moves to review — so the reflection must be posted first."""
    fake = FakeMC().install(agent_mod, monkeypatch)
    out = _unwrap(agent_mod.mc_patch_task)(
        TASK, board_id=BOARD, status="review",
        comment="Was gelernt: x", comment_type="reflection",
    )
    fake.assert_agent_only()
    writes = [(c["method"], c["path"], c["json"]) for c in fake.calls if c["method"] != "GET"]
    assert writes[0] == ("POST", f"/api/v1/agent/boards/{BOARD}/tasks/{TASK}/comments",
                         {"content": "Was gelernt: x", "comment_type": "reflection"})
    assert writes[1][0] == "PATCH"
    assert "Status → review" in out


# ── Honest errors, never an admin fallback ──────────────────────────────


def test_agent_403_is_reported_not_retried_as_admin(agent_mod, monkeypatch):
    fake = FakeMC(overrides={
        ("POST", f"/api/v1/agent/boards/{BOARD}/tasks/{TASK}/deliverables"):
            (403, {"detail": "Missing scopes: tasks:write"}),
    }).install(agent_mod, monkeypatch)
    out = _unwrap(agent_mod.mc_register_deliverable)(
        task_id=TASK, board_id=BOARD, title="Report", content="x",
    )
    fake.assert_agent_only()
    assert "403" in out
    assert "registriert" not in out


def test_agent_mode_without_token_in_file_is_an_honest_error(monkeypatch, tmp_path):
    env_file = tmp_path / "agent.env"
    env_file.write_text("MC_BASE_URL=http://x\n")  # no token inside
    monkeypatch.delenv("MC_AGENT_TOKEN", raising=False)
    monkeypatch.setenv("MC_AGENT_ENV_FILE", str(env_file))
    mod = _load(tmp_path)
    fake = FakeMC().install(mod, monkeypatch)
    out = _unwrap(mod.mc_patch_task)(TASK, board_id=BOARD, status="review")
    assert fake.calls == [], "no token → must not call anything (and never as admin)"
    assert "Token" in out or "token" in out
    assert "Status → review" not in out
    assert not (_tool_names(mod) & OPERATOR_ONLY)


def test_agent_tasks_error_is_surfaced(agent_mod, monkeypatch):
    fake = FakeMC(overrides={
        ("GET", f"/api/v1/agent/boards/{BOARD}/tasks"): (401, {"detail": "Invalid token"}),
    }).install(agent_mod, monkeypatch)
    out = _unwrap(agent_mod.mc_tasks)()
    fake.assert_agent_only()
    assert "401" in out


# ── Operator mode: unchanged paths, create-task route fixed ─────────────


def test_operator_mc_create_task_hits_board_scoped_route(operator_mod, monkeypatch):
    calls = []

    def fake_api(method, path, **kwargs):
        calls.append((method, path, kwargs.get("json")))
        if path == "/agents":
            return [{"id": "agent-x", "name": "Builder"}]
        return {"id": "new-task-uuid-0000", "title": "T"}

    monkeypatch.setattr(operator_mod, "_api", fake_api)
    out = _unwrap(operator_mod.mc_create_task)("T", board_id=BOARD, agent_name="builder")
    posts = [c for c in calls if c[0] == "POST"]
    assert posts == [("POST", f"/boards/{BOARD}/tasks",
                      {"title": "T", "description": "", "priority": "medium",
                       "assigned_agent_id": "agent-x"})]
    assert "Task erstellt" in out


def test_operator_mc_tasks_still_uses_admin_routes(operator_mod, monkeypatch):
    calls = []

    def fake_api(method, path, **kwargs):
        calls.append((method, path))
        return [{"id": TASK, "title": "Card one", "status": "inbox"}]

    monkeypatch.setattr(operator_mod, "_api", fake_api)
    out = _unwrap(operator_mod.mc_tasks)(board_id=BOARD)
    assert ("GET", f"/boards/{BOARD}/tasks") in calls
    assert "Card one" in out
