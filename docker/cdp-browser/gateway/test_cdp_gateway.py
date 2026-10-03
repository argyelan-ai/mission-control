"""Unit tests for cdp_gateway's identification + ownership tracking.

Deliberately exercises only the synchronous, dependency-free parts
(`GatewayState`, `slug_from_path`, `normalize_slug`, `identify_agent_sync`,
`_ReverseDnsCache` with an injected resolver) — no real sockets, no real
Chromium, no `websockets` server. The end-to-end proxy behaviour (actually
routing bytes between an agent and Chromium) is covered by
`docker/cdp-browser/test_gateway.sh`, which runs this module against a real
built image (bauplan.md's integration test).

Run: `python3 -m pytest docker/cdp-browser/gateway/test_cdp_gateway.py -v`
(stdlib + pytest + pytest-asyncio only — matches the CI step in bauplan.md).
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from cdp_gateway import (  # noqa: E402
    GatewayState,
    _ReverseDnsCache,
    _SHARED,
    identify_agent_sync,
    normalize_slug,
    slug_from_path,
    strip_agent_prefix,
)


class FakeHeaders(dict):
    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


# ── slug parsing / validation ──────────────────────────────────────────────

def test_normalize_slug_accepts_lowercase_alnum_dash():
    assert normalize_slug("sparky") == "sparky"
    assert normalize_slug("Sparky") == "sparky"
    assert normalize_slug("agent-2") == "agent-2"


def test_normalize_slug_rejects_path_traversal_and_junk():
    assert normalize_slug("../../etc") is None
    assert normalize_slug("a/b") is None
    assert normalize_slug('a"b') is None
    assert normalize_slug("") is None
    assert normalize_slug(None) is None


def test_slug_from_path_extracts_agent_segment():
    assert slug_from_path("/a/sparky/json/version") == "sparky"
    assert slug_from_path("/a/boss") == "boss"
    assert slug_from_path("/json/version") is None
    assert slug_from_path("/a/../etc/json/version") is None


def test_strip_agent_prefix_removes_only_the_recognised_prefix():
    assert strip_agent_prefix("/a/sparky/json/version") == "/json/version"
    assert strip_agent_prefix("/a/sparky") == "/"
    assert strip_agent_prefix("/json/version") == "/json/version"
    # Sabotage probe target: an invalid slug must NOT be stripped, so a
    # malformed /a/ path falls through to Chromium unchanged (and 404s
    # there) instead of being silently rewritten.
    assert strip_agent_prefix("/a/../json/version") == "/a/../json/version"


# ── identification order: path > header > (async) reverse DNS > _shared ───

def test_identify_prefers_path_over_header():
    slug, method = identify_agent_sync(
        path="/a/alpha/json/version", headers=FakeHeaders({"X-MC-Agent": "beta"}), peer_ip="10.0.0.5",
    )
    assert (slug, method) == ("alpha", "path")


def test_identify_falls_back_to_header():
    slug, method = identify_agent_sync(
        path="/json/version", headers=FakeHeaders({"X-MC-Agent": "beta"}), peer_ip="10.0.0.5",
    )
    assert (slug, method) == ("beta", "header")


def test_identify_returns_none_when_neither_present():
    slug, method = identify_agent_sync(path="/json/version", headers=FakeHeaders(), peer_ip="10.0.0.5")
    assert (slug, method) == (None, "none")


def test_identify_ignores_malformed_header_value():
    slug, _ = identify_agent_sync(
        path="/json/version", headers=FakeHeaders({"X-MC-Agent": "../etc"}), peer_ip=None,
    )
    assert slug is None


@pytest.mark.asyncio
async def test_reverse_dns_cache_extracts_slug_from_container_name():
    def fake_resolver(ip):
        assert ip == "172.18.0.9"
        return ("mc-agent-sparky.mission-control_default", [], ["172.18.0.9"])

    cache = _ReverseDnsCache(resolver=fake_resolver)
    assert await cache.lookup_slug("172.18.0.9") == "sparky"


@pytest.mark.asyncio
async def test_reverse_dns_cache_returns_none_for_non_agent_host():
    def fake_resolver(ip):
        return ("some-other-container.mission-control_default", [], [ip])

    cache = _ReverseDnsCache(resolver=fake_resolver)
    assert await cache.lookup_slug("172.18.0.3") is None


@pytest.mark.asyncio
async def test_reverse_dns_cache_handles_lookup_failure():
    def failing_resolver(ip):
        raise OSError("no PTR record")

    cache = _ReverseDnsCache(resolver=failing_resolver)
    assert await cache.lookup_slug("172.18.0.3") is None


@pytest.mark.asyncio
async def test_reverse_dns_cache_reuses_cached_result_within_ttl():
    calls = {"n": 0}

    def counting_resolver(ip):
        calls["n"] += 1
        return ("mc-agent-sparky.net", [], [ip])

    fake_time = {"t": 0.0}
    cache = _ReverseDnsCache(now_fn=lambda: fake_time["t"], resolver=counting_resolver)
    assert await cache.lookup_slug("172.18.0.9") == "sparky"
    fake_time["t"] += 10  # well within the 60s TTL
    assert await cache.lookup_slug("172.18.0.9") == "sparky"
    assert calls["n"] == 1, "second lookup within TTL must hit the cache, not resolve again"


@pytest.mark.asyncio
async def test_reverse_dns_cache_expires_after_ttl():
    calls = {"n": 0}

    def counting_resolver(ip):
        calls["n"] += 1
        return ("mc-agent-sparky.net", [], [ip])

    fake_time = {"t": 0.0}
    cache = _ReverseDnsCache(now_fn=lambda: fake_time["t"], resolver=counting_resolver)
    await cache.lookup_slug("172.18.0.9")
    fake_time["t"] += 61
    await cache.lookup_slug("172.18.0.9")
    assert calls["n"] == 2


# ── GatewayState: ownership tracking from Target.* traffic ────────────────

def test_create_target_response_sets_owner():
    state = GatewayState(now_fn=lambda: 1.0)
    state.observe_response("Target.createTarget", {}, {"targetId": "T1"}, agent="sparky")
    assert state.owner_of("T1") == "sparky"


def test_create_target_response_ignored_for_shared_agent():
    state = GatewayState(now_fn=lambda: 1.0)
    state.observe_response("Target.createTarget", {}, {"targetId": "T1"}, agent=_SHARED)
    assert state.owner_of("T1") is None


def test_create_browser_context_then_target_created_inherits_context_owner():
    state = GatewayState(now_fn=lambda: 1.0)
    state.observe_response("Target.createBrowserContext", {}, {"browserContextId": "ctx1"}, agent="boss")
    state.apply_target_event("targetCreated", {
        "targetInfo": {"targetId": "T2", "type": "page", "browserContextId": "ctx1", "title": "", "url": ""},
    })
    assert state.owner_of("T2") is None  # target_owner unset...
    assert state.targets["T2"].agent == "boss"  # ...but attributed via the context


def test_session_activity_updates_last_active_at_without_a_target_event():
    """Covers M11: a plain tab switch (Target.activateTarget) never fires a
    Target.target* event, so attribution of 'currently active' has to come
    from traffic carrying the tab's sessionId instead."""
    times = iter([1.0, 5.0])
    state = GatewayState(now_fn=lambda: next(times))
    state.apply_target_event("targetCreated", {
        "targetInfo": {"targetId": "T1", "type": "page", "title": "", "url": "https://example.org"},
    })
    state.observe_response("Target.attachToTarget", {"targetId": "T1"}, {"sessionId": "S1"}, agent="sparky")
    state.mark_active_by_session("S1")
    assert state.targets["T1"].last_active_at == 5.0


def test_mark_active_by_session_is_a_noop_for_unknown_session():
    state = GatewayState(now_fn=lambda: 1.0)
    state.mark_active_by_session("unknown-session")  # must not raise


def test_target_destroyed_clears_ownership_too():
    state = GatewayState(now_fn=lambda: 1.0)
    state.observe_response("Target.createTarget", {}, {"targetId": "T1"}, agent="sparky")
    state.apply_target_event("targetCreated", {"targetInfo": {"targetId": "T1", "type": "page", "title": "", "url": ""}})
    state.apply_target_event("targetDestroyed", {"targetId": "T1"})
    assert "T1" not in state.targets
    assert state.owner_of("T1") is None


def test_targets_for_filters_by_agent():
    state = GatewayState(now_fn=lambda: 1.0)
    state.observe_response("Target.createTarget", {}, {"targetId": "T1"}, agent="sparky")
    state.apply_target_event("targetCreated", {"targetInfo": {"targetId": "T1", "type": "page", "title": "A", "url": "https://a"}})
    state.observe_response("Target.createTarget", {}, {"targetId": "T2"}, agent="boss")
    state.apply_target_event("targetCreated", {"targetInfo": {"targetId": "T2", "type": "page", "title": "B", "url": "https://b"}})

    sparky_ids = {t.id for t in state.targets_for("sparky")}
    boss_ids = {t.id for t in state.targets_for("boss")}
    assert sparky_ids == {"T1"}
    assert boss_ids == {"T2"}
    assert {t.id for t in state.targets_for(None)} == {"T1", "T2"}


def test_as_mc_targets_json_shape():
    state = GatewayState(now_fn=lambda: 1.0)
    state.observe_response("Target.createTarget", {}, {"targetId": "T1"}, agent="sparky")
    state.apply_target_event("targetCreated", {"targetInfo": {"targetId": "T1", "type": "page", "title": "A", "url": "https://a"}})
    [row] = state.as_mc_targets_json("sparky")
    assert row["targetId"] == "T1"
    assert row["agent"] == "sparky"
    assert row["title"] == "A"
    assert row["url"] == "https://a"


# ── sabotage probes: each must flip exactly the test(s) it breaks ─────────

def test_sabotage_removing_owner_attribution_breaks_filtering():
    """If `record_owner`/context inheritance were commented out,
    `test_targets_for_filters_by_agent` must go red — this test documents
    that expectation so a future regression that silently no-ops ownership
    tracking (e.g. an early `return` added to `apply_target_event`) is
    caught the same way."""
    state = GatewayState(now_fn=lambda: 1.0)
    state.apply_target_event("targetCreated", {"targetInfo": {"targetId": "T1", "type": "page", "title": "", "url": ""}})
    # No observe_response call at all -> must stay unattributed.
    assert state.targets["T1"].agent is None
    assert state.targets_for("sparky") == []
