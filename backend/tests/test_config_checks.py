"""Config checks (Welle 2, PR 2-1): one check registry + the ``config_checks`` table.

Pins the contract the later UI (test on save, "tested · 3 h ago") builds on:

- a wrong key is stored as ``fail`` with a short, secret-free text;
- a value nobody tested reads ``never_tested`` — also after it was replaced;
- the result and its age come from the database, so they survive a restart;
- every existing test button (Telegram, Slack, HuggingFace, embeddings,
  GitHub, host probe, runtime model probe) writes its result through the
  registry, while its old endpoint keeps its old answer;
- every check kind is one registry entry with a procedure and a cost hint.

External calls are always mocked: no test reaches a real provider.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from tests.conftest import test_engine


def _session() -> AsyncSession:
    return AsyncSession(test_engine, expire_on_commit=False)


async def _store_secret(key: str, value: str) -> None:
    from app.services.secrets_helper import upsert_secret_by_key

    async with _session() as s:
        await upsert_secret_by_key(s, key=key, value=value)


async def _rows():
    from app.models.config_check import ConfigCheck

    async with _session() as s:
        return list((await s.exec(select(ConfigCheck))).all())


def _fake_http(status_by_host: dict[str, int], calls: list | None = None):
    """httpx transport stub: answers by host name, records nothing secret."""

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        code = status_by_host.get(request.url.host, 599)
        return httpx.Response(code, json={"data": [], "ok": code == 200})

    return httpx.MockTransport(handler)


@pytest.fixture
def mock_http(monkeypatch):
    """Route every httpx.AsyncClient the check procedures open through a stub."""
    state: dict = {"status": {}, "calls": []}
    real_init = httpx.AsyncClient.__init__

    def init(self, *args, **kwargs):
        kwargs["transport"] = _fake_http(state["status"], state["calls"])
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", init)
    return state


@pytest.fixture(autouse=True)
def _own_sessions_go_to_the_test_db(monkeypatch):
    """Some readers (``ai_provider_config._secret``) open their own session;
    never let them reach a developer's real database."""
    monkeypatch.setattr("app.database.engine", test_engine)


@pytest.fixture(autouse=True)
def _restore_telegram_settings():
    keys = ["telegram_bot_token", "telegram_reports_bot_token"]
    before = {k: getattr(settings, k) for k in keys}
    yield
    for k, v in before.items():
        setattr(settings, k, v)


# ── 1. The registry itself ────────────────────────────────────────────────


def test_every_kind_is_one_entry_with_procedure_and_cost_hint():
    from app.services.config_checks import COST_HINTS, REGISTRY

    assert {"secret", "telegram", "slack", "github", "embeddings", "host", "runtime"} <= set(
        REGISTRY
    )
    for key, kind in REGISTRY.items():
        assert kind.key == key
        assert kind.label
        assert kind.cost in COST_HINTS, key
        assert callable(kind.run) and callable(kind.list_subjects)


def test_secret_testers_name_real_secret_templates_and_carry_a_cost():
    from app.routers.secrets import PROVIDER_TEMPLATES
    from app.services.config_checks import COST_HINTS
    from app.services.config_checks.kinds import SECRET_TESTERS

    template_keys = {t["key"] for t in PROVIDER_TEMPLATES}
    assert SECRET_TESTERS, "the generic secret kind needs at least one tester"
    for key, tester in SECRET_TESTERS.items():
        assert key in template_keys, key
        assert tester.cost in COST_HINTS, key
    # Decision E8a: the Claude token check is a real call that spends quota,
    # so the registry must say so (the UI runs it only on save / on request).
    assert SECRET_TESTERS["claude_code_oauth_token"].cost == "quota"


# ── 2. never tested ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_untested_secret_reads_never_tested(auth_client: AsyncClient):
    await _store_secret("openai_api_key", "sk-test-NEVER-CHECKED")

    resp = await auth_client.get("/api/v1/config-checks/secret/openai_api_key")
    assert resp.status_code == 200, resp.text
    item = resp.json()
    assert item["state"] == "never_tested"
    assert item["checked_at"] is None
    assert item["configured"] is True and item["testable"] is True
    assert item["cost"] == "free"
    assert "NEVER-CHECKED" not in resp.text


@pytest.mark.asyncio
async def test_list_shows_every_subject_including_never_tested(auth_client: AsyncClient):
    await _store_secret("openai_api_key", "sk-a")
    await _store_secret("x_api_key", "no-tester-for-this")
    settings.telegram_bot_token = ""
    settings.telegram_reports_bot_token = ""

    body = (await auth_client.get("/api/v1/config-checks")).json()
    by_ref = {(i["kind"], i["subject_id"]): i for i in body["items"]}

    assert by_ref[("secret", "openai_api_key")]["state"] == "never_tested"
    # No procedure for this key: honest "cannot be tested", never a fake green.
    assert by_ref[("secret", "x_api_key")]["testable"] is False
    # Unconfigured subjects are listed too, so the UI can say "not set".
    assert by_ref[("telegram", "command_bot")]["configured"] is False
    assert {k["key"] for k in body["kinds"]} >= {"secret", "telegram", "slack"}
    assert "sk-a" not in str(body)


# ── 3. wrong key → fail stored ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_wrong_key_is_stored_as_fail_without_the_key(
    auth_client: AsyncClient, mock_http
):
    await _store_secret("openai_api_key", "sk-WRONG-KEY-123456")
    mock_http["status"]["api.openai.com"] = 401

    resp = await auth_client.post("/api/v1/config-checks/secret/openai_api_key/run")
    assert resp.status_code == 200, resp.text
    item = resp.json()
    assert item["state"] == "fail"
    assert "401" in item["message"]
    assert item["checked_at"] is not None
    assert "WRONG-KEY" not in resp.text

    rows = await _rows()
    assert len(rows) == 1
    assert (rows[0].kind, rows[0].subject_id, rows[0].ok) == ("secret", "openai_api_key", False)
    assert "WRONG-KEY" not in rows[0].message
    # The procedure really called the provider with the stored key.
    assert mock_http["calls"][0].headers["authorization"] == "Bearer sk-WRONG-KEY-123456"

    again = (await auth_client.get("/api/v1/config-checks/secret/openai_api_key")).json()
    assert again["state"] == "fail"


@pytest.mark.asyncio
async def test_good_key_is_stored_as_ok_and_one_row_per_subject(
    auth_client: AsyncClient, mock_http
):
    await _store_secret("anthropic_api_key", "sk-ant-GOOD")
    mock_http["status"]["api.anthropic.com"] = 200

    for _ in range(2):
        item = (
            await auth_client.post("/api/v1/config-checks/secret/anthropic_api_key/run")
        ).json()
        assert item["state"] == "ok", item
    assert len(await _rows()) == 1  # latest result only, upserted


@pytest.mark.asyncio
async def test_secret_that_cannot_be_decrypted_fails_with_a_hint(auth_client: AsyncClient):
    from app.models.secret import Secret

    async with _session() as s:
        s.add(Secret(key="openai_api_key", encrypted_value="not-a-fernet-token"))
        await s.commit()

    item = (await auth_client.post("/api/v1/config-checks/secret/openai_api_key/run")).json()
    assert item["state"] == "fail"
    assert "decrypt" in item["message"].lower()


@pytest.mark.asyncio
async def test_claude_token_check_is_a_real_call_with_the_token(
    auth_client: AsyncClient, mock_http
):
    await _store_secret("claude_code_oauth_token", "sk-ant-oat01-EXPIRED")
    mock_http["status"]["api.anthropic.com"] = 401

    item = (
        await auth_client.post("/api/v1/config-checks/secret/claude_code_oauth_token/run")
    ).json()
    assert item["state"] == "fail" and item["cost"] == "quota"
    sent = mock_http["calls"][0]
    assert sent.headers["authorization"] == "Bearer sk-ant-oat01-EXPIRED"
    assert "EXPIRED" not in str(item)


# ── 4. a replaced value is no longer "tested" ────────────────────────────


@pytest.mark.asyncio
async def test_replacing_the_value_turns_the_result_into_never_tested(
    auth_client: AsyncClient, mock_http
):
    await _store_secret("openai_api_key", "sk-first")
    mock_http["status"]["api.openai.com"] = 200
    assert (
        await auth_client.post("/api/v1/config-checks/secret/openai_api_key/run")
    ).json()["state"] == "ok"

    await _store_secret("openai_api_key", "sk-second")
    item = (await auth_client.get("/api/v1/config-checks/secret/openai_api_key")).json()
    assert item["state"] == "never_tested"
    assert item["value_changed"] is True


# ── 5. the age survives a restart ────────────────────────────────────────


@pytest.mark.asyncio
async def test_result_and_age_come_from_the_database(auth_client: AsyncClient, mock_http):
    from app.models.config_check import ConfigCheck
    from app.services import config_checks

    await _store_secret("openai_api_key", "sk-persist")
    mock_http["status"]["api.openai.com"] = 200
    await auth_client.post("/api/v1/config-checks/secret/openai_api_key/run")

    # "Restart": age the row in the DB alone and read it back through a
    # fresh session — the age can only come from the stored row.
    three_days_ago = datetime.now(timezone.utc) - timedelta(days=3)
    async with _session() as s:
        row = (await s.exec(select(ConfigCheck))).one()
        row.checked_at = three_days_ago
        s.add(row)
        await s.commit()

    async with _session() as s:
        item = await config_checks.status_item(s, "secret", "openai_api_key")
    assert item["state"] == "ok"
    checked = datetime.fromisoformat(item["checked_at"])
    assert abs((checked - three_days_ago).total_seconds()) < 2
    assert item["age_seconds"] >= 3 * 86400 - 5


# ── 6. errors and auth ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_kind_or_subject_is_404(auth_client: AsyncClient):
    assert (await auth_client.get("/api/v1/config-checks/nope/x")).status_code == 404
    assert (
        await auth_client.get("/api/v1/config-checks/secret/no_such_secret")
    ).status_code == 404
    assert (
        await auth_client.post("/api/v1/config-checks/nope/x/run")
    ).status_code == 404


@pytest.mark.asyncio
async def test_untestable_and_unconfigured_runs_are_refused_and_not_stored(
    auth_client: AsyncClient,
):
    await _store_secret("x_api_key", "abc")
    settings.telegram_bot_token = ""

    r1 = await auth_client.post("/api/v1/config-checks/secret/x_api_key/run")
    assert r1.status_code == 409 and r1.json()["detail"]["code"] == "not_testable"
    r2 = await auth_client.post("/api/v1/config-checks/telegram/command_bot/run")
    assert r2.status_code == 409 and r2.json()["detail"]["code"] == "not_configured"
    assert await _rows() == []


@pytest.mark.asyncio
async def test_running_a_check_requires_admin(client: AsyncClient):
    from app.auth import create_access_token
    from app.models.user import User

    user_id = uuid.uuid4()
    async with _session() as s:
        s.add(User(id=user_id, email="v@mc.local", name="V", role="viewer", is_active=True))
        await s.commit()
    headers = {"Authorization": f"Bearer {create_access_token(str(user_id), 'viewer')}"}
    await _store_secret("openai_api_key", "sk-x")

    assert (
        await client.post("/api/v1/config-checks/secret/openai_api_key/run", headers=headers)
    ).status_code == 403
    # Reading the status (no values, no spend) stays open to signed-in users.
    assert (await client.get("/api/v1/config-checks", headers=headers)).status_code == 200


# ── 7. the old test buttons write through the registry ───────────────────


@pytest.mark.asyncio
async def test_telegram_button_records_both_bots_and_keeps_its_answer(
    auth_client: AsyncClient, monkeypatch
):
    from app.services.config_checks import kinds

    async def fake_get_me(token: str) -> dict:
        if token == "111:good":
            return {"ok": True, "result": {"username": "mc_bot"}}
        return {"ok": False, "description": "Unauthorized"}

    monkeypatch.setattr(kinds, "telegram_get_me", fake_get_me)
    settings.telegram_bot_token = "111:good"
    settings.telegram_reports_bot_token = "222:bad"

    body = (await auth_client.post("/api/v1/channels/telegram/test-connection")).json()
    assert body["command_bot"]["connected"] is True
    assert body["reports_bot"]["error"] == "Unauthorized"

    rows = {r.subject_id: r for r in await _rows() if r.kind == "telegram"}
    assert rows["command_bot"].ok is True
    assert rows["reports_bot"].ok is False
    assert "222:bad" not in rows["reports_bot"].message


@pytest.mark.asyncio
async def test_slack_button_records_its_result(auth_client: AsyncClient, monkeypatch):
    from app.services import slack_client

    async def fake_call(token: str) -> dict:
        return {"ok": False, "error": "invalid_auth"}

    monkeypatch.setattr(slack_client, "_call_auth_test", fake_call)
    await _store_secret("slack_bot_token", "xoxb-REVOKED")

    body = (await auth_client.post("/api/v1/slack/test-connection")).json()
    assert body["connected"] is False
    (row,) = [r for r in await _rows() if r.kind == "slack"]
    assert row.ok is False and row.subject_id == "workspace"
    assert "REVOKED" not in row.message


@pytest.mark.asyncio
async def test_huggingface_button_records_into_the_hf_token_secret(
    auth_client: AsyncClient, monkeypatch
):
    from app.services.config_checks import kinds

    async def fake_whoami(token: str):
        return 401, {}

    monkeypatch.setattr(kinds, "hf_whoami", fake_whoami)
    await _store_secret("hf_token", "hf_REVOKED")

    body = (await auth_client.post("/api/v1/ai-providers/huggingface/test-connection")).json()
    assert body["connected"] is False and "401" in body["error"]
    item = (await auth_client.get("/api/v1/config-checks/secret/hf_token")).json()
    assert item["state"] == "fail"


@pytest.mark.asyncio
async def test_embeddings_button_records_the_active_provider(
    auth_client: AsyncClient, monkeypatch
):
    from app.services import embedding_provider

    provider = embedding_provider.active_embedding_provider()

    async def fake_embed(text: str):
        return [0.0] * embedding_provider.EMBED_DIM

    monkeypatch.setattr(provider, "embed", fake_embed)
    body = (await auth_client.post("/api/v1/ai-providers/embeddings/test-connection")).json()
    assert body["connected"] is True
    item = (await auth_client.get(f"/api/v1/config-checks/embeddings/{provider.key}")).json()
    assert item["state"] == "ok"


@pytest.mark.asyncio
async def test_github_probe_records_its_result(auth_client: AsyncClient):
    await _store_secret("github_owner", "some-owner")
    await _store_secret("github_token", "ghp_BAD")

    async def fake_run(self, *cmd, **kw):
        raise RuntimeError("HTTP 401: Bad credentials")

    with patch("app.services.git_service.GitService._run_cmd", new=fake_run):
        body = (await auth_client.get("/api/v1/repos/github-status?probe=true")).json()
    assert body["connected"] is False
    item = (await auth_client.get("/api/v1/config-checks/github/connection")).json()
    assert item["state"] == "fail" and "401" in item["message"]
    assert "ghp_BAD" not in str(item)


@pytest.mark.asyncio
async def test_host_probe_records_for_a_stored_host_only(auth_client: AsyncClient, monkeypatch):
    from app.models.host import Host
    from app.services import host_probe

    async def fake_probe(resolved):
        return {"reachable": False, "reason": "Connection refused"}

    monkeypatch.setattr(host_probe, "probe_host", fake_probe)
    host_id = uuid.uuid4()
    async with _session() as s:
        s.add(Host(id=host_id, slug="box-a", display_name="Box A", kind="ssh",
                   ssh_host="192.0.2.10", ssh_user="ops"))
        await s.commit()

    body = (await auth_client.post("/api/v1/hosts/probe", json={"host_id": "box-a"})).json()
    assert body["reachable"] is False
    item = (await auth_client.get(f"/api/v1/config-checks/host/{host_id}")).json()
    assert item["state"] == "fail" and "refused" in item["message"]

    # Ad-hoc probe in the add-box wizard: there is no stored thing to attach to.
    await auth_client.post("/api/v1/hosts/probe", json={"ssh_host": "192.0.2.11"})
    assert len([r for r in await _rows() if r.kind == "host"]) == 1


@pytest.mark.asyncio
async def test_runtime_model_probe_records_and_keeps_its_answer(
    auth_client: AsyncClient, monkeypatch
):
    from app.models.runtime import Runtime
    from app.services import agent_runtime_switch

    async def fake_probe(_rt):
        return None  # endpoint answered nothing usable

    monkeypatch.setattr(agent_runtime_switch, "probe_runtime_model", fake_probe)
    rt_id = uuid.uuid4()
    async with _session() as s:
        s.add(Runtime(id=rt_id, slug="lms-x", display_name="LMS X", runtime_type="lmstudio",
                      endpoint="http://192.0.2.20:1234/v1", model_identifier="old"))
        await s.commit()

    body = (await auth_client.post("/api/v1/runtimes/lms-x/probe-model")).json()
    assert body["changed"] is False and body["new_model_identifier"] == "old"
    item = (await auth_client.get(f"/api/v1/config-checks/runtime/{rt_id}")).json()
    assert item["state"] == "fail"
