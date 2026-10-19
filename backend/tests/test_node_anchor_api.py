"""Backend side of node-agent anchors (ADR-096 §3.2, PR 4).

- ``desired_state.watch`` is the allowlist MC sends to the agent: strict
  (kind from a fixed set, names without shell or path characters, capped).
- The heartbeat stores what the agent reports (``anchors`` + ``docker``).
- ``device_state.anchor_status`` answers "is this anchor running?" with
  ``running`` / ``stopped`` / ``unknown`` — and never ``running`` from a
  stale, missing or unreadable report.

RFC-5737 placeholders and made-up names only — the repo is public.
"""
from datetime import timedelta

import pytest

from app.services import device_state as ds
from app.utils import utcnow
from tests.test_node_device_control import _age_last_seen, _device_state, _heartbeat, _pair

_WATCH = [{"kind": "container", "name": "glm-head"}, {"kind": "process", "name": "llama-server"}]


# ── PUT desired_state.watch ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_watch_is_accepted_and_sent_to_the_agent(client, auth_client):
    token, host_id = await _pair(client, auth_client)
    resp = await auth_client.put(f"/api/v1/nodes/{host_id}/device-state", json={"watch": _WATCH})
    assert resp.status_code == 200, resp.text
    assert resp.json()["desired_state"] == {"watch": _WATCH}
    # watch alone is not a control wish: the device light stays out of "pending".
    assert resp.json()["diff"] == []

    hb = await _heartbeat(client, token)
    assert hb.json()["desired_state"] == {"watch": _WATCH}


@pytest.mark.asyncio
async def test_empty_watch_means_no_opinion(client, auth_client):
    _, host_id = await _pair(client, auth_client)
    resp = await auth_client.put(f"/api/v1/nodes/{host_id}/device-state", json={"watch": []})
    assert resp.status_code == 200, resp.text
    assert resp.json()["desired_state"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry",
    [
        {"kind": "container", "name": "x; rm -rf /"},
        {"kind": "container", "name": "../etc"},
        {"kind": "container", "name": "-flag"},
        {"kind": "container", "name": "a" * 65},
        {"kind": "shell", "name": "ok"},
        {"kind": "container", "name": "ok", "extra": 1},
    ],
)
async def test_bad_watch_entry_is_422(client, auth_client, entry):
    _, host_id = await _pair(client, auth_client)
    resp = await auth_client.put(f"/api/v1/nodes/{host_id}/device-state", json={"watch": [entry]})
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_too_many_watch_entries_is_422(client, auth_client):
    _, host_id = await _pair(client, auth_client)
    many = [{"kind": "process", "name": f"p{i}"} for i in range(ds.WATCH_MAX + 1)]
    resp = await auth_client.put(f"/api/v1/nodes/{host_id}/device-state", json={"watch": many})
    assert resp.status_code == 422, resp.text


# ── Heartbeat stores the report ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_heartbeat_anchors_are_stored(client, auth_client):
    token, host_id = await _pair(client, auth_client)
    anchors = [
        {"kind": "container", "name": "glm-head", "state": "unknown", "reason": "docker_no_access"},
        {"kind": "process", "name": "llama-server", "state": "running"},
    ]
    hb = await _heartbeat(client, token, device_state=_device_state(docker="no_access", anchors=anchors))
    assert hb.status_code == 200, hb.text

    stored = (await auth_client.get(f"/api/v1/nodes/{host_id}/device-state")).json()["device_state"]
    assert stored["docker"] == "no_access"
    assert stored["anchors"] == anchors


@pytest.mark.asyncio
async def test_old_agent_without_anchors_still_valid(client, auth_client):
    token, host_id = await _pair(client, auth_client)
    hb = await _heartbeat(client, token, device_state=_device_state())
    assert hb.status_code == 200, hb.text
    stored = (await auth_client.get(f"/api/v1/nodes/{host_id}/device-state")).json()["device_state"]
    assert "anchors" not in stored


# ── anchor_status: never a false "running" ───────────────────────────────────


def _current(*anchors):
    return {"gpu_mode": "eco", "anchors": list(anchors)}


def test_anchor_running_when_fresh():
    cur = _current({"kind": "container", "name": "glm-head", "state": "running"})
    assert ds.anchor_status(cur, utcnow(), "container", "glm-head") == "running"


def test_anchor_stopped_when_fresh():
    cur = _current({"kind": "container", "name": "glm-head", "state": "stopped"})
    assert ds.anchor_status(cur, utcnow(), "container", "glm-head") == "stopped"


def test_anchor_unknown_when_stale():
    cur = _current({"kind": "container", "name": "glm-head", "state": "running"})
    old = utcnow() - timedelta(seconds=ds.STALE_AFTER_S + 1)
    assert ds.anchor_status(cur, old, "container", "glm-head") == "unknown"


def test_anchor_unknown_without_timestamp():
    cur = _current({"kind": "container", "name": "glm-head", "state": "running"})
    assert ds.anchor_status(cur, None, "container", "glm-head") == "unknown"


def test_anchor_unknown_when_not_reported():
    cur = _current({"kind": "container", "name": "other", "state": "running"})
    assert ds.anchor_status(cur, utcnow(), "container", "glm-head") == "unknown"
    assert ds.anchor_status(None, utcnow(), "container", "glm-head") == "unknown"
    assert ds.anchor_status({}, utcnow(), "container", "glm-head") == "unknown"


def test_anchor_kind_must_match():
    cur = _current({"kind": "process", "name": "glm-head", "state": "running"})
    assert ds.anchor_status(cur, utcnow(), "container", "glm-head") == "unknown"


def test_anchor_unknown_state_passes_through():
    cur = _current({"kind": "container", "name": "glm-head", "state": "unknown", "reason": "docker_no_access"})
    assert ds.anchor_status(cur, utcnow(), "container", "glm-head") == "unknown"


def test_anchor_garbage_state_is_unknown():
    cur = _current({"kind": "container", "name": "glm-head", "state": "yes"})
    assert ds.anchor_status(cur, utcnow(), "container", "glm-head") == "unknown"


def test_watch_does_not_count_as_diff():
    assert ds.desired_state_diff({"watch": _WATCH}, {"gpu_mode": "eco"}) == []


def test_limits_match_the_agent():
    """Agent and backend must agree on the allowlist rules (one file each)."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "node-agent" / "mc-node-agent.py"
    spec = importlib.util.spec_from_file_location("mc_node_agent_limits", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.WATCH_MAX == ds.WATCH_MAX
    assert mod.ANCHOR_KINDS == ds.ANCHOR_KINDS
    import typing

    from app.routers.nodes import WatchAnchor

    assert typing.get_args(WatchAnchor.model_fields["kind"].annotation) == ds.ANCHOR_KINDS
    assert mod.ANCHOR_NAME_MAX == ds.ANCHOR_NAME_MAX
    assert mod.ANCHOR_NAME_CHARS == ds.ANCHOR_NAME_CHARS
    for name in ("glm-head", "a.b_c-1", "-x", "x y", "a" * 65, "", "../e", "x;y", "ä"):
        assert mod.valid_anchor_name(name) == ds.valid_anchor_name(name), name


@pytest.mark.asyncio
async def test_endpoint_flow_running_then_stale(client, auth_client):
    """End to end: agent reports running → anchor_status running; report
    ages past the stale limit → unknown, not running."""
    token, host_id = await _pair(client, auth_client)
    await _heartbeat(
        client, token,
        device_state=_device_state(anchors=[{"kind": "container", "name": "glm-head", "state": "running"}]),
    )
    dev = (await auth_client.get(f"/api/v1/nodes/{host_id}/device-state")).json()
    from datetime import datetime

    reported = datetime.fromisoformat(dev["device_state_updated_at"])
    assert ds.anchor_status(dev["device_state"], reported, "container", "glm-head") == "running"
    await _age_last_seen(host_id)
    later = utcnow() + timedelta(seconds=ds.STALE_AFTER_S + 5)
    assert ds.anchor_status(dev["device_state"], reported, "container", "glm-head", now=later) == "unknown"
