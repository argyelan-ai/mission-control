"""Worker flow over MCP (ADR-101): mc_next_task / mc_progress / mc_finish /
mc_handbook plus the vault tools of scripts/mc-mcp.py.

What these tests pin:

* The worker tools exist only for a registered agent that opted in with
  MC_MCP_WORKER=1 — not for a plain agent-mode process (a harness with its own
  bridge), not for a guest session (ADR-100 §2), not for the operator.
* Every call a worker tool makes goes to an agent-scoped route with the agent's
  own token (context-reconstruction-design F11) — never the admin path.
* mc_finish runs the SAME pre-finish check as the `mc` command line
  (ADR-037): it calls mc_cli.commands._cmd_finish, no second copy of the rules.
* Every answer names the next step, because some programs ignore the server
  instructions.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MCMCP_PATH = REPO / "scripts" / "mc-mcp.py"
CLI_DIR = REPO / "scripts" / "mc-cli"

AGENT_TOKEN = "tok-worker-test"
SESSION_TOKEN = "mcs_sessiontoken"
BOARD = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
TASK = "11111111-2222-3333-4444-555555555555"
AGENT = "99999999-8888-7777-6666-555555555555"
ATTEMPT = "att-0001"
WORKER_TOOLS = {"mc_next_task", "mc_progress", "mc_finish", "mc_handbook"}
VAULT_TOOLS = {"mc_vault_search", "mc_vault_read", "mc_vault_write"}

REFLECTION = (
    "## What was done\nBuilt the export and ran it against the sample data.\n\n"
    "## What worked\nThe existing parser covered every row.\n\n"
    "## What was unclear\nWhich date format the report expects.\n\n"
    "## Lesson for agent memory\nCheck the date format before the first run.\n"
)

_IDENTITY_VARS = (
    "MC_AGENT_TOKEN", "MC_AGENT_ENV_FILE", "MC_COMMS_SESSION_FILE",
    "MC_MCP_WORKER", "MC_API_URL",
    "MC_MCP_HEARTBEAT_SECONDS", "MC_MCP_HEARTBEAT_QUIET_MINUTES",
)


def _load(tmp_path: Path, name: str = "mc_mcp_worker_test"):
    """Import a copy of the script from a temp folder: its `.env` is a fake
    operator file and scripts/mc-cli is NOT beside it."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(MCMCP_PATH, scripts / "mc-mcp.py")
    (tmp_path / ".env").write_text("JWT_SECRET_KEY=operator-admin-secret\n")
    spec = importlib.util.spec_from_file_location(name, scripts / "mc-mcp.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _clean_env(monkeypatch):
    for var in _IDENTITY_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def worker_mod(monkeypatch, tmp_path):
    """A registered agent that opted into the worker flow. Heartbeat off unless
    a test switches it on."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("MC_AGENT_TOKEN", AGENT_TOKEN)
    monkeypatch.setenv("MC_MCP_WORKER", "1")
    monkeypatch.setenv("MC_MCP_HEARTBEAT_SECONDS", "0")
    mod = _load(tmp_path)
    yield mod
    mod._stop_heartbeat(send_idle=False)


@pytest.fixture
def agent_mod(monkeypatch, tmp_path):
    """Agent mode without the worker switch (a harness with its own bridge)."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("MC_AGENT_TOKEN", AGENT_TOKEN)
    return _load(tmp_path)


@pytest.fixture
def guest_mod(monkeypatch, tmp_path):
    """A guest session (ADR-100) that also sets MC_MCP_WORKER=1 — still a guest."""
    _clean_env(monkeypatch)
    f = tmp_path / "session.json"
    f.write_text(json.dumps({"token": SESSION_TOKEN, "address": "guest@desk:project"}))
    monkeypatch.setenv("MC_COMMS_SESSION_FILE", str(f))
    monkeypatch.setenv("MC_MCP_WORKER", "1")
    return _load(tmp_path)


@pytest.fixture
def operator_mod(monkeypatch, tmp_path):
    """No agent identity; MC_MCP_WORKER=1 alone must not make a worker."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("MC_MCP_WORKER", "1")
    return _load(tmp_path)


def _unwrap(tool):
    return getattr(tool, "fn", tool)


def _tool_names(mod) -> set[str]:
    return {t.name for t in asyncio.run(mod.mcp.list_tools())}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _Resp:
    def __init__(self, status: int, payload):
        self.status_code = status
        self._payload = payload
        self.text = "" if payload is None else json.dumps(payload)

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


class Board:
    """A small stateful MC: one card, its checklist and comments, a queue of
    poll answers, the vault. Records every HTTP call."""

    EXTRA = {"new_comments": [], "my_agent_id": AGENT, "is_board_lead": False}

    def __init__(self):
        self.calls: list[dict] = []
        self.polls: list[dict] = [{"state": "idle"}]
        self.card = {
            "id": TASK, "title": "Build the export", "status": "inbox",
            "board_id": BOARD, "assigned_agent_id": AGENT,
            "dispatch_attempt_id": ATTEMPT, "human_review_required": False,
            "description": "Export the report.",
        }
        self.checklist: list[dict] = []
        self.comments: list[dict] = []
        self.patch_error: tuple[int, dict] | None = None
        self.detail_error: tuple[int, dict] | None = None
        self.poll_error: tuple[int, dict] | None = None
        self.me_current_task: dict | None = None
        self.heartbeats: list[dict] = []
        self.vault_hits: list[dict] = []
        self.notes: dict[str, dict] = {}
        self.vault_writes: list[dict] = []

    # ── poll answers ─────────────────────────────────────────────────────
    def new_task(self, **top) -> dict:
        return {
            "state": "new_task",
            "task": {
                "id": TASK, "title": self.card["title"], "status": self.card["status"],
                "assigned_agent_id": AGENT, "dispatched_at": _now_iso(), "ack_at": None,
                "board_id": BOARD, "workspace_path": "/work/export",
                "prompt": "Export the report as CSV.\nWhen done: mc finish.",
                "slug": "build-the-export",
                "dispatch_attempt_id": self.card["dispatch_attempt_id"],
            },
            **top,
        }

    def _next_poll(self) -> dict:
        answer = self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
        return {**self.EXTRA, **answer}

    # ── routing ──────────────────────────────────────────────────────────
    def route(self, method: str, path: str, params, body):
        agent = "/api/v1/agent"
        task = f"{agent}/boards/{BOARD}/tasks/{TASK}"
        if (method, path) == ("GET", f"{agent}/me/poll"):
            if self.poll_error:
                return self.poll_error
            return 200, self._next_poll()
        if (method, path) == ("GET", f"{agent}/me"):
            return 200, {"id": AGENT, "name": "worker", "board_id": BOARD,
                         "current_task": self.me_current_task}
        if (method, path) == ("POST", f"{agent}/me/heartbeat"):
            self.heartbeats.append(body)
            return 200, {"ok": True}
        if (method, path) == ("GET", f"{task}/detail"):
            if self.detail_error:
                return self.detail_error
            return 200, {**self.card, "assigned_agent_name": "worker"}
        if (method, path) == ("GET", f"{task}/checklist"):
            return 200, self.checklist
        if (method, path) == ("GET", f"{task}/comments"):
            return 200, self.comments
        if (method, path) == ("POST", f"{task}/comments"):
            comment = {
                "id": f"c-{len(self.comments) + 1}", "content": body["content"],
                "comment_type": body.get("comment_type", "message"),
                "author_type": "agent", "author_agent_id": AGENT,
                "created_at": _now_iso(),
            }
            self.comments.append(comment)
            return 201, comment
        if (method, path) == ("PATCH", task):
            if self.patch_error:
                return self.patch_error
            self.card["status"] = body["status"]
            return 200, dict(self.card)
        if (method, path) == ("GET", f"{agent}/boards/{BOARD}/tasks"):
            return 200, [dict(self.card)]
        if (method, path) == ("GET", f"{agent}/vault/search"):
            return 200, {"q": (params or {}).get("q"), "hits": self.vault_hits,
                         "requesting_agent": "worker"}
        if method == "GET" and path.startswith(f"{agent}/vault/note/"):
            note = self.notes.get(path[len(f"{agent}/vault/note/"):])
            return (200, note) if note else (404, {"detail": "note not found"})
        if (method, path) == ("POST", f"{agent}/vault/note"):
            self.vault_writes.append(body)
            return 200, {"ok": True, "envelope": "env-1.json",
                         "expected_target": "knowledge/export-notes.md"}
        if (method, path) == ("GET", "/api/v1/vault/search"):
            return 200, {"q": (params or {}).get("q"), "hits": self.vault_hits}
        if method == "GET" and path.startswith("/api/v1/vault/note/"):
            note = self.notes.get(path[len("/api/v1/vault/note/"):])
            return (200, note) if note else (404, {"detail": "note not found"})
        return 404, {"detail": "not found"}

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
                base, _, rest = url.partition("://")
                host, slash, tail = rest.partition("/")
                path = slash + tail
                fake.calls.append({
                    "method": method, "path": path, "base": f"{base}://{host}",
                    "headers": dict(headers or {}), "params": params, "json": json,
                })
                status, payload = fake.route(method, path, params, json)
                return _Resp(status, payload)

        monkeypatch.setattr(mod.httpx, "Client", _Client)
        return self

    # ── assertions ───────────────────────────────────────────────────────
    def assert_agent_only(self, token: str = AGENT_TOKEN):
        assert self.calls, "no HTTP call was made"
        for c in self.calls:
            assert c["headers"].get("Authorization") == f"Bearer {token}", c
            assert c["path"].startswith("/api/v1/agent/"), c

    def of(self, method: str, suffix: str) -> list[dict]:
        return [c for c in self.calls if c["method"] == method and c["path"].endswith(suffix)]


@pytest.fixture
def board(worker_mod, monkeypatch):
    return Board().install(worker_mod, monkeypatch)


def _accept(mod, board) -> str:
    """Deliver the card and accept it; afterwards the poll says `working`."""
    board.polls = [board.new_task(), {"state": "working", "task_id": TASK}]
    return _unwrap(mod.mc_next_task)()


def _use_cli(mod, monkeypatch):
    monkeypatch.setattr(mod, "_CLI_DIR", CLI_DIR)


# ── Who gets which tool ──────────────────────────────────────────────────────


def test_worker_gets_the_worker_and_vault_tools(worker_mod):
    names = _tool_names(worker_mod)
    assert WORKER_TOOLS <= names
    assert VAULT_TOOLS <= names
    assert worker_mod.WORKER_MODE is True


def test_plain_agent_mode_has_no_worker_tools(agent_mod):
    """A harness with its own bridge polls and beats itself: a second poll from
    here would eat its comments."""
    names = _tool_names(agent_mod)
    assert not (WORKER_TOOLS & names)
    assert VAULT_TOOLS <= names
    assert agent_mod.WORKER_MODE is False
    assert not agent_mod.mcp.instructions


def test_guest_session_gains_nothing(guest_mod):
    """ADR-100 §2: a guest keeps exactly its comms tools — no worker tools and
    no vault tools, even with MC_MCP_WORKER=1."""
    names = _tool_names(guest_mod)
    assert not (WORKER_TOOLS & names)
    assert not (VAULT_TOOLS & names)
    assert guest_mod.WORKER_MODE is False
    assert {"mc_who", "mc_send"} <= names


def test_operator_is_never_a_worker_and_cannot_write_the_vault(operator_mod):
    names = _tool_names(operator_mod)
    assert not (WORKER_TOOLS & names)
    assert {"mc_vault_search", "mc_vault_read"} <= names
    assert "mc_vault_write" not in names
    assert operator_mod.WORKER_MODE is False


@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("YES", True), ("on", True),
    ("0", False), ("", False), ("no", False),
])
def test_worker_switch_values(monkeypatch, tmp_path, value, expected):
    _clean_env(monkeypatch)
    monkeypatch.setenv("MC_AGENT_TOKEN", AGENT_TOKEN)
    monkeypatch.setenv("MC_MCP_WORKER", value)
    monkeypatch.setenv("MC_MCP_HEARTBEAT_SECONDS", "0")
    assert _load(tmp_path).WORKER_MODE is expected


# ── Instructions on connect ──────────────────────────────────────────────────


def test_worker_instructions_reach_the_client(worker_mod):
    from fastmcp import Client

    async def read():
        async with Client(worker_mod.mcp) as c:
            return c.initialize_result.instructions

    text = asyncio.run(read())
    for word in ("mc_next_task", "mc_progress", "mc_finish", "mc_handbook"):
        assert word in text


def test_handbook_explains_the_loop(worker_mod):
    text = _unwrap(worker_mod.mc_handbook)()
    for word in ("mc_next_task", "mc_progress", "mc_finish", "## What was done",
                 "## Lesson for agent memory", "mc_checklist", "mc_vault_search"):
        assert word in text
    assert "mc_patch_task" in text  # the rule: never finish with it


# ── MC_API_URL ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value,base", [
    (None, "http://localhost/api/v1"),
    ("", "http://localhost/api/v1"),
    ("http://mc.example:8000", "http://mc.example:8000/api/v1"),
    ("http://mc.example:8000/", "http://mc.example:8000/api/v1"),
    ("http://mc.example:8000/api/v1", "http://mc.example:8000/api/v1"),
    ("  http://mc.example/api/v1/ ", "http://mc.example/api/v1"),
])
def test_mc_api_url_sets_the_base(monkeypatch, tmp_path, value, base):
    _clean_env(monkeypatch)
    monkeypatch.setenv("MC_AGENT_TOKEN", AGENT_TOKEN)
    if value is not None:
        monkeypatch.setenv("MC_API_URL", value)
    assert _load(tmp_path).MC_BASE == base


def test_calls_go_to_the_configured_mc(monkeypatch, tmp_path):
    _clean_env(monkeypatch)
    monkeypatch.setenv("MC_AGENT_TOKEN", AGENT_TOKEN)
    monkeypatch.setenv("MC_MCP_WORKER", "1")
    monkeypatch.setenv("MC_MCP_HEARTBEAT_SECONDS", "0")
    monkeypatch.setenv("MC_API_URL", "http://mc.example:8000")
    mod = _load(tmp_path)
    board = Board().install(mod, monkeypatch)
    _unwrap(mod.mc_next_task)()
    assert board.calls[0]["base"] == "http://mc.example:8000"
    assert board.calls[0]["path"] == "/api/v1/agent/me/poll"


# ── mc_next_task: a new card ─────────────────────────────────────────────────


def test_next_task_accepts_the_card_and_names_the_next_step(worker_mod, board):
    out = _accept(worker_mod, board)

    assert out.startswith("New card accepted: Build the export")
    assert f"Card id: {TASK}" in out and BOARD in out
    assert "--- BRIEF (from Mission Control) ---" in out
    assert "Export the report as CSV." in out
    assert "--- END OF BRIEF ---" in out
    assert "Next step:" in out
    assert "mc_progress" in out and "mc_finish" in out
    # the brief speaks in `mc` verbs — the answer maps them to tools
    assert "mc finish" in out and "mc_checklist_add" in out and "mc ack" in out

    patches = board.of("PATCH", f"/tasks/{TASK}")
    assert len(patches) == 1
    assert patches[0]["json"] == {"status": "in_progress"}
    assert patches[0]["headers"]["X-Dispatch-Attempt-Id"] == ATTEMPT
    assert board.card["status"] == "in_progress"
    assert worker_mod._held_card()["task_id"] == TASK
    board.assert_agent_only()


def test_next_task_without_attempt_id_sends_no_attempt_header(worker_mod, board):
    board.card["dispatch_attempt_id"] = None
    _accept(worker_mod, board)
    patch = board.of("PATCH", f"/tasks/{TASK}")[0]
    assert "X-Dispatch-Attempt-Id" not in patch["headers"]
    assert worker_mod._held_card()["attempt_id"] == ""


def test_next_task_reports_a_failed_accept_and_does_not_keep_the_card(worker_mod, board):
    board.patch_error = (409, {"detail": "Task run_control=stopped — Updates nicht erlaubt"})
    out = _accept(worker_mod, board)
    assert out.startswith("Error: MC delivered card Build the export (11111111) but accepting it failed:")
    assert "run_control=stopped" in out
    assert "Next step: call mc_next_task again." in out
    assert worker_mod._held_card() is None


def test_next_task_treats_the_legacy_double_in_progress_refusal_as_accepted(worker_mod, board):
    board.patch_error = (400, {"detail": "Ungueltiger Status-Uebergang: In Progress -> In Progress"})
    out = _accept(worker_mod, board)
    assert out.startswith("New card accepted: Build the export")
    assert worker_mod._held_card()["task_id"] == TASK


def test_next_task_without_card_id_is_an_error(worker_mod, board):
    board.polls = [{"state": "new_task", "task": {"title": "x"}}]
    out = _unwrap(worker_mod.mc_next_task)()
    assert out.startswith("Error: MC answered new_task without a card id.")
    assert not board.of("PATCH", f"/tasks/{TASK}")


def test_redelivered_card_is_named_as_the_same_card(worker_mod, board):
    """Orphan-run redispatch: fresh attempt id, status stays in_progress, flag
    at the top level of the poll answer."""
    _accept(worker_mod, board)
    board.card["dispatch_attempt_id"] = "att-0002"
    board.polls = [board.new_task(orphaned_run_redispatched=True, redispatch_count=1)]
    out = _unwrap(worker_mod.mc_next_task)()

    assert out.startswith("This is your card again (MC re-delivered it): Build the export")
    assert "Continue where you stopped, do not start over." in out
    assert "Do not create the checklist again" in out
    patches = board.of("PATCH", f"/tasks/{TASK}")
    assert patches[-1]["json"] == {"status": "in_progress"}
    assert patches[-1]["headers"]["X-Dispatch-Attempt-Id"] == "att-0002"
    assert worker_mod._held_card()["attempt_id"] == "att-0002"


def test_redelivery_is_detected_without_the_flag_when_the_card_is_held(worker_mod, board):
    _accept(worker_mod, board)
    board.polls = [board.new_task()]
    out = _unwrap(worker_mod.mc_next_task)()
    assert out.startswith("This is your card again (MC re-delivered it): Build the export")


# ── mc_next_task: idle, holds, working, end answers ──────────────────────────


def test_idle_names_the_wait(worker_mod, board):
    out = _unwrap(worker_mod.mc_next_task)()
    assert out.startswith("No card for you right now.")
    assert "Next step: call mc_next_task again in about a minute" in out
    board.assert_agent_only()


@pytest.mark.parametrize("flag,reason", [
    ("system_halted", "the system is halted"),
    ("system_draining", "the system is draining"),
    ("update_drain", "an MC update is running"),
    ("runtime_not_ready", "your model runtime is not ready"),
])
def test_idle_hold_names_the_reason(worker_mod, board, flag, reason):
    board.polls = [{"state": "idle", flag: True, "detail": "back in a few minutes"}]
    out = _unwrap(worker_mod.mc_next_task)()
    assert f"MC is holding new work ({reason}): back in a few minutes." in out
    assert "No card for you right now." not in out
    assert "Next step:" in out


def test_plain_idle_while_holding_a_card_drops_it(worker_mod, board):
    _accept(worker_mod, board)
    board.polls = [{"state": "idle"}]
    out = _unwrap(worker_mod.mc_next_task)()
    assert "MC no longer lists card Build the export (11111111) as your active card — stop working on it." in out
    assert worker_mod._held_card() is None


def test_held_idle_keeps_the_card(worker_mod, board):
    """A halted system answers idle for the restart of an orphaned run — the
    card is still the worker's."""
    _accept(worker_mod, board)
    board.polls = [{"state": "idle", "system_halted": True, "detail": "halted by the operator"}]
    out = _unwrap(worker_mod.mc_next_task)()
    assert "MC is holding new work (the system is halted)" in out
    assert "no longer lists" not in out
    assert worker_mod._held_card()["task_id"] == TASK


def test_working_on_an_in_progress_card_says_continue(worker_mod, board):
    board.card["status"] = "in_progress"
    board.polls = [{"state": "working", "task_id": TASK}]
    out = _unwrap(worker_mod.mc_next_task)()
    assert out.startswith("You are already working on card Build the export (11111111).")
    assert "mc_progress" in out and "mc_finish" in out
    assert worker_mod._held_card()["task_id"] == TASK
    assert worker_mod._held_card()["attempt_id"] == ATTEMPT
    board.assert_agent_only()


def test_working_on_a_review_card_says_wait_and_drops_it(worker_mod, board):
    _accept(worker_mod, board)
    board.card["status"] = "review"
    out = _unwrap(worker_mod.mc_next_task)()
    assert out.startswith("Card Build the export (11111111) is waiting for the review decision. Nothing to do for you now.")
    assert "Next step:" in out
    assert worker_mod._held_card() is None


def test_working_but_card_unreadable_is_said_plainly(worker_mod, board):
    board.detail_error = (403, {"detail": "Agent not assigned to this board"})
    board.polls = [{"state": "working", "task_id": TASK}]
    out = _unwrap(worker_mod.mc_next_task)()
    assert out.startswith("MC lists card 11111111 as your active card, but reading it failed:")
    assert worker_mod._held_card() is None


@pytest.mark.parametrize("state,phrase", [
    ("cancelled", "was cancelled. Stop working on it; do not finish it."),
    ("stopped", "was stopped by the operator. Stop working on it now"),
])
def test_end_answer_stops_the_card_and_is_reported_once(worker_mod, board, state, phrase):
    _accept(worker_mod, board)
    board.polls = [{"state": state, "task_id": TASK}]
    first = _unwrap(worker_mod.mc_next_task)()
    assert f"Card Build the export (11111111) {phrase}" in first
    assert worker_mod._held_card() is None

    # MC repeats the old end answer while nothing else is to deliver
    second = _unwrap(worker_mod.mc_next_task)()
    assert second.startswith("No card for you right now.")


def test_end_answer_for_a_card_delivered_again_is_reported_again(worker_mod, board):
    """stop -> resume -> stop: the second stop of the same card must be told."""
    _accept(worker_mod, board)
    board.polls = [{"state": "stopped", "task_id": TASK}]
    _unwrap(worker_mod.mc_next_task)()
    board.card["status"] = "inbox"
    _accept(worker_mod, board)
    board.polls = [{"state": "stopped", "task_id": TASK}]
    out = _unwrap(worker_mod.mc_next_task)()
    assert "was stopped by the operator" in out


def test_poll_error_is_reported_as_is(worker_mod, board):
    board.poll_error = (401, {"detail": "Invalid agent token"})
    out = _unwrap(worker_mod.mc_next_task)()
    assert out.startswith("Error: HTTP 401")
    assert "Next step: fix the connection or token, then call mc_next_task again." in out
    board.assert_agent_only()


# ── Comments and messages the poll hands over only once ──────────────────────


def test_new_comments_are_passed_on(worker_mod, board):
    board.polls = [{"state": "idle", "new_comments": [{
        "comment_id": "c9", "task_id": TASK, "task_title": "Build the export",
        "content": "Please use ISO dates.", "comment_type": "feedback",
        "source": "operator", "created_at": _now_iso(),
    }]}]
    out = _unwrap(worker_mod.mc_next_task)()
    assert "New comments on your cards (MC shows them only once):" in out
    assert "- [feedback] on Build the export (11111111): Please use ISO dates." in out
    assert out.index("New comments") < out.index("No card for you right now.")


def test_shown_messages_are_acked_on_the_next_poll(worker_mod, board):
    board.polls = [
        {"state": "idle", "new_messages": [
            {"id": "m1", "thread_id": "th-1", "seq": 4, "sender": "lead",
             "message_type": "question", "body": "Which format?", "question_meta": None},
            {"id": "m2", "thread_id": "th-1", "seq": 5, "sender": "lead",
             "message_type": "message", "body": "CSV please.", "question_meta": None},
        ]},
        {"state": "idle"},
    ]
    first = _unwrap(worker_mod.mc_next_task)()
    assert "New messages:" in first
    assert "- lead (question): Which format?" in first
    assert board.calls[0]["params"] is None  # nothing shown yet, nothing to ack

    _unwrap(worker_mod.mc_next_task)()
    polls = board.of("GET", "/agent/me/poll")
    assert json.loads(polls[1]["params"]["acked_seq"]) == {"th-1": 5}


# ── mc_progress ──────────────────────────────────────────────────────────────


def test_progress_posts_a_progress_comment_on_the_held_card(worker_mod, board):
    _accept(worker_mod, board)
    out = _unwrap(worker_mod.mc_progress)("Parser done, writing the CSV now.")
    assert out.startswith("Progress note saved on card Build the export (11111111).")
    assert "Next step:" in out
    post = board.of("POST", f"/tasks/{TASK}/comments")[0]
    assert post["json"] == {"content": "Parser done, writing the CSV now.",
                            "comment_type": "progress"}
    assert post["headers"]["X-Dispatch-Attempt-Id"] == ATTEMPT
    board.assert_agent_only()


def test_progress_rejects_an_empty_note(worker_mod, board):
    _accept(worker_mod, board)
    out = _unwrap(worker_mod.mc_progress)("   ")
    assert out.startswith("Error: the note is empty.")
    assert not board.of("POST", f"/tasks/{TASK}/comments")


def test_progress_without_a_card_says_fetch_one(worker_mod, board):
    out = _unwrap(worker_mod.mc_progress)("done something")
    assert out == "Error: no active card. Call mc_next_task first."


def test_progress_recovers_the_card_after_a_server_restart(worker_mod, board):
    """A fresh server process holds no card: MC's own record (/agent/me) names
    the worker's active card."""
    board.card["status"] = "in_progress"
    board.me_current_task = {"id": TASK, "title": "Build the export",
                             "status": "in_progress", "board_id": BOARD}
    out = _unwrap(worker_mod.mc_progress)("Back after a restart.")
    assert out.startswith("Progress note saved on card Build the export (11111111).")
    assert worker_mod._held_card()["task_id"] == TASK
    board.assert_agent_only()


# ── mc_finish: the same checks as `mc finish` ────────────────────────────────


def test_finish_runs_the_cli_finish_and_reports_the_real_status(worker_mod, board, monkeypatch):
    _use_cli(worker_mod, monkeypatch)
    _accept(worker_mod, board)
    board.calls.clear()
    out = _unwrap(worker_mod.mc_finish)(REFLECTION)

    assert out.startswith("Card finished: Build the export (11111111) — status is now done.")
    assert "Next step: call mc_next_task for your next card." in out
    assert board.card["status"] == "done"
    assert worker_mod._held_card() is None

    seq = [(c["method"], c["path"].rsplit("/", 1)[-1]) for c in board.calls]
    assert seq[:5] == [("GET", "detail"), ("GET", "checklist"), ("GET", "comments"),
                       ("POST", "comments"), ("PATCH", TASK)]
    reflection = board.of("POST", f"/tasks/{TASK}/comments")[0]["json"]
    assert reflection["comment_type"] == "reflection"
    patch = board.of("PATCH", f"/tasks/{TASK}")[0]
    assert patch["json"]["status"] == "done"
    assert patch["headers"]["X-Dispatch-Attempt-Id"] == ATTEMPT
    board.assert_agent_only()


def test_finish_for_review_sets_review(worker_mod, board, monkeypatch):
    _use_cli(worker_mod, monkeypatch)
    _accept(worker_mod, board)
    out = _unwrap(worker_mod.mc_finish)(REFLECTION, review=True)
    assert "status is now review" in out
    assert board.card["status"] == "review"


def test_finish_honours_human_review_required(worker_mod, board, monkeypatch):
    """The CLI turns done into review for such a card and says so — the note
    reaches the worker."""
    _use_cli(worker_mod, monkeypatch)
    board.card["human_review_required"] = True
    _accept(worker_mod, board)
    out = _unwrap(worker_mod.mc_finish)(REFLECTION)
    assert "status is now review" in out
    assert "Human-Review" in out
    assert board.card["status"] == "review"


def test_finish_is_blocked_by_an_open_checklist_item(worker_mod, board, monkeypatch):
    _use_cli(worker_mod, monkeypatch)
    _accept(worker_mod, board)
    board.checklist = [{"id": "abcdef12-0000-0000-0000-000000000000",
                        "title": "Run the export", "status": "open"}]
    out = _unwrap(worker_mod.mc_finish)(REFLECTION)

    assert out.startswith("Error:")
    assert "Checklist-Item" in out
    assert "Hint:" in out and "mc_checklist_done" in out
    assert board.card["status"] == "in_progress"
    assert not board.of("POST", f"/tasks/{TASK}/comments")
    assert worker_mod._held_card()["task_id"] == TASK


def test_finish_requires_the_reflection_sections(worker_mod, board, monkeypatch):
    _use_cli(worker_mod, monkeypatch)
    _accept(worker_mod, board)
    board.calls.clear()
    out = _unwrap(worker_mod.mc_finish)("All done, everything works as expected, nothing to add here at all.")
    assert out.startswith("Error:")
    assert "## What was done" in out and "## Lesson for agent memory" in out
    assert board.calls == []  # refused before any call to MC
    assert board.card["status"] == "in_progress"


def test_finish_checks_the_reflection_before_it_looks_for_a_card(worker_mod, board, monkeypatch):
    # No card held: the text is still checked first, without a single call to MC.
    _use_cli(worker_mod, monkeypatch)
    out = _unwrap(worker_mod.mc_finish)("All done, everything works as expected, nothing to add here at all.")
    assert out.startswith("Error:")
    assert "## What was done" in out
    assert board.calls == []


def test_finish_posts_no_second_reflection_within_the_window(worker_mod, board, monkeypatch):
    _use_cli(worker_mod, monkeypatch)
    _accept(worker_mod, board)
    board.comments.append({
        "id": "c-0", "content": REFLECTION, "comment_type": "reflection",
        "author_type": "agent", "author_agent_id": AGENT, "created_at": _now_iso(),
    })
    out = _unwrap(worker_mod.mc_finish)(REFLECTION)
    assert out.startswith("Card finished:")
    assert not board.of("POST", f"/tasks/{TASK}/comments")
    assert board.card["status"] == "done"


def test_finish_with_a_stale_attempt_points_to_next_task(worker_mod, board, monkeypatch):
    _use_cli(worker_mod, monkeypatch)
    _accept(worker_mod, board)
    board.patch_error = (409, {"detail": "Stale dispatch_attempt_id — dein Run ist veraltet."})
    out = _unwrap(worker_mod.mc_finish)(REFLECTION)
    assert out.startswith("Error:")
    assert "Stale dispatch_attempt_id" in out
    assert "Hint:" in out and "mc_next_task" in out
    assert "mc done" not in out  # the CLI's own retry hint names verbs a worker lacks
    assert worker_mod._held_card()["task_id"] == TASK


def test_finish_refused_by_the_proof_gate_keeps_the_card(worker_mod, board, monkeypatch):
    _use_cli(worker_mod, monkeypatch)
    _accept(worker_mod, board)
    board.patch_error = (422, {"detail": "Proof gate: no evidence attached"})
    out = _unwrap(worker_mod.mc_finish)(REFLECTION)
    assert out.startswith("Error:") and "HTTP 422" in out
    assert "Hint:" in out
    assert worker_mod._held_card()["task_id"] == TASK


def test_finish_without_the_cli_finishes_nothing(worker_mod, board):
    """The script copy has no scripts/mc-cli beside it: no unchecked finish."""
    _accept(worker_mod, board)
    board.calls.clear()
    out = _unwrap(worker_mod.mc_finish)(REFLECTION)
    assert out.startswith("Error: the mc command line (scripts/mc-cli) is not next to this server")
    assert not [c for c in board.calls if c["method"] in ("PATCH", "POST")]
    assert board.card["status"] == "in_progress"


def test_finish_without_a_card_says_fetch_one(worker_mod, board, monkeypatch):
    _use_cli(worker_mod, monkeypatch)
    out = _unwrap(worker_mod.mc_finish)(REFLECTION)
    assert out == "Error: no active card. Call mc_next_task first."


def test_finish_rejects_a_workdir_that_is_no_folder(worker_mod, board, monkeypatch, tmp_path):
    _use_cli(worker_mod, monkeypatch)
    _accept(worker_mod, board)
    out = _unwrap(worker_mod.mc_finish)(REFLECTION, workdir=str(tmp_path / "missing"))
    assert out.startswith("Error: workdir")
    assert board.card["status"] == "in_progress"


def test_finish_restores_the_working_directory(worker_mod, board, monkeypatch, tmp_path):
    import os
    _use_cli(worker_mod, monkeypatch)
    _accept(worker_mod, board)
    before = os.getcwd()
    work = tmp_path / "work"
    work.mkdir()
    out = _unwrap(worker_mod.mc_finish)(REFLECTION, workdir=str(work))
    assert out.startswith("Card finished:")
    assert os.getcwd() == before


def test_finish_contract_with_the_cli(worker_mod, monkeypatch):
    """mc_finish depends on these names and this signature of the mc CLI. A
    rename there must fail here, not in a worker's session."""
    import inspect
    _use_cli(worker_mod, monkeypatch)
    commands, config, errors = worker_mod._mc_cli()
    assert list(inspect.signature(commands._cmd_finish).parameters) == ["args", "client", "cfg"]
    assert issubclass(errors.UsageError, errors.CLIError)
    assert issubclass(errors.ClientError, errors.CLIError)
    assert issubclass(errors.ServerError, errors.CLIError)
    cfg = config.Config(api_url="http://x", agent_token="t", task_id=TASK,
                        board_id=BOARD, dispatch_attempt_id=None)
    assert cfg.require_task_context() == (BOARD, TASK)


# ── Heartbeat ────────────────────────────────────────────────────────────────


@pytest.fixture
def beating(monkeypatch, tmp_path):
    """A worker with the heartbeat on. The interval is long, so only the
    synchronous first beat and explicit ticks happen inside a test."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("MC_AGENT_TOKEN", AGENT_TOKEN)
    monkeypatch.setenv("MC_MCP_WORKER", "1")
    monkeypatch.setenv("MC_MCP_HEARTBEAT_SECONDS", "45")
    mod = _load(tmp_path)
    board = Board().install(mod, monkeypatch)
    yield mod, board
    mod._stop_heartbeat(send_idle=False)


def test_accepting_a_card_sends_a_working_heartbeat(beating):
    mod, board = beating
    _accept(mod, board)
    assert board.heartbeats[0] == {"status": "working", "task_id": TASK, "attempt_id": ATTEMPT}
    board.assert_agent_only()


def test_heartbeat_interval_is_capped_below_the_turn_signal_age(monkeypatch, beating):
    mod, _ = beating
    monkeypatch.setenv("MC_MCP_HEARTBEAT_SECONDS", "600")
    assert mod._heartbeat_interval() == 60
    monkeypatch.setenv("MC_MCP_HEARTBEAT_SECONDS", "nonsense")
    assert mod._heartbeat_interval() == 30


def test_heartbeat_tick_beats_only_while_the_worker_is_active(monkeypatch, beating):
    """The beat proves recent MC calls, not that the model works: after the
    quiet window it stops, so MC's own stall checks see the silence."""
    mod, board = beating
    _accept(mod, board)
    board.heartbeats.clear()

    mod._heartbeat_tick()
    assert len(board.heartbeats) == 1

    monkeypatch.setenv("MC_MCP_HEARTBEAT_QUIET_MINUTES", "20")
    mod._last_call -= 21 * 60
    mod._heartbeat_tick()
    assert len(board.heartbeats) == 1  # quiet: no beat

    monkeypatch.setenv("MC_MCP_HEARTBEAT_QUIET_MINUTES", "0")
    mod._heartbeat_tick()
    assert len(board.heartbeats) == 2  # 0 = no limit


def test_heartbeat_tick_without_a_card_sends_nothing(beating):
    mod, board = beating
    mod._heartbeat_tick()
    assert board.heartbeats == []


def test_finishing_stops_the_heartbeat_with_an_idle_beat(monkeypatch, beating):
    mod, board = beating
    _use_cli(mod, monkeypatch)
    _accept(mod, board)
    _unwrap(mod.mc_finish)(REFLECTION)
    assert board.heartbeats[-1] == {"status": "idle"}
    board.heartbeats.clear()
    mod._heartbeat_tick()
    assert board.heartbeats == []


def test_heartbeat_off_sends_no_beat_at_all(worker_mod, board, monkeypatch):
    _use_cli(worker_mod, monkeypatch)
    _accept(worker_mod, board)
    worker_mod._heartbeat_tick()
    _unwrap(worker_mod.mc_finish)(REFLECTION)
    assert board.heartbeats == []


def test_a_tool_call_over_the_wire_stamps_the_last_call(worker_mod, board):
    from fastmcp import Client

    worker_mod._last_call -= 3600
    stale = worker_mod._last_call

    async def call():
        async with Client(worker_mod.mcp) as c:
            return (await c.call_tool("mc_handbook", {})).data

    assert "mc_next_task" in asyncio.run(call())
    assert worker_mod._last_call > stale + 3000


# ── Vault tools ──────────────────────────────────────────────────────────────


_HIT = {
    "path": "knowledge/export-notes.md", "id": "n1", "agent": "worker",
    "type": "knowledge", "tags": ["export"], "project": None,
    "title": "Export notes", "date": "2026-11-01", "task": None, "status": "active",
    "content": "The report expects ISO dates.\n\n" + "x" * 600,
}


def test_vault_search_lists_hits_with_a_short_excerpt(worker_mod, board):
    board.vault_hits = [_HIT]
    out = _unwrap(worker_mod.mc_vault_search)("export", limit=500)
    assert out.startswith("1 notes for 'export':")
    assert "- Export notes — knowledge/export-notes.md [knowledge · worker · 2026-11-01]" in out
    assert "The report expects ISO dates." in out
    assert "x" * 300 not in out  # an excerpt, not the note
    assert "Next step: read a note in full with mc_vault_read(path)." in out
    call = board.of("GET", "/agent/vault/search")[0]
    assert call["params"] == {"q": "export", "limit": 50}
    board.assert_agent_only()


def test_vault_search_rejects_an_empty_query(worker_mod, board):
    assert _unwrap(worker_mod.mc_vault_search)("  ") == "Error: the search text is empty."
    assert board.calls == []


def test_vault_read_returns_frontmatter_and_content(worker_mod, board):
    board.notes["knowledge/export-notes.md"] = {
        "frontmatter": {"title": "Export notes", "type": "knowledge"},
        "content": "The report expects ISO dates.",
    }
    out = _unwrap(worker_mod.mc_vault_read)("/knowledge/export-notes.md")
    assert "title: Export notes" in out
    assert "The report expects ISO dates." in out
    assert board.calls[0]["path"] == "/api/v1/agent/vault/note/knowledge/export-notes.md"
    board.assert_agent_only()


@pytest.mark.parametrize("path", ["../secrets.md", "knowledge/../../etc/passwd", "/.."])
def test_vault_read_refuses_a_path_that_climbs_out(worker_mod, board, path):
    out = _unwrap(worker_mod.mc_vault_read)(path)
    assert out.startswith("Error:") and "'..'" in out
    assert board.calls == []


def test_vault_read_reports_a_missing_note(worker_mod, board):
    out = _unwrap(worker_mod.mc_vault_read)("nope.md")
    assert out.startswith("Error: HTTP 404")


def test_vault_write_links_the_note_to_the_held_card(worker_mod, board):
    _accept(worker_mod, board)
    out = _unwrap(worker_mod.mc_vault_write)(
        "Export notes", "The report expects ISO dates, not local ones.",
        type="knowledge", tags=["export"],
    )
    assert "knowledge/export-notes.md" in out
    assert "background" in out
    assert board.vault_writes == [{
        "title": "Export notes",
        "content": "The report expects ISO dates, not local ones.",
        "type": "knowledge", "tags": ["export"], "task_id": TASK,
    }]
    board.assert_agent_only()


def test_plain_agent_uses_the_agent_vault_routes(agent_mod, monkeypatch):
    board = Board().install(agent_mod, monkeypatch)
    board.vault_hits = [_HIT]
    _unwrap(agent_mod.mc_vault_search)("export")
    _unwrap(agent_mod.mc_vault_write)("Export notes", "The report expects ISO dates.")
    assert board.vault_writes[0] == {"title": "Export notes",
                                     "content": "The report expects ISO dates.",
                                     "type": "note"}
    board.assert_agent_only()


def test_operator_vault_tools_use_the_admin_routes(operator_mod, monkeypatch):
    board = Board().install(operator_mod, monkeypatch)
    board.vault_hits = [_HIT]
    board.notes["knowledge/export-notes.md"] = {"frontmatter": {}, "content": "ISO dates."}
    assert "Export notes" in _unwrap(operator_mod.mc_vault_search)("export")
    assert "ISO dates." in _unwrap(operator_mod.mc_vault_read)("knowledge/export-notes.md")
    assert [c["path"] for c in board.calls] == [
        "/api/v1/vault/search", "/api/v1/vault/note/knowledge/export-notes.md",
    ]
