"""Tests fuer /agent/tasks/{id}/visual-verify Endpoint + TelegramReports Media-Group.

Testet den Integrations-Layer zwischen MC-Backend und mc-playwright Service.
Der echte Playwright-Call wird gemockt — mc-playwright-Container wird in
einem separaten Live-Test mitgetestet.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _setup_agent_with_task():
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="VerifyTest", slug=f"vt-{uuid.uuid4().hex[:6]}"))
        token_raw, token_hash = generate_agent_token()
        s.add(Agent(
            id=agent_id, name="VerifyAgent", role="tester",
            board_id=board_id, agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write", "chat:write"],
            provision_status="provisioned",
        ))
        s.add(Task(
            id=task_id, board_id=board_id, title="Verify Task",
            status="in_progress",
            assigned_agent_id=agent_id, owner_agent_id=agent_id,
        ))
        await s.commit()

    return {"board_id": board_id, "agent_id": agent_id, "task_id": task_id, "token": token_raw}


def _fake_verify_result():
    return {
        "screenshots": [
            {"path": "/shared-deliverables/TASK/verify-desktop.png", "viewport": "desktop", "bytes": 123456},
            {"path": "/shared-deliverables/TASK/verify-mobile.png", "viewport": "mobile", "bytes": 87654},
        ],
        "scroll_shots": [
            {"path": "/shared-deliverables/TASK/scroll-top.png", "position": "top", "bytes": 10000},
            {"path": "/shared-deliverables/TASK/scroll-middle.png", "position": "middle", "bytes": 10000},
            {"path": "/shared-deliverables/TASK/scroll-bottom.png", "position": "bottom", "bytes": 10000},
        ],
        "metrics": {
            "url": "https://preview.example.com", "status_code": 200,
            "ttfb_ms": 120, "fcp_ms": 450, "lcp_ms": 600,
            "total_bytes": 12345, "load_total_ms": 800,
        },
    }


@pytest.mark.asyncio
async def test_visual_verify_registers_deliverables(client, fake_redis):
    """Alle Screenshots+Scroll-Shots werden als TaskDeliverable registriert."""
    data = await _setup_agent_with_task()
    fake = _fake_verify_result()

    mock_reports = MagicMock()
    mock_reports.configured = True
    mock_reports.send_media_group = AsyncMock(return_value={"ok": True})

    with patch("app.services.visual_verifier.verify_url", new_callable=AsyncMock, return_value=fake), \
         patch("app.services.visual_verifier.telegram_reports", mock_reports):
        r = await client.post(
            f"/api/v1/agent/tasks/{data['task_id']}/visual-verify",
            json={"url": "https://preview.example.com", "viewports": ["desktop", "mobile"]},
            headers={"Authorization": f"Bearer {data['token']}"},
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["deliverables_registered"] == 5  # 2 screenshots + 3 scroll_shots
    assert len(body["screenshots"]) == 2

    # In DB: 5 TaskDeliverable-Rows mit type=screenshot
    from app.models.deliverable import TaskDeliverable
    from sqlmodel import select
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        all_d = (await s.exec(
            select(TaskDeliverable).where(TaskDeliverable.task_id == data["task_id"])
        )).all()
        assert len(all_d) == 5
        for d in all_d:
            assert d.deliverable_type == "screenshot"


@pytest.mark.asyncio
async def test_visual_verify_sends_telegram_media_group(client, fake_redis):
    """Bei send_to_telegram=true (default) wird send_media_group aufgerufen."""
    data = await _setup_agent_with_task()
    fake = _fake_verify_result()

    mock_reports = MagicMock()
    mock_reports.configured = True
    mock_reports.send_media_group = AsyncMock(return_value={"ok": True})

    with patch("app.services.visual_verifier.verify_url", new_callable=AsyncMock, return_value=fake), \
         patch("app.services.visual_verifier.telegram_reports", mock_reports):
        r = await client.post(
            f"/api/v1/agent/tasks/{data['task_id']}/visual-verify",
            json={"url": "https://preview.example.com"},
            headers={"Authorization": f"Bearer {data['token']}"},
        )

    assert r.status_code == 200
    assert r.json()["telegram_sent"] is True
    mock_reports.send_media_group.assert_awaited_once()
    call_args = mock_reports.send_media_group.await_args
    # caption sollte Metrics-Block enthalten
    assert "Performance" in call_args.kwargs.get("caption", "") or \
           "Performance" in (call_args.args[1] if len(call_args.args) > 1 else "")


@pytest.mark.asyncio
async def test_visual_verify_respects_no_telegram(client, fake_redis):
    """send_to_telegram=false → media_group nicht aufgerufen."""
    data = await _setup_agent_with_task()
    fake = _fake_verify_result()

    mock_reports = MagicMock()
    mock_reports.configured = True
    mock_reports.send_media_group = AsyncMock()

    with patch("app.services.visual_verifier.verify_url", new_callable=AsyncMock, return_value=fake), \
         patch("app.services.visual_verifier.telegram_reports", mock_reports):
        r = await client.post(
            f"/api/v1/agent/tasks/{data['task_id']}/visual-verify",
            json={"url": "https://preview.example.com", "send_to_telegram": False},
            headers={"Authorization": f"Bearer {data['token']}"},
        )

    assert r.status_code == 200
    assert r.json()["telegram_sent"] is False
    mock_reports.send_media_group.assert_not_called()


@pytest.mark.asyncio
async def test_visual_verify_rejects_foreign_task(client, fake_redis):
    """Agent darf nicht visual-verify auf fremdem Task (Ownership-Check)."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    data = await _setup_agent_with_task()

    # Fremder Task
    foreign_task_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        other_id = uuid.uuid4()
        s.add(Agent(
            id=other_id, name="OtherOwner", role="developer",
            board_id=data["board_id"], agent_token_hash=generate_agent_token()[1],
            scopes=["tasks:read"],             provision_status="provisioned",
        ))
        s.add(Task(
            id=foreign_task_id, board_id=data["board_id"], title="Foreign Task",
            status="in_progress",
            assigned_agent_id=other_id, owner_agent_id=other_id,
        ))
        await s.commit()

    r = await client.post(
        f"/api/v1/agent/tasks/{foreign_task_id}/visual-verify",
        json={"url": "https://evil.example.com"},
        headers={"Authorization": f"Bearer {data['token']}"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_visual_verify_503_when_playwright_unreachable(client, fake_redis):
    """Wenn mc-playwright unreachable ist → 503 mit klarer Message."""
    import httpx
    data = await _setup_agent_with_task()

    with patch(
        "app.services.visual_verifier.verify_url",
        new_callable=AsyncMock,
        side_effect=httpx.ConnectError("Connection refused"),
    ):
        r = await client.post(
            f"/api/v1/agent/tasks/{data['task_id']}/visual-verify",
            json={"url": "https://preview.example.com"},
            headers={"Authorization": f"Bearer {data['token']}"},
        )

    assert r.status_code == 503
    assert "unreachable" in r.json()["detail"].lower() or "mc-playwright" in r.json()["detail"].lower()


# ────────────────────────────────────────────────────────────────────
# TelegramReports Media-Group Unit-Tests
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_media_group_falls_back_to_single_photo_for_one_item(tmp_path):
    """Media-Group mit nur 1 Photo → fallback auf send_photo."""
    from app.services.telegram_reports import TelegramReportsService
    svc = TelegramReportsService()
    svc._token = "fake:token"
    svc._chat_id = "123"

    # Dummy-PNG
    p = tmp_path / "one.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

    mock_send_photo = AsyncMock(return_value={"ok": True, "message_id": 99})
    with patch.object(TelegramReportsService, "send_photo", mock_send_photo):
        r = await svc.send_media_group([str(p)], caption="test")

    assert r == {"ok": True, "message_id": 99}
    mock_send_photo.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_media_group_skips_missing_files(tmp_path):
    """Nicht-existente Files werden gefiltert, kein Crash."""
    from app.services.telegram_reports import TelegramReportsService
    svc = TelegramReportsService()
    svc._token = "fake:token"
    svc._chat_id = "123"

    # Nur 1 Datei existiert
    p_real = tmp_path / "real.png"
    p_real.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

    mock_send_photo = AsyncMock(return_value={"ok": True, "message_id": 1})
    with patch.object(TelegramReportsService, "send_photo", mock_send_photo):
        # 3 paths, nur real.png existiert → fallback auf single
        r = await svc.send_media_group([
            str(p_real), "/nope/missing1.png", "/nope/missing2.png",
        ])

    # Hat single-send fallback genutzt
    assert r == {"ok": True, "message_id": 1}


# ────────────────────────────────────────────────────────────────────
# Interaktions-Mode (auth_token, credential_id, interactions, wait_for)
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_visual_verify_passes_interaction_params_through(client, fake_redis):
    """auth_token, interactions, wait_for_selector, full_page werden an verify_url durchgereicht."""
    data = await _setup_agent_with_task()
    fake = _fake_verify_result()

    captured_kwargs: dict = {}

    async def _capture(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return fake

    mock_reports = MagicMock()
    mock_reports.configured = True
    mock_reports.send_media_group = AsyncMock(return_value={"ok": True})

    with patch("app.services.visual_verifier.verify_url", side_effect=_capture), \
         patch("app.services.visual_verifier.telegram_reports", mock_reports):
        r = await client.post(
            f"/api/v1/agent/tasks/{data['task_id']}/visual-verify",
            json={
                "url": "http://caddy/",
                "auth_token": "fake-jwt-token",
                "interactions": [
                    {"action": "click", "selector": "button.foo"},
                    {"action": "wait_for", "selector": "#modal"},
                ],
                "wait_for_selector": "#modal-body",
                "full_page": False,
                "send_to_telegram": False,
            },
            headers={"Authorization": f"Bearer {data['token']}"},
        )

    assert r.status_code == 200, r.text
    assert captured_kwargs.get("auth_token") == "fake-jwt-token"
    assert captured_kwargs.get("wait_for_selector") == "#modal-body"
    assert captured_kwargs.get("full_page") is False
    interactions = captured_kwargs.get("interactions")
    assert interactions and len(interactions) == 2
    assert interactions[0]["action"] == "click"
    assert interactions[0]["selector"] == "button.foo"
    assert interactions[1]["action"] == "wait_for"


@pytest.mark.asyncio
async def test_visual_verify_credential_id_resolves_from_vault(client, fake_redis):
    """credential_id → Backend entschluesselt Vault → login-dict an verify_url."""
    import json as _json
    from app.models.credential import Credential
    from app.services.encryption import encrypt

    data = await _setup_agent_with_task()

    # Tester-Agent muss CREDENTIALS_READ Scope fuer diesen Test haben
    from app.models.agent import Agent
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        a = await s.get(Agent, data["agent_id"])
        a.scopes = list(a.scopes or []) + ["credentials:read"]
        await s.commit()

    cred_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Credential(
            id=cred_id, name="MC Login",
            credential_type="login",
            encrypted_data=encrypt(_json.dumps({"username": "mark@example.com", "password": "s3cret"})),
            url="http://caddy/login",
        ))
        await s.commit()

    captured_kwargs: dict = {}

    async def _capture(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return _fake_verify_result()

    mock_reports = MagicMock()
    mock_reports.configured = True
    mock_reports.send_media_group = AsyncMock(return_value={"ok": True})

    with patch("app.services.visual_verifier.verify_url", side_effect=_capture), \
         patch("app.services.visual_verifier.telegram_reports", mock_reports):
        r = await client.post(
            f"/api/v1/agent/tasks/{data['task_id']}/visual-verify",
            json={
                "url": "http://caddy/",
                "credential_id": str(cred_id),
                "send_to_telegram": False,
            },
            headers={"Authorization": f"Bearer {data['token']}"},
        )

    assert r.status_code == 200, r.text
    login = captured_kwargs.get("login")
    assert login is not None
    assert login["url"] == "http://caddy/login"
    assert login["username"] == "mark@example.com"
    assert login["password"] == "s3cret"


@pytest.mark.asyncio
async def test_visual_verify_credential_id_requires_credentials_read_scope(client, fake_redis):
    """Agent ohne credentials:read bekommt 403 wenn credential_id mitgeschickt wird."""
    from app.models.credential import Credential
    from app.services.encryption import encrypt
    import json as _json

    data = await _setup_agent_with_task()
    # Default-Tester in _setup hat nur tasks:read, tasks:write, chat:write
    # → fehlt credentials:read

    cred_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Credential(
            id=cred_id, name="Locked",
            credential_type="login",
            encrypted_data=encrypt(_json.dumps({"username": "u", "password": "p"})),
            url="http://caddy/login",
        ))
        await s.commit()

    r = await client.post(
        f"/api/v1/agent/tasks/{data['task_id']}/visual-verify",
        json={
            "url": "http://caddy/",
            "credential_id": str(cred_id),
            "send_to_telegram": False,
        },
        headers={"Authorization": f"Bearer {data['token']}"},
    )

    assert r.status_code == 403
    assert "credentials:read" in r.json()["detail"]


@pytest.mark.asyncio
async def test_visual_verify_credential_id_404_when_missing(client, fake_redis):
    """credential_id das nicht existiert → 404."""
    data = await _setup_agent_with_task()
    # Scope hinzufuegen damit wir nicht am 403 haengen bleiben
    from app.models.agent import Agent
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        a = await s.get(Agent, data["agent_id"])
        a.scopes = list(a.scopes or []) + ["credentials:read"]
        await s.commit()

    r = await client.post(
        f"/api/v1/agent/tasks/{data['task_id']}/visual-verify",
        json={
            "url": "http://caddy/",
            "credential_id": str(uuid.uuid4()),
            "send_to_telegram": False,
        },
        headers={"Authorization": f"Bearer {data['token']}"},
    )

    assert r.status_code == 404
    assert "Credential" in r.json()["detail"]


async def _patch_agent_scoped_redis(monkeypatch, fake_redis):
    """Patcht `from app.redis_client import get_redis` in agent_scoped auf fakeredis."""
    import app.redis_client as rc
    async def _fake_get_redis():
        return fake_redis
    monkeypatch.setattr(rc, "get_redis", _fake_get_redis)


@pytest.mark.asyncio
async def test_visual_verify_dedup_skips_second_send(client, fake_redis, monkeypatch):
    """Zweite visual-verify auf derselben Task → Telegram dedup (keine 2. send)."""
    await _patch_agent_scoped_redis(monkeypatch, fake_redis)
    data = await _setup_agent_with_task()
    fake = _fake_verify_result()

    mock_reports = MagicMock()
    mock_reports.configured = True
    mock_reports.send_media_group = AsyncMock(return_value={"ok": True})

    with patch("app.services.visual_verifier.verify_url", new_callable=AsyncMock, return_value=fake), \
         patch("app.services.visual_verifier.telegram_reports", mock_reports):
        # Erste Call — sendet
        r1 = await client.post(
            f"/api/v1/agent/tasks/{data['task_id']}/visual-verify",
            json={"url": "https://example.com"},
            headers={"Authorization": f"Bearer {data['token']}"},
        )
        # Zweite Call — muss deduped sein
        r2 = await client.post(
            f"/api/v1/agent/tasks/{data['task_id']}/visual-verify",
            json={"url": "https://example.com"},
            headers={"Authorization": f"Bearer {data['token']}"},
        )

    assert r1.status_code == 200
    assert r1.json()["telegram_sent"] is True
    assert r1.json().get("telegram_skipped") is None

    assert r2.status_code == 200
    assert r2.json()["telegram_sent"] is False
    assert r2.json()["telegram_skipped"] == "already_sent"

    # send_media_group wurde nur EINMAL aufgerufen (zweite Call deduped)
    assert mock_reports.send_media_group.await_count == 1


@pytest.mark.asyncio
async def test_visual_verify_force_resend_overrides_dedup(client, fake_redis, monkeypatch):
    """force_telegram_resend=True überschreibt Dedup — beide Calls senden."""
    await _patch_agent_scoped_redis(monkeypatch, fake_redis)
    data = await _setup_agent_with_task()
    fake = _fake_verify_result()

    mock_reports = MagicMock()
    mock_reports.configured = True
    mock_reports.send_media_group = AsyncMock(return_value={"ok": True})

    with patch("app.services.visual_verifier.verify_url", new_callable=AsyncMock, return_value=fake), \
         patch("app.services.visual_verifier.telegram_reports", mock_reports):
        r1 = await client.post(
            f"/api/v1/agent/tasks/{data['task_id']}/visual-verify",
            json={"url": "https://example.com"},
            headers={"Authorization": f"Bearer {data['token']}"},
        )
        # Zweite Call mit force_telegram_resend=True
        r2 = await client.post(
            f"/api/v1/agent/tasks/{data['task_id']}/visual-verify",
            json={"url": "https://example.com", "force_telegram_resend": True},
            headers={"Authorization": f"Bearer {data['token']}"},
        )

    assert r1.json()["telegram_sent"] is True
    assert r2.json()["telegram_sent"] is True
    assert r2.json().get("telegram_skipped") is None
    # Beide Calls haben send_media_group getriggert
    assert mock_reports.send_media_group.await_count == 2


@pytest.mark.asyncio
async def test_visual_verify_dedup_scoped_per_task(client, fake_redis, monkeypatch):
    """Dedup ist per-task — zweite Task auf gleichem Agent sendet normal."""
    await _patch_agent_scoped_redis(monkeypatch, fake_redis)
    data1 = await _setup_agent_with_task()

    # Zweite Task gleicher Agent
    from app.models.task import Task
    task2_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Task(
            id=task2_id, board_id=data1["board_id"], title="Second Task",
            status="in_progress",
            assigned_agent_id=data1["agent_id"], owner_agent_id=data1["agent_id"],
        ))
        await s.commit()

    fake = _fake_verify_result()
    mock_reports = MagicMock()
    mock_reports.configured = True
    mock_reports.send_media_group = AsyncMock(return_value={"ok": True})

    with patch("app.services.visual_verifier.verify_url", new_callable=AsyncMock, return_value=fake), \
         patch("app.services.visual_verifier.telegram_reports", mock_reports):
        r1 = await client.post(
            f"/api/v1/agent/tasks/{data1['task_id']}/visual-verify",
            json={"url": "https://a.example.com"},
            headers={"Authorization": f"Bearer {data1['token']}"},
        )
        r2 = await client.post(
            f"/api/v1/agent/tasks/{task2_id}/visual-verify",
            json={"url": "https://b.example.com"},
            headers={"Authorization": f"Bearer {data1['token']}"},
        )

    assert r1.json()["telegram_sent"] is True
    assert r2.json()["telegram_sent"] is True
    # Beide Tasks senden — Dedup ist per-task, nicht per-agent
    assert mock_reports.send_media_group.await_count == 2


@pytest.mark.asyncio
async def test_visual_verify_inline_login_beats_credential_id(client, fake_redis):
    """Wenn inline login UND credential_id gesetzt → inline login gewinnt."""
    import json as _json
    from app.models.credential import Credential
    from app.models.agent import Agent
    from app.services.encryption import encrypt

    data = await _setup_agent_with_task()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        a = await s.get(Agent, data["agent_id"])
        a.scopes = list(a.scopes or []) + ["credentials:read"]
        await s.commit()

    cred_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Credential(
            id=cred_id, name="Vault Login",
            credential_type="login",
            encrypted_data=encrypt(_json.dumps({"username": "from_vault", "password": "vault_pass"})),
            url="http://caddy/login",
        ))
        await s.commit()

    captured_kwargs: dict = {}

    async def _capture(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return _fake_verify_result()

    mock_reports = MagicMock()
    mock_reports.configured = True
    mock_reports.send_media_group = AsyncMock(return_value={"ok": True})

    with patch("app.services.visual_verifier.verify_url", side_effect=_capture), \
         patch("app.services.visual_verifier.telegram_reports", mock_reports):
        r = await client.post(
            f"/api/v1/agent/tasks/{data['task_id']}/visual-verify",
            json={
                "url": "http://caddy/",
                "credential_id": str(cred_id),
                "login": {
                    "url": "http://caddy/login",
                    "username": "inline_user",
                    "password": "inline_pass",
                },
                "send_to_telegram": False,
            },
            headers={"Authorization": f"Bearer {data['token']}"},
        )

    assert r.status_code == 200, r.text
    login = captured_kwargs.get("login")
    assert login["username"] == "inline_user"
    assert login["password"] == "inline_pass"


@pytest.mark.asyncio
async def test_visual_verify_login_failed_returns_422(client, fake_redis):
    """Bug B (2026-04-23): mc-playwright meldet form-login als 'fertig' obwohl
    Page nach Submit auf /login bleibt → Screenshot zeigt Login-Maske statt
    eingeloggter Page. Backend MUSS das als 422 abweisen, nicht als ok=true
    durchwinken.
    """
    data = await _setup_agent_with_task()
    fake = _fake_verify_result()
    # mc-playwright meldet jetzt explizit succeeded=False
    fake["login"] = {
        "succeeded": False,
        "final_url": "http://caddy/login",
        "reason": "Page blieb nach Submit auf der Login-URL",
    }

    mock_reports = MagicMock()
    mock_reports.configured = True
    mock_reports.send_media_group = AsyncMock(return_value={"ok": True})

    with patch("app.services.visual_verifier.verify_url", new_callable=AsyncMock, return_value=fake), \
         patch("app.services.visual_verifier.telegram_reports", mock_reports):
        r = await client.post(
            f"/api/v1/agent/tasks/{data['task_id']}/visual-verify",
            json={
                "url": "http://caddy/tasks",
                "login": {
                    "url": "http://caddy/login",
                    "username": "x", "password": "y",
                },
            },
            headers={"Authorization": f"Bearer {data['token']}"},
        )

    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "form_login_failed"
    assert "Login-URL" in detail["message"]
    # KEINE Telegram-Sendung wenn Login fehlschlug
    mock_reports.send_media_group.assert_not_called()


@pytest.mark.asyncio
async def test_visual_verify_login_succeeded_passes_through(client, fake_redis):
    """Wenn mc-playwright login.succeeded=True meldet, laeuft alles normal durch.
    Sanity-Check fuer den happy-path."""
    data = await _setup_agent_with_task()
    fake = _fake_verify_result()
    fake["login"] = {
        "succeeded": True,
        "final_url": "http://caddy/tasks",
        "reason": None,
    }

    mock_reports = MagicMock()
    mock_reports.configured = True
    mock_reports.send_media_group = AsyncMock(return_value={"ok": True})

    with patch("app.services.visual_verifier.verify_url", new_callable=AsyncMock, return_value=fake), \
         patch("app.services.visual_verifier.telegram_reports", mock_reports):
        r = await client.post(
            f"/api/v1/agent/tasks/{data['task_id']}/visual-verify",
            json={
                "url": "http://caddy/tasks",
                "login": {"url": "http://caddy/login", "username": "x", "password": "y"},
                "send_to_telegram": False,
            },
            headers={"Authorization": f"Bearer {data['token']}"},
        )

    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


@pytest.mark.asyncio
async def test_visual_verify_no_login_field_does_not_break(client, fake_redis):
    """Backwards-compat: alte mc-playwright Versionen liefern keinen login-Field.
    Backend darf nicht crashen wenn key fehlt."""
    data = await _setup_agent_with_task()
    fake = _fake_verify_result()
    # Bewusst KEIN "login" key

    mock_reports = MagicMock()
    mock_reports.configured = True
    mock_reports.send_media_group = AsyncMock(return_value={"ok": True})

    with patch("app.services.visual_verifier.verify_url", new_callable=AsyncMock, return_value=fake), \
         patch("app.services.visual_verifier.telegram_reports", mock_reports):
        r = await client.post(
            f"/api/v1/agent/tasks/{data['task_id']}/visual-verify",
            json={"url": "https://preview.example.com", "send_to_telegram": False},
            headers={"Authorization": f"Bearer {data['token']}"},
        )

    assert r.status_code == 200, r.text
