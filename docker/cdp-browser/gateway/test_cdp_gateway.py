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
    assert normalize_slug("alpha") == "alpha"
    assert normalize_slug("Alpha") == "alpha"
    assert normalize_slug("agent-2") == "agent-2"


def test_normalize_slug_rejects_path_traversal_and_junk():
    assert normalize_slug("../../etc") is None
    assert normalize_slug("a/b") is None
    assert normalize_slug('a"b') is None
    assert normalize_slug("") is None
    assert normalize_slug(None) is None


def test_slug_from_path_extracts_agent_segment():
    assert slug_from_path("/a/alpha/json/version") == "alpha"
    assert slug_from_path("/a/beta") == "beta"
    assert slug_from_path("/json/version") is None
    assert slug_from_path("/a/../etc/json/version") is None


def test_strip_agent_prefix_removes_only_the_recognised_prefix():
    assert strip_agent_prefix("/a/alpha/json/version") == "/json/version"
    assert strip_agent_prefix("/a/alpha") == "/"
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
        return ("mc-agent-alpha.<net>", [], ["172.18.0.9"])

    cache = _ReverseDnsCache(resolver=fake_resolver)
    assert await cache.lookup_slug("172.18.0.9") == "alpha"


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
        return ("mc-agent-alpha.<net>", [], [ip])

    fake_time = {"t": 0.0}
    cache = _ReverseDnsCache(now_fn=lambda: fake_time["t"], resolver=counting_resolver)
    assert await cache.lookup_slug("172.18.0.9") == "alpha"
    fake_time["t"] += 10  # well within the 60s TTL
    assert await cache.lookup_slug("172.18.0.9") == "alpha"
    assert calls["n"] == 1, "second lookup within TTL must hit the cache, not resolve again"


@pytest.mark.asyncio
async def test_reverse_dns_cache_expires_after_ttl():
    calls = {"n": 0}

    def counting_resolver(ip):
        calls["n"] += 1
        return ("mc-agent-alpha.<net>", [], [ip])

    fake_time = {"t": 0.0}
    cache = _ReverseDnsCache(now_fn=lambda: fake_time["t"], resolver=counting_resolver)
    await cache.lookup_slug("172.18.0.9")
    fake_time["t"] += 61
    await cache.lookup_slug("172.18.0.9")
    assert calls["n"] == 2


# ── GatewayState: ownership tracking from Target.* traffic ────────────────

def test_create_target_response_sets_owner():
    state = GatewayState(now_fn=lambda: 1.0)
    state.observe_response("Target.createTarget", {}, {"targetId": "T1"}, agent="alpha")
    assert state.owner_of("T1") == "alpha"


def test_create_target_response_ignored_for_shared_agent():
    state = GatewayState(now_fn=lambda: 1.0)
    state.observe_response("Target.createTarget", {}, {"targetId": "T1"}, agent=_SHARED)
    assert state.owner_of("T1") is None


def test_create_browser_context_then_target_created_inherits_context_owner():
    state = GatewayState(now_fn=lambda: 1.0)
    state.observe_response("Target.createBrowserContext", {}, {"browserContextId": "ctx1"}, agent="beta")
    state.apply_target_event("targetCreated", {
        "targetInfo": {"targetId": "T2", "type": "page", "browserContextId": "ctx1", "title": "", "url": ""},
    })
    assert state.owner_of("T2") is None  # target_owner unset...
    assert state.targets["T2"].agent == "beta"  # ...but attributed via the context


def test_session_activity_updates_last_active_at_without_a_target_event():
    """Covers M11: a plain tab switch (Target.activateTarget) never fires a
    Target.target* event, so attribution of 'currently active' has to come
    from traffic carrying the tab's sessionId instead."""
    times = iter([1.0, 5.0])
    state = GatewayState(now_fn=lambda: next(times))
    state.apply_target_event("targetCreated", {
        "targetInfo": {"targetId": "T1", "type": "page", "title": "", "url": "https://example.org"},
    })
    state.observe_response("Target.attachToTarget", {"targetId": "T1"}, {"sessionId": "S1"}, agent="alpha")
    state.mark_active_by_session("S1")
    assert state.targets["T1"].last_active_at == 5.0


def test_mark_active_by_session_is_a_noop_for_unknown_session():
    state = GatewayState(now_fn=lambda: 1.0)
    state.mark_active_by_session("unknown-session")  # must not raise


def test_target_destroyed_clears_ownership_too():
    state = GatewayState(now_fn=lambda: 1.0)
    state.observe_response("Target.createTarget", {}, {"targetId": "T1"}, agent="alpha")
    state.apply_target_event("targetCreated", {"targetInfo": {"targetId": "T1", "type": "page", "title": "", "url": ""}})
    state.apply_target_event("targetDestroyed", {"targetId": "T1"})
    assert "T1" not in state.targets
    assert state.owner_of("T1") is None


def test_targets_for_filters_by_agent():
    state = GatewayState(now_fn=lambda: 1.0)
    state.observe_response("Target.createTarget", {}, {"targetId": "T1"}, agent="alpha")
    state.apply_target_event("targetCreated", {"targetInfo": {"targetId": "T1", "type": "page", "title": "A", "url": "https://a"}})
    state.observe_response("Target.createTarget", {}, {"targetId": "T2"}, agent="beta")
    state.apply_target_event("targetCreated", {"targetInfo": {"targetId": "T2", "type": "page", "title": "B", "url": "https://b"}})

    alpha_ids = {t.id for t in state.targets_for("alpha")}
    beta_ids = {t.id for t in state.targets_for("beta")}
    assert alpha_ids == {"T1"}
    assert beta_ids == {"T2"}
    assert {t.id for t in state.targets_for(None)} == {"T1", "T2"}


def test_as_mc_targets_json_shape():
    state = GatewayState(now_fn=lambda: 1.0)
    state.observe_response("Target.createTarget", {}, {"targetId": "T1"}, agent="alpha")
    state.apply_target_event("targetCreated", {"targetInfo": {"targetId": "T1", "type": "page", "title": "A", "url": "https://a"}})
    [row] = state.as_mc_targets_json("alpha")
    assert row["targetId"] == "T1"
    assert row["agent"] == "alpha"
    assert row["title"] == "A"
    assert row["url"] == "https://a"


# ── sabotage probes: each must flip exactly the test(s) it breaks ─────────

def test_opener_id_inherits_opener_tabs_owner():
    """A `window.open()` popup never goes through Target.createTarget (no
    response to attribute from), but CDP's targetCreated carries the new
    tab's `openerId` — the popup belongs to whoever owns the tab that
    opened it."""
    state = GatewayState(now_fn=lambda: 1.0)
    state.observe_response("Target.createTarget", {}, {"targetId": "T1"}, agent="alpha")
    state.apply_target_event("targetCreated", {"targetInfo": {"targetId": "T1", "type": "page", "title": "", "url": ""}})
    state.apply_target_event("targetCreated", {
        "targetInfo": {"targetId": "T2", "type": "page", "title": "", "url": "", "openerId": "T1"},
    })
    assert state.targets["T2"].agent == "alpha"


def test_discovering_connection_does_not_attribute_a_foreign_targets_created_event():
    """Regression guard for the misattribution bug found in review: a
    connection with `Target.setDiscoverTargets` on (every Puppeteer/omp
    client) receives targetCreated for EVERY tab in the shared browser, not
    just the ones it created. `apply_target_event` must never be told "this
    connection's agent" and use it as a fallback owner — attribution comes
    only from an explicit createTarget response, a context owner, or the
    opener tab's owner. Simulates the `proxy_ws.from_upstream` path: the
    event is fed through `apply_target_event` exactly as the real proxy does
    (no per-connection agent is passed in anywhere)."""
    state = GatewayState(now_fn=lambda: 1.0)
    # alpha's own connection creates and owns T1.
    state.observe_response("Target.createTarget", {}, {"targetId": "T1"}, agent="alpha")
    state.apply_target_event("targetCreated", {"targetInfo": {"targetId": "T1", "type": "page", "title": "", "url": ""}})
    # beta opens a brand-new, unrelated tab (no opener, no shared context) —
    # alpha's discover-enabled connection sees this targetCreated too, but
    # must never end up owning it.
    state.apply_target_event("targetCreated", {
        "targetInfo": {"targetId": "FOREIGN", "type": "page", "title": "", "url": "https://foreign.example"},
    })
    assert state.owner_of("FOREIGN") is None
    assert state.targets["FOREIGN"].agent is None


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
    assert state.targets_for("alpha") == []


# ── claim on use (live finding 04.10.2026: omp never creates a tab) ────────

def _page_event(tid, url="https://example.org"):
    return {"targetInfo": {"targetId": tid, "type": "page", "title": "", "url": url}}


def test_omp_claim_target_on_a_session_claims_the_tab():
    state = GatewayState(now_fn=lambda: 1.0)
    state.apply_target_event("targetCreated", _page_event("T1"))
    state.observe_response("Target.attachToTarget", {"targetId": "T1", "flatten": True}, {"sessionId": "S1"}, agent="alpha")
    state.observe_command("OMP.claimTarget", "S1", "alpha")
    assert state.owner_of("T1") == "alpha"
    assert state.targets["T1"].agent == "alpha"


def test_navigation_claims_and_the_last_navigating_agent_wins():
    state = GatewayState(now_fn=lambda: 1.0)
    state.apply_target_event("targetCreated", _page_event("T1"))
    state.observe_event("Target.attachedToTarget", {"sessionId": "SA", "targetInfo": {"targetId": "T1"}})
    state.observe_event("Target.attachedToTarget", {"sessionId": "SB", "targetInfo": {"targetId": "T1"}})
    state.observe_command("Page.navigate", "SA", "alpha")
    assert state.owner_of("T1") == "alpha"
    state.observe_command("Page.navigate", "SB", "beta")
    assert state.owner_of("T1") == "beta"


def test_bookkeeping_commands_never_claim():
    """Puppeteer auto-attaches to EVERY tab on connect and sends
    Runtime.enable / Page.enable / runIfWaitingForDebugger on each — that is
    plumbing, not "working in this tab"."""
    state = GatewayState(now_fn=lambda: 1.0)
    state.apply_target_event("targetCreated", _page_event("T1"))
    state.observe_event("Target.attachedToTarget", {"sessionId": "S1", "targetInfo": {"targetId": "T1"}})
    for method in ("Runtime.enable", "Page.enable", "Runtime.runIfWaitingForDebugger", "Page.getFrameTree"):
        state.observe_command(method, "S1", "alpha")
    assert state.owner_of("T1") is None


def test_unidentified_connection_never_claims():
    state = GatewayState(now_fn=lambda: 1.0)
    state.apply_target_event("targetCreated", _page_event("T1"))
    state.observe_event("Target.attachedToTarget", {"sessionId": "S1", "targetInfo": {"targetId": "T1"}})
    state.observe_command("OMP.claimTarget", "S1", _SHARED)
    state.observe_command("Page.navigate", "S1", None)
    assert state.owner_of("T1") is None


def test_page_level_socket_claims_the_page_in_its_url():
    state = GatewayState(now_fn=lambda: 1.0)
    state.apply_target_event("targetCreated", _page_event("T7"))
    state.observe_command("Page.navigate", None, "alpha", page_target="T7")
    assert state.owner_of("T7") == "alpha"


def test_session_map_is_recorded_for_unidentified_connections_too():
    """The session -> tab map is a fact about the browser; an agent's later
    claim on a session another connection opened must still resolve."""
    state = GatewayState(now_fn=lambda: 1.0)
    state.observe_response("Target.attachToTarget", {"targetId": "T1"}, {"sessionId": "S1"}, agent=_SHARED)
    assert state.session_target["S1"] == "T1"


def test_watcher_reset_keeps_claims_for_tabs_that_still_exist():
    state = GatewayState(now_fn=lambda: 1.0)
    state.apply_target_event("targetCreated", _page_event("T1"))
    state.observe_command("Page.navigate", None, "alpha", page_target="T1")
    state.reset_targets()  # our watcher reconnected; the tab is still open
    state.apply_target_event("targetCreated", _page_event("T1"))
    assert state.targets["T1"].agent == "alpha"


def test_mc_targets_reports_unclaimed_tabs_as_unassigned():
    state = GatewayState(now_fn=lambda: 1.0)
    state.apply_target_event("targetCreated", _page_event("MINE"))
    state.apply_target_event("targetCreated", _page_event("NOBODY"))
    state.observe_command("Page.navigate", None, "alpha", page_target="MINE")
    rows = {r["targetId"]: r["agent"] for r in state.as_mc_targets_json()}
    assert rows == {"MINE": "alpha", "NOBODY": None}


# ── _FrameSniffer: observes a copy of the byte stream ─────────────────────

import json as _json  # noqa: E402
import os as _os  # noqa: E402
import struct as _struct  # noqa: E402

from cdp_gateway import _FrameSniffer, page_id_from_path  # noqa: E402


def _ws_frame(payload: bytes, *, opcode=0x1, fin=True, mask=None) -> bytes:
    b0 = (0x80 if fin else 0) | opcode
    n = len(payload)
    mbit = 0x80 if mask else 0
    if n < 126:
        head = _struct.pack("!BB", b0, mbit | n)
    elif n < 65536:
        head = _struct.pack("!BBH", b0, mbit | 126, n)
    else:
        head = _struct.pack("!BBQ", b0, mbit | 127, n)
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return head + mask + payload
    return head + payload


def test_sniffer_reads_masked_client_frames_split_across_feeds():
    seen = []
    sniffer = _FrameSniffer(seen.append)
    data = _ws_frame(_json.dumps({"id": 1, "method": "OMP.claimTarget"}).encode(), mask=b"\x01\x02\x03\x04")
    for i in range(len(data)):  # worst case: one byte at a time
        sniffer.feed(data[i:i + 1])
    assert seen == [{"id": 1, "method": "OMP.claimTarget"}]


def test_sniffer_reassembles_fragments_around_a_control_frame():
    seen = []
    sniffer = _FrameSniffer(seen.append)
    body = _json.dumps({"method": "Target.targetCreated", "params": {}}).encode()
    data = (
        _ws_frame(body[:10], fin=False)
        + _ws_frame(b"ping", opcode=0x9)
        + _ws_frame(body[10:], opcode=0x0)
    )
    sniffer.feed(data)
    assert seen == [{"method": "Target.targetCreated", "params": {}}]


def test_sniffer_skips_oversized_messages_and_keeps_going():
    seen = []
    sniffer = _FrameSniffer(seen.append, max_message=1024)
    big = _ws_frame(_json.dumps({"id": 2, "result": {"data": "x" * 5000}}).encode())
    small = _ws_frame(_json.dumps({"id": 3, "result": {}}).encode())
    stream = big + small
    for i in range(0, len(stream), 700):
        sniffer.feed(stream[i:i + 700])
    assert seen == [{"id": 3, "result": {}}]
    assert not sniffer.broken


def test_sniffer_switches_off_on_a_compressed_frame_instead_of_raising():
    seen = []
    sniffer = _FrameSniffer(seen.append)
    sniffer.feed(bytes([0xC1, 0x02]) + b"xx")  # RSV1 set = permessage-deflate
    sniffer.feed(_ws_frame(b'{"id": 1}'))
    assert sniffer.broken
    assert seen == []


def test_sniffer_handles_64bit_lengths():
    seen = []
    sniffer = _FrameSniffer(seen.append, max_message=200_000)
    payload = _json.dumps({"id": 9, "result": {"blob": "y" * 70_000}}).encode()
    sniffer.feed(_ws_frame(payload, mask=_os.urandom(4)))
    assert seen and seen[0]["id"] == 9


def test_page_id_from_path():
    assert page_id_from_path("/devtools/page/ABC") == "ABC"
    assert page_id_from_path("/devtools/browser/ABC") is None
