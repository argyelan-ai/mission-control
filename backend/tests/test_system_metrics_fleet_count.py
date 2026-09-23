"""/api/v1/system/metrics — the sidebar's agent badge must match /agents.

The sidebar read "Agents 14/14" while 12 of 14 agents were paused: a paused
agent keeps heartbeating (status "idle"), and the endpoint only counted alive
statuses. /agents splits by operational_mode ("2 active · 12 paused") and
hides archived agents; the metrics endpoint now returns the same split.
"""

from datetime import datetime, timezone


async def test_agent_counts_split_by_operational_mode(auth_client, make_agent):
    await make_agent(name="alpha", status="idle", operational_mode="active")
    await make_agent(name="beta", status="busy", operational_mode="active")
    for i in range(3):
        await make_agent(name=f"paused-{i}", status="idle", operational_mode="paused")
    # Archived agents are not part of the roster /agents shows.
    await make_agent(
        name="gone",
        status="offline",
        operational_mode="active",
        archived_at=datetime.now(timezone.utc),
    )

    resp = await auth_client.get("/api/v1/system/metrics")
    assert resp.status_code == 200
    agents = resp.json()["agents"]

    assert agents["total"] == 5
    assert agents["active"] == 2
    assert agents["paused"] == 3


async def test_agent_counts_empty_fleet(auth_client):
    resp = await auth_client.get("/api/v1/system/metrics")
    assert resp.status_code == 200
    agents = resp.json()["agents"]
    assert (agents["total"], agents["active"], agents["paused"]) == (0, 0, 0)


async def test_online_counts_the_same_roster_as_total(auth_client, make_agent):
    """An archived agent that still reports "idle" must not push online above total."""
    await make_agent(name="alpha", status="idle", operational_mode="active")
    await make_agent(
        name="gone",
        status="idle",
        operational_mode="active",
        archived_at=datetime.now(timezone.utc),
    )

    resp = await auth_client.get("/api/v1/system/metrics")
    assert resp.status_code == 200
    agents = resp.json()["agents"]

    assert agents["total"] == 1
    assert agents["online"] == 1
    assert agents["online"] <= agents["total"]
