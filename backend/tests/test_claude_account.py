"""Claude account card (Welle 2, PR 2-4): status, reconnect, disconnect, stale agents.

Pins the contract the settings card builds on:

- the status reads the registry check of the vault token (``secret`` /
  ``claude_code_oauth_token``) — "never tested" until someone saves or clicks;
  reading the status never calls Claude (decision E8a: the real call only on
  save and on request);
- "N agents still use the old token": a running Claude container that started
  before the token was last changed is listed (it got the old token at its
  bootstrap); one started after it, a stopped one, a host agent and an agent
  on another provider are not;
- reconnect runs ``claude setup-token`` in a throwaway container from the
  Claude agent image, shows the sign-in link, sends the code the user pastes,
  and stores the printed token through the same vault helper every other
  path uses — then runs the real check once. The token never appears in a
  response or a log line, and the container is removed afterwards.

Docker and Claude are always mocked: no test reaches a container or Anthropic.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine

TOKEN_KEY = "claude_code_oauth_token"
FAKE_TOKEN = "sk-ant-oat01-" + "Zq7" * 30  # shape only, not a real token
LOGIN_URL = "https://claude.ai/oauth/authorize?code=true&client_id=abc&state=xyz"


def _session() -> AsyncSession:
    return AsyncSession(test_engine, expire_on_commit=False)


async def _store_token(value: str = "sk-ant-oat01-old-" + "a" * 40, *, at: datetime | None = None):
    from app.models.secret import Secret
    from app.services.secrets_helper import upsert_secret_by_key

    async with _session() as s:
        row = await upsert_secret_by_key(s, key=TOKEN_KEY, value=value)
        if at is not None:
            row.updated_at = at
            s.add(row)
            await s.commit()
        return (await s.exec(select(Secret).where(Secret.key == TOKEN_KEY))).first()


async def _runtime(slug: str, runtime_type: str):
    from app.models.runtime import Runtime

    async with _session() as s:
        rt = Runtime(id=uuid.uuid4(), slug=slug, display_name=slug, runtime_type=runtime_type, endpoint="http://example.invalid")
        s.add(rt)
        await s.commit()
        await s.refresh(rt)
        return rt


@pytest.fixture(autouse=True)
def _own_sessions_go_to_the_test_db(monkeypatch):
    monkeypatch.setattr("app.database.engine", test_engine)


@pytest.fixture(autouse=True)
def _no_login_left_over():
    from app.services import claude_account

    claude_account._reset_login_for_tests()
    yield
    claude_account._reset_login_for_tests()


class FakeDocker:
    """Stands in for the docker CLI: records every call, answers by verb."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.started_at: dict[str, tuple[str, str]] = {}  # container -> (status, iso)
        self.pane = ""
        self.fail_run = False

    def __call__(self, args: list[str], timeout: float) -> tuple[int, str]:
        self.calls.append(list(args))
        verb = args[0]
        if verb == "inspect":
            lines = []
            for name in args[3:]:
                if name in self.started_at:
                    status, iso = self.started_at[name]
                    lines.append(f"/{name}|{status}|{iso}")
            return (0 if lines else 1), "\n".join(lines)
        if verb == "run":
            return (1, "Unable to find image") if self.fail_run else (0, "c0ffee\n")
        if verb == "exec" and "capture-pane" in args:
            return 0, self.pane
        return 0, ""

    def verbs(self) -> list[str]:
        return [c[0] for c in self.calls]


@pytest.fixture
def docker(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr("app.services.claude_account._docker", fake)
    return fake


@pytest.fixture
def claude_check(monkeypatch):
    """The registry's real procedure for the token, with the HTTP call stubbed."""
    from app.services.config_checks import kinds
    from app.services.config_checks.registry import Outcome

    calls: list[str] = []

    async def fake(token: str):
        calls.append(token)
        if token.startswith("sk-ant-oat01-bad"):
            return Outcome(False, "Claude rejected the key (HTTP 401) — expired, revoked or mistyped?")
        return Outcome(True, "Claude accepted the key (HTTP 200).")

    monkeypatch.setitem(
        kinds.SECRET_TESTERS, TOKEN_KEY, kinds.SecretTester("quota", fake)
    )
    return calls


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")


# ── 1. Status ─────────────────────────────────────────────────────────────


async def test_status_without_token_is_not_connected_and_calls_nothing(auth_client, docker, claude_check):
    resp = await auth_client.get("/api/v1/claude-account")
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is False
    assert body["check"] is None
    assert body["stale_agents"] == []
    assert claude_check == []


async def test_status_reads_the_registry_check_and_never_runs_it(auth_client, docker, claude_check):
    await _store_token()
    body = (await auth_client.get("/api/v1/claude-account")).json()
    assert body["configured"] is True
    assert body["check"]["state"] == "never_tested"
    assert body["check"]["cost"] == "quota"
    assert body["token_updated_at"]
    # E8a: reading the card must not spend plan quota.
    assert claude_check == []

    run = await auth_client.post(f"/api/v1/config-checks/secret/{TOKEN_KEY}/run")
    assert run.status_code == 200
    body = (await auth_client.get("/api/v1/claude-account")).json()
    assert body["check"]["state"] == "ok"
    assert body["check"]["checked_at"]
    assert len(claude_check) == 1


async def test_a_broken_token_reads_red_with_its_text(auth_client, docker, claude_check):
    await _store_token("sk-ant-oat01-bad-" + "b" * 40)
    await auth_client.post(f"/api/v1/config-checks/secret/{TOKEN_KEY}/run")
    check = (await auth_client.get("/api/v1/claude-account")).json()["check"]
    assert check["state"] == "fail"
    assert "401" in check["message"]


async def test_status_is_admin_only(client):
    resp = await client.get("/api/v1/claude-account")
    assert resp.status_code in (401, 403)


# ── 2. Agents that still run with the old token ───────────────────────────


async def test_lists_running_claude_agents_started_before_the_token_changed(
    auth_client, docker, make_agent
):
    now = datetime.now(timezone.utc)
    await _store_token(at=now - timedelta(minutes=5))
    claude_rt = await _runtime("anthropic-claude-sonnet", "anthropic_cloud")
    local_rt = await _runtime("local-vllm", "vllm")

    old = await make_agent(name="Old Claude", runtime_id=claude_rt.id)
    fresh = await make_agent(name="Fresh Claude", runtime_id=claude_rt.id)
    stopped = await make_agent(name="Stopped Claude", runtime_id=claude_rt.id)
    await make_agent(name="Host Claude", runtime_id=claude_rt.id, agent_runtime="host")
    other = await make_agent(name="Local Model", runtime_id=local_rt.id)

    docker.started_at = {
        "mc-agent-old-claude": ("running", _iso(now - timedelta(hours=2))),
        "mc-agent-fresh-claude": ("running", _iso(now - timedelta(minutes=1))),
        "mc-agent-stopped-claude": ("exited", _iso(now - timedelta(hours=2))),
        "mc-agent-local-model": ("running", _iso(now - timedelta(hours=2))),
    }

    body = (await auth_client.get("/api/v1/claude-account")).json()
    assert [a["id"] for a in body["stale_agents"]] == [str(old.id)]
    assert body["stale_agents"][0]["name"] == "Old Claude"
    assert body["stale_agents"][0]["busy"] is False
    assert body["claude_agents"] == 3  # docker agents on Claude (host one counted apart)
    assert body["host_agents"] == 1
    ids = {a["id"] for a in body["stale_agents"]}
    assert str(fresh.id) not in ids and str(stopped.id) not in ids and str(other.id) not in ids
    # One inspect call for all containers, and nothing that acts on them.
    assert docker.verbs() == ["inspect"]


async def test_a_busy_stale_agent_is_marked_busy(auth_client, docker, make_agent, make_board, make_task):
    now = datetime.now(timezone.utc)
    await _store_token(at=now - timedelta(minutes=5))
    rt = await _runtime("anthropic-claude-opus", "anthropic_cloud")
    board = await make_board()
    agent = await make_agent(name="Worker", runtime_id=rt.id)
    task = await make_task(board.id)
    from app.models.agent import Agent

    async with _session() as s:
        row = await s.get(Agent, agent.id)
        row.current_task_id = task.id
        s.add(row)
        await s.commit()
    docker.started_at = {"mc-agent-worker": ("running", _iso(now - timedelta(hours=1)))}

    stale = (await auth_client.get("/api/v1/claude-account")).json()["stale_agents"]
    assert stale == [
        {"id": str(agent.id), "name": "Worker", "busy": True, "started_at": stale[0]["started_at"]}
    ]


async def test_docker_unavailable_lists_no_stale_agents_instead_of_failing(
    auth_client, docker, make_agent
):
    await _store_token(at=datetime.now(timezone.utc))
    rt = await _runtime("anthropic-claude-haiku", "anthropic_cloud")
    await make_agent(name="Somebody", runtime_id=rt.id)
    docker.started_at = {}
    resp = await auth_client.get("/api/v1/claude-account")
    assert resp.status_code == 200
    assert resp.json()["stale_agents"] == []


# ── 3. Reconnect via ``claude setup-token`` ───────────────────────────────


async def test_reconnect_starts_setup_token_in_a_throwaway_container(auth_client, docker):
    resp = await auth_client.post("/api/v1/claude-account/login")
    assert resp.status_code == 200
    assert resp.json()["state"] == "starting"

    run = next(c for c in docker.calls if c[0] == "run")
    assert "--rm" in run
    assert any(a.startswith("mc-claude-login-") for a in run)
    assert any("mc-claude-agent" in a for a in run)
    # No secret goes into the throwaway container.
    assert not any("TOKEN" in a for a in run)
    tmux = next(c for c in docker.calls if c[0] == "exec" and "new-session" in c)
    assert tmux[-2:] == ["claude", "setup-token"]


async def test_the_sign_in_link_is_shown_once_claude_prints_it(auth_client, docker):
    await auth_client.post("/api/v1/claude-account/login")
    docker.pane = (
        "Browser didn't open? Use the url below to sign in:\n\n"
        f"{LOGIN_URL}\n\n"
        "Paste code here if prompted >\n"
    )
    body = (await auth_client.get("/api/v1/claude-account/login")).json()
    assert body["state"] == "waiting_for_code"
    assert body["url"] == LOGIN_URL


async def test_submitting_the_code_stores_the_token_and_runs_the_check_once(
    auth_client, docker, claude_check, caplog
):
    caplog.set_level(logging.DEBUG)
    await auth_client.post("/api/v1/claude-account/login")
    docker.pane = f"Use the url below to sign in:\n{LOGIN_URL}\nPaste code here if prompted >\n"
    await auth_client.get("/api/v1/claude-account/login")

    docker.pane += (
        "\n✓ Long-lived authentication token created successfully!\n\n"
        f"Your OAuth token (valid for 1 year):\n\n{FAKE_TOKEN}\n\n"
        "Store this token securely.\n"
    )
    resp = await auth_client.post("/api/v1/claude-account/login/code", json={"code": "abc123#state-xyz"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "connected"

    # The code went in as literal keys, then Enter.
    sends = [c for c in docker.calls if c[0] == "exec" and "send-keys" in c]
    assert sends[0][-2:] == ["-l", "abc123#state-xyz"]
    assert sends[1][-1] == "Enter"

    # Stored in the vault through the shared helper, and checked once (E8a: on save).
    from app.services.secrets_helper import get_secret_plaintext_by_key

    async with _session() as s:
        assert await get_secret_plaintext_by_key(s, TOKEN_KEY) == FAKE_TOKEN
    assert claude_check == [FAKE_TOKEN]
    status = (await auth_client.get("/api/v1/claude-account")).json()
    assert status["check"]["state"] == "ok"

    # The throwaway container is gone, so is its scrollback.
    assert ["rm", "-f"] == next(c for c in docker.calls if c[0] == "rm")[:2]

    # The token is never echoed: not in any response, not in the log.
    assert FAKE_TOKEN not in resp.text
    assert FAKE_TOKEN not in (await auth_client.get("/api/v1/claude-account/login")).text
    assert FAKE_TOKEN not in caplog.text


async def test_a_code_with_control_characters_is_refused(auth_client, docker):
    await auth_client.post("/api/v1/claude-account/login")
    resp = await auth_client.post("/api/v1/claude-account/login/code", json={"code": "abc\n; rm -rf /"})
    assert resp.status_code == 422
    assert not [c for c in docker.calls if "send-keys" in c]


async def test_code_without_a_running_login_is_a_conflict(auth_client, docker):
    resp = await auth_client.post("/api/v1/claude-account/login/code", json={"code": "abc123"})
    assert resp.status_code == 409


async def test_claude_refusing_the_code_reads_failed_without_storing(auth_client, docker, claude_check):
    await auth_client.post("/api/v1/claude-account/login")
    docker.pane = f"{LOGIN_URL}\nPaste code here if prompted >\n"
    await auth_client.get("/api/v1/claude-account/login")
    docker.pane += "\nOAuth error: Invalid code. Please make sure the full code was copied\n"
    body = (await auth_client.post("/api/v1/claude-account/login/code", json={"code": "wrong"})).json()
    assert body["state"] == "failed"
    assert body["error"]
    from app.services.secrets_helper import get_secret_plaintext_by_key

    async with _session() as s:
        assert await get_secret_plaintext_by_key(s, TOKEN_KEY) is None
    assert claude_check == []


async def test_image_missing_reads_failed_with_a_reason(auth_client, docker):
    docker.fail_run = True
    body = (await auth_client.post("/api/v1/claude-account/login")).json()
    assert body["state"] == "failed"
    assert body["error"]


async def test_cancel_removes_the_container(auth_client, docker):
    await auth_client.post("/api/v1/claude-account/login")
    resp = await auth_client.delete("/api/v1/claude-account/login")
    assert resp.status_code == 200
    assert resp.json()["state"] == "idle"
    assert any(c[:2] == ["rm", "-f"] for c in docker.calls)


async def test_a_new_login_replaces_an_unfinished_one(auth_client, docker):
    await auth_client.post("/api/v1/claude-account/login")
    first = next(c for c in docker.calls if c[0] == "run")
    name = first[first.index("--name") + 1]
    await auth_client.post("/api/v1/claude-account/login")
    assert ["rm", "-f", name] in docker.calls
