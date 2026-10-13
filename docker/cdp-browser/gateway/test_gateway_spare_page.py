"""The shared browser never runs out of pages (incident 13.10.2026).

omp attaches to the shared browser in "connected" mode and by design NEVER
creates a tab there: it adopts an existing page (omp 18.10.1
`tools/browser/attach.ts::pickElectronTarget` — "never create a tab in a
borrowed browser"; 18.1.10 did the same). Ending an agent's working phase
(`DELETE /mc/sessions/<token>?agent_tabs=1`, ADR-088 lifecycle) closes the
tab the agent worked in — which is Chromium's startup tab omp had claimed.
On 07.10.2026 21:22 that closed the LAST page; from then on every omp
`browser.open` failed ("Timed out after waiting 10000ms" in 18.10.1), and
the agent improvised its own Chromium in /tmp.

The gateway therefore keeps one page in the shared browser: whenever its
watcher sees the browser without a page outside the agents' and sessions'
own contexts, it opens a blank one — owned by nobody, exactly like
Chromium's startup tab.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from cdp_gateway import CdpGateway, GatewayState, session_owner_key  # noqa: E402

TOKEN = "tok_" + "S" * 40
SID = "22222222-3333-4444-5555-666666666666"


class FakeHeaders(dict):
    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


def _created(state, tid, *, ctx=None, url="about:blank"):
    info = {"targetId": tid, "type": "page", "title": "", "url": url}
    if ctx:
        info["browserContextId"] = ctx
    state.apply_target_event("targetCreated", {"targetInfo": info})


def _synced_gateway(monkeypatch, calls, *, new_id="SPARE"):
    gateway = CdpGateway(upstream_host="127.0.0.1", upstream_port=1)

    async def fake_call(commands, **kw):
        calls.extend(commands)
        return [{"result": {"targetId": new_id}} if m == "Target.createTarget" else {"result": {}}
                for m, _p in commands]

    monkeypatch.setattr(gateway, "_cdp_call", fake_call)
    return gateway


# ── the pure rule ──────────────────────────────────────────────────────────

def test_a_page_in_the_default_context_counts_one_in_an_owned_context_does_not():
    state = GatewayState()
    assert not state.has_shared_page()
    _created(state, "IN-SESSION-CTX", ctx="CTX-S")
    state.record_context_owner("CTX-S", session_owner_key(SID))
    assert not state.has_shared_page()          # omp must not need a session's private tab
    _created(state, "DEFAULT", ctx="CTX-DEFAULT")
    assert state.has_shared_page()


# ── the incident path ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ending_an_agent_phase_that_closes_the_last_page_leaves_a_blank_one(monkeypatch):
    calls = []
    gateway = _synced_gateway(monkeypatch, calls)
    _created(gateway.state, "STARTUP")                                 # Chromium's startup tab
    await gateway.handle_watcher_message({"id": 1, "result": {}})      # discover burst done
    assert calls == []                                                 # a page exists: nothing to do
    gateway.state.register_session(TOKEN, SID, "alpha")
    gateway.state.record_owner("STARTUP", "alpha")                    # omp claimed it

    status, _ct, body = await gateway.handle_http(
        f"/mc/sessions/{TOKEN}?agent_tabs=1", FakeHeaders({"Host": "cdp-browser:9300"}), "10.0.0.5", "DELETE")
    assert status == 200 and json.loads(body)["closedTargets"] == 1
    assert calls == [("Target.closeTarget", {"targetId": "STARTUP"})]

    # Chromium reports the close on the watcher connection.
    await gateway.handle_watcher_message({"method": "Target.targetDestroyed", "params": {"targetId": "STARTUP"}})
    assert ("Target.createTarget", {"url": "about:blank"}) in calls
    assert gateway.state.has_shared_page()
    spare = gateway.state.targets["SPARE"]
    assert spare.agent is None and spare.creator is None               # nobody's, like the startup tab


@pytest.mark.asyncio
async def test_an_empty_browser_gets_its_page_once_the_watcher_is_synced(monkeypatch):
    calls = []
    gateway = _synced_gateway(monkeypatch, calls)
    await gateway.handle_watcher_message({"id": 1, "result": {}})
    assert calls == [("Target.createTarget", {"url": "about:blank"})]
    # The real targetCreated for it arrives later and changes nothing.
    await gateway.handle_watcher_message({"method": "Target.targetCreated", "params": {"targetInfo": {
        "targetId": "SPARE", "type": "page", "title": "", "url": "about:blank"}}})
    await gateway.handle_watcher_message({"method": "Target.targetDestroyed", "params": {"targetId": "OTHER"}})
    assert calls == [("Target.createTarget", {"url": "about:blank"})]


@pytest.mark.asyncio
async def test_no_extra_page_while_one_is_left_or_before_the_watcher_knows_the_browser(monkeypatch):
    calls = []
    gateway = _synced_gateway(monkeypatch, calls)
    # Before the discover burst is complete the gateway knows nothing: no page yet.
    await gateway.handle_watcher_message({"method": "Target.targetDestroyed", "params": {"targetId": "X"}})
    assert calls == []
    await gateway.handle_watcher_message({"method": "Target.targetCreated", "params": {"targetInfo": {
        "targetId": "A", "type": "page", "title": "", "url": "https://a.example"}}})
    await gateway.handle_watcher_message({"method": "Target.targetCreated", "params": {"targetInfo": {
        "targetId": "B", "type": "page", "title": "", "url": "https://b.example"}}})
    await gateway.handle_watcher_message({"id": 1, "result": {}})
    await gateway.handle_watcher_message({"method": "Target.targetDestroyed", "params": {"targetId": "A"}})
    assert calls == []                                                 # B is still there
    assert set(gateway.state.targets) == {"B"}


@pytest.mark.asyncio
async def test_a_failed_create_is_not_fatal_and_is_retried_on_the_next_event(monkeypatch):
    gateway = CdpGateway(upstream_host="127.0.0.1", upstream_port=1)
    attempts = []

    async def failing(commands, **kw):
        attempts.append(commands)
        return [{"error": {"message": "unanswered within 5s"}}]

    monkeypatch.setattr(gateway, "_cdp_call", failing)
    await gateway.handle_watcher_message({"id": 1, "result": {}})
    await gateway.handle_watcher_message({"method": "Target.targetDestroyed", "params": {"targetId": "X"}})
    assert len(attempts) == 2
    assert not gateway.state.has_shared_page()
