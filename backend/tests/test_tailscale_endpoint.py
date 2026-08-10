"""Tailscale-vs-LAN endpoint preference (fix/fix-tailscale-endpoint).

Live incident: a runtime endpoint was built from Host.ssh_host, which held
the box's LAN IP. SSH from the backend container tolerated that fine; an HTTP
call from a host agent (launchd/tmux on the Mac) did not, because a Tailscale
route on that machine hijacks the LAN IP
(memory: spark-tailscale-route-hijack-host-agents). Three things are tested
here:
  (a) address classification (tailscale / lan / public / unknown),
  (b) endpoint construction prefers a known Tailscale address over ssh_host,
  (c) a failing probe against a LAN endpoint on a host with a known Tailscale
      address produces a concrete, actionable warning — not a silent rewrite,
  (d) a pure-LAN host (no Tailscale address on file) gets no such warning —
      not every box has Tailscale,
  (e) IPv6 literals and plain hostnames never raise.

No DB/Redis connection to anything real — async_session/fake_redis fixtures
(same autouse pattern as test_switch_grace_recovery.py).
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.activity import ActivityEvent
from app.models.runtime import Runtime
from app.services import address_classify
from app.services.host_resolver import ResolvedHost
from app.services.agent_runtime_switch import ProbedModel
from app.services.runtime_manager import _host_ip
from app.services.runtime_watcher import UNREACHABLE_EVENT_THRESHOLD, RuntimeWatcher


def _fake_get_redis(fake_redis):
    async def _get():
        return fake_redis
    return _get


async def _mk_runtime(session: AsyncSession, **overrides) -> Runtime:
    fields = dict(
        slug="ts-rt",
        display_name="TS RT",
        runtime_type="vllm_docker",
        endpoint="http://10.20.30.40:8000/v1",
        model_identifier="some-model",
        launch_command="uvx sparkrun run @official/x --solo",
        enabled=True,
    )
    fields.update(overrides)
    rt = Runtime(**fields)
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


async def _events(session: AsyncSession) -> list[ActivityEvent]:
    result = await session.exec(select(ActivityEvent))
    return list(result.all())


# ── (a) address classification ───────────────────────────────────────────


@pytest.mark.parametrize(
    "address, expected",
    [
        ("100.100.200.50", address_classify.CLASS_TAILSCALE),
        ("10.20.30.40", address_classify.CLASS_LAN),
        ("10.0.0.5", address_classify.CLASS_LAN),
        ("172.20.1.1", address_classify.CLASS_LAN),
        ("8.8.8.8", address_classify.CLASS_PUBLIC),
        ("sparky.tail1234.ts.net", address_classify.CLASS_TAILSCALE),
        # URLs and host:port pairs classify by their host part.
        ("http://100.100.200.50:8000/v1", address_classify.CLASS_TAILSCALE),
        ("http://10.20.30.40:8000/v1", address_classify.CLASS_LAN),
        ("sparky.tail1234.ts.net:8000", address_classify.CLASS_TAILSCALE),
    ],
)
def test_classify_address(address, expected):
    assert address_classify.classify_address(address) == expected


def test_classify_address_empty_and_none():
    assert address_classify.classify_address(None) == address_classify.CLASS_UNKNOWN
    assert address_classify.classify_address("") == address_classify.CLASS_UNKNOWN


# ── (e) IPv6 + hostnames never raise ─────────────────────────────────────


@pytest.mark.parametrize(
    "address",
    [
        "::1",
        "fe80::1",
        "2001:4860:4860::8888",  # a public IPv6 address (Google DNS)
        "[::1]:8000",
        "[2001:4860:4860::8888]:443",
        "some-random-hostname",
        "box.internal",
        "not a url at all $$$",
    ],
)
def test_classify_address_never_raises(address):
    result = address_classify.classify_address(address)
    assert result in (
        address_classify.CLASS_TAILSCALE,
        address_classify.CLASS_LAN,
        address_classify.CLASS_PUBLIC,
        address_classify.CLASS_UNKNOWN,
    )


def test_classify_address_ipv6_specific_cases():
    # Loopback/link-local IPv6 are private → 'lan', not 'unknown' or a crash.
    assert address_classify.classify_address("::1") == address_classify.CLASS_LAN
    assert address_classify.classify_address("fe80::1") == address_classify.CLASS_LAN
    # A public IPv6 address classifies as public.
    assert (
        address_classify.classify_address("2001:4860:4860::8888")
        == address_classify.CLASS_PUBLIC
    )
    # Plain hostnames that aren't *.ts.net can't be classified without a DNS
    # lookup — 'unknown', not a guess.
    assert (
        address_classify.classify_address("box.internal")
        == address_classify.CLASS_UNKNOWN
    )


# ── (b) endpoint construction prefers Tailscale ──────────────────────────


def test_host_ip_prefers_tailscale_when_both_known():
    host = ResolvedHost(
        ssh_host="10.20.30.40", tailscale_host="100.100.200.50", kind="ssh"
    )
    assert _host_ip(host) == "100.100.200.50"


def test_host_ip_falls_back_to_ssh_host_without_tailscale():
    host = ResolvedHost(ssh_host="10.20.30.40", tailscale_host=None, kind="ssh")
    assert _host_ip(host) == "10.20.30.40"


def test_host_ip_ignores_bogus_tailscale_value():
    """A typo'd LAN IP entered as tailscale_host must not silently become the
    endpoint — only a value that actually classifies as Tailscale wins."""
    host = ResolvedHost(
        ssh_host="10.20.30.40", tailscale_host="10.0.0.9", kind="ssh"
    )
    assert _host_ip(host) == "10.20.30.40"


def test_suggest_endpoint_fix_rewrites_lan_endpoint():
    fixed = address_classify.suggest_endpoint_fix(
        "http://10.20.30.40:8000/v1", "100.100.200.50"
    )
    assert fixed == "http://100.100.200.50:8000/v1"


def test_suggest_endpoint_fix_no_tailscale_known():
    assert address_classify.suggest_endpoint_fix(
        "http://10.20.30.40:8000/v1", None
    ) is None


def test_suggest_endpoint_fix_endpoint_already_tailscale():
    assert address_classify.suggest_endpoint_fix(
        "http://100.100.200.50:8000/v1", "100.100.200.50"
    ) is None


# ── (c) failing probe + known Tailscale address → concrete warning ──────


@pytest.mark.asyncio
async def test_unreachable_event_suggests_tailscale_fix(
    async_session: AsyncSession, fake_redis
):
    rt = await _mk_runtime(async_session, endpoint="http://10.20.30.40:8000/v1")
    resolved = ResolvedHost(
        ssh_host="10.20.30.40", tailscale_host="100.100.200.50", kind="ssh"
    )
    watcher = RuntimeWatcher(interval=90)

    with (
        patch("app.services.runtime_watcher.probe_runtime_model_info",
              new=AsyncMock(return_value=ProbedModel(None, None))),
        patch("app.services.runtime_watcher.get_redis", _fake_get_redis(fake_redis)),
        patch("app.services.runtime_watcher.resolve_host_for_runtime",
              new=AsyncMock(return_value=resolved)),
    ):
        for _ in range(UNREACHABLE_EVENT_THRESHOLD):
            await watcher.tick(session=async_session)

    events = await _events(async_session)
    unreachable = [e for e in events if e.event_type == "runtime.unreachable"]
    assert len(unreachable) == 1
    detail = unreachable[0].detail or {}
    assert detail.get("suggested_endpoint") == "http://100.100.200.50:8000/v1"
    assert "100.100.200.50" in unreachable[0].title


# ── (d) pure-LAN host, no Tailscale address on file → no warning ────────


@pytest.mark.asyncio
async def test_unreachable_event_no_suggestion_without_tailscale_address(
    async_session: AsyncSession, fake_redis
):
    rt = await _mk_runtime(async_session, endpoint="http://10.20.30.40:8000/v1")
    resolved = ResolvedHost(ssh_host="10.20.30.40", tailscale_host=None, kind="ssh")
    watcher = RuntimeWatcher(interval=90)

    with (
        patch("app.services.runtime_watcher.probe_runtime_model_info",
              new=AsyncMock(return_value=ProbedModel(None, None))),
        patch("app.services.runtime_watcher.get_redis", _fake_get_redis(fake_redis)),
        patch("app.services.runtime_watcher.resolve_host_for_runtime",
              new=AsyncMock(return_value=resolved)),
    ):
        for _ in range(UNREACHABLE_EVENT_THRESHOLD):
            await watcher.tick(session=async_session)

    events = await _events(async_session)
    unreachable = [e for e in events if e.event_type == "runtime.unreachable"]
    assert len(unreachable) == 1
    detail = unreachable[0].detail or {}
    assert "suggested_endpoint" not in detail
    assert json.dumps(detail)  # detail stays JSON-serializable either way
