"""Tests for app.services.jarvis_briefing (ADR-062).

The scheduled morning-briefing job is exercised with a fake Redis + mocked
jarvis_core (mc_client / frontier), so no network, OpenAI, or real Redis is hit.
"""
from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from app.services import jarvis_briefing as jb

ZURICH = ZoneInfo("Europe/Zurich")


# ── Pure helpers ─────────────────────────────────────────────────────────


def test_parse_hhmm_valid_and_default():
    assert jb.parse_hhmm("06:30") == (6, 30)
    assert jb.parse_hhmm("23:59") == (23, 59)
    assert jb.parse_hhmm("nonsense") == (6, 30)
    assert jb.parse_hhmm("25:00") == (6, 30)
    assert jb.parse_hhmm("07:61") == (6, 30)


def test_seconds_until_next_later_today():
    now = datetime(2026, 7, 10, 6, 0, tzinfo=ZURICH)
    # 06:30 is 30 minutes away
    assert jb.seconds_until_next(6, 30, now) == 30 * 60


def test_seconds_until_next_wraps_to_tomorrow():
    now = datetime(2026, 7, 10, 7, 0, tzinfo=ZURICH)
    # 06:30 already passed → next is tomorrow 06:30 = 23h30m away
    assert jb.seconds_until_next(6, 30, now) == (23 * 3600 + 30 * 60)


# ── Fake Redis ───────────────────────────────────────────────────────────


class _FakeRedis:
    """Minimal Redis with NX-aware set(), get(), delete()."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.set_calls: list[tuple] = []

    async def set(self, key, value, nx=False, ex=None):
        self.set_calls.append((key, value, nx, ex))
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)
        return 1


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(jb.settings, "jarvis_briefing_enabled", True)
    monkeypatch.setattr(jb.settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(jb.settings, "jarvis_frontier_model", "")
    monkeypatch.setattr(jb, "_JARVIS_CORE_OK", True)


def _patch_redis(monkeypatch, fake):
    async def _get_redis():
        return fake
    monkeypatch.setattr("app.redis_client.get_redis", _get_redis)


# ── run_briefing_once ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_briefing_once_disabled(monkeypatch):
    monkeypatch.setattr(jb.settings, "jarvis_briefing_enabled", False)
    res = await jb.run_briefing_once(datetime(2026, 7, 10, 6, 30, tzinfo=ZURICH))
    assert res == {"ok": False, "reason": "disabled"}


@pytest.mark.asyncio
async def test_run_briefing_once_happy_path(monkeypatch, enabled):
    fake = _FakeRedis()
    _patch_redis(monkeypatch, fake)

    monkeypatch.setattr(jb.mc_client, "vault_briefing", AsyncMock(return_value={"open_tasks": []}))
    monkeypatch.setattr(jb.jtools, "format_briefing_as_context", lambda b: "- Offen: 0 Tasks")
    monkeypatch.setattr(jb.frontier, "complete_text", AsyncMock(return_value="Guten Morgen — 0 offen."))
    write = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(jb.mc_client, "vault_write_note", write)

    now = datetime(2026, 7, 10, 6, 30, tzinfo=ZURICH)
    res = await jb.run_briefing_once(now)

    assert res["ok"] is True and res["date"] == "2026-07-10"
    # Vault note written with the dated title
    write.assert_awaited_once()
    assert write.await_args.kwargs["title"] == "Morgenbriefing 2026-07-10"
    # Redis holds the real text (second set overwrote the __generating__ placeholder)
    stored = json.loads(fake.store["mc:jarvis:briefing:2026-07-10"])
    assert stored["text"] == "Guten Morgen — 0 offen."


@pytest.mark.asyncio
async def test_run_briefing_once_idempotent_skip(monkeypatch, enabled):
    fake = _FakeRedis()
    # Pre-seed today's key → NX guard fails → skip.
    fake.store["mc:jarvis:briefing:2026-07-10"] = json.dumps({"date": "2026-07-10", "text": "x"})
    _patch_redis(monkeypatch, fake)

    briefing = AsyncMock()
    monkeypatch.setattr(jb.mc_client, "vault_briefing", briefing)

    now = datetime(2026, 7, 10, 6, 30, tzinfo=ZURICH)
    res = await jb.run_briefing_once(now)

    assert res["skipped"] is True
    briefing.assert_not_awaited()  # no generation on a duplicate day


@pytest.mark.asyncio
async def test_run_briefing_once_releases_guard_on_failure(monkeypatch, enabled):
    fake = _FakeRedis()
    _patch_redis(monkeypatch, fake)

    monkeypatch.setattr(jb.mc_client, "vault_briefing", AsyncMock(return_value={"open_tasks": []}))
    monkeypatch.setattr(jb.jtools, "format_briefing_as_context", lambda b: "ctx")
    monkeypatch.setattr(jb.frontier, "complete_text", AsyncMock(side_effect=RuntimeError("boom")))

    now = datetime(2026, 7, 10, 6, 30, tzinfo=ZURICH)
    res = await jb.run_briefing_once(now)

    assert res["ok"] is False
    # Guard released → a later retry can run again.
    assert "mc:jarvis:briefing:2026-07-10" not in fake.store
