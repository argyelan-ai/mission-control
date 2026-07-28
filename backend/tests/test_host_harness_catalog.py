"""The host-harness registry is what the agent wizard renders — not a copy of it.

Same failure mode as the runtime-switch lock (see
test_runtime_switchable_field.py), one rule further along: the wizard kept its
OWN list of host harnesses plus its own protocol map. Consequences:

  * `claude` never appeared — a host Claude agent (what Boss is) could not be
    created through the wizard at all, although the backend stages exactly that
    via host_provisioning.stage_host_agent_files.
  * every host harness was assumed to be a SINGLETON bridge, so even if claude
    had been listed it would have been greyed out as soon as Boss existed —
    although ClaudeHostAdapter.singleton_slug is None ON PURPOSE.

`host_harness_catalog()` is now the one description, served on
GET /api/v1/runtimes/compat-matrix (the endpoint the wizard already calls, so
no extra roundtrip). The tests below iterate HOST_ADAPTERS itself: a newly
registered host harness is covered — and must declare label/protocol/singleton
— the moment it is added.
"""
from __future__ import annotations

import pytest

from app.services.harness_compat import HARNESS_PROTOCOLS
from app.services.host_harness_adapter import HOST_ADAPTERS, host_harness_catalog


def _catalog_by_key() -> dict[str, dict]:
    return {entry["key"]: entry for entry in host_harness_catalog()}


# ── 1. The catalog IS the registry ────────────────────────────────────────


def test_catalog_covers_the_registry_exactly_once():
    catalog = host_harness_catalog()
    keys = [entry["key"] for entry in catalog]
    assert keys == list(HOST_ADAPTERS), "catalog must mirror HOST_ADAPTERS, in order"
    assert len(keys) == len(set(keys))


@pytest.mark.parametrize("harness", list(HOST_ADAPTERS))
def test_each_entry_reports_the_adapter_it_describes(harness: str):
    adapter = HOST_ADAPTERS[harness]
    entry = _catalog_by_key()[harness]

    assert entry["label"] and entry["label"] != harness, (
        f"host harness '{harness}' needs a human-readable label on its adapter "
        f"— the wizard renders this string"
    )
    assert entry["protocol"] == adapter.protocol
    # The protocol must be one the compat layer actually knows, otherwise the
    # wizard would filter every provider away for this harness.
    assert harness in HARNESS_PROTOCOLS
    assert entry["protocol"] in HARNESS_PROTOCOLS[harness]

    assert entry["singleton_slug"] == getattr(adapter, "singleton_slug", None)
    assert entry["singleton"] is (entry["singleton_slug"] is not None)
    assert entry["supports_bootstrap"] is getattr(adapter, "supports_bootstrap", True)


# ── 2. The concrete singleton verdicts ────────────────────────────────────


@pytest.mark.parametrize("harness", ["hermes", "grok", "kimi"])
def test_singleton_bridges_are_flagged_singleton(harness: str):
    """These hardcode config dir + plist to one slug (2026-07-12 incident)."""
    entry = _catalog_by_key()[harness]
    assert entry["singleton"] is True
    assert entry["singleton_slug"] == harness


def test_claude_is_not_a_singleton():
    """The whole point: arbitrarily many claude host agents are legitimate.

    ClaudeHostAdapter.singleton_slug is None because
    host_provisioning.stage_host_agent_files stages any claude host agent into
    ~/.mc/agents/<slug>/. A UI that assumes "host ⇒ singleton" blocks every new
    one as soon as Boss exists.
    """
    entry = _catalog_by_key()["claude"]
    assert entry["singleton"] is False
    assert entry["singleton_slug"] is None


def test_at_least_one_singleton_and_one_non_singleton_exist():
    """Guards the two tests above from becoming vacuous in either direction."""
    flags = {entry["singleton"] for entry in host_harness_catalog()}
    assert flags == {True, False}


# ── 3. It actually reaches the endpoint the wizard calls ──────────────────


@pytest.mark.asyncio
async def test_compat_matrix_endpoint_ships_the_host_harness_catalog(auth_client):
    """The wizard fetches this endpoint already — no extra roundtrip needed."""
    resp = await auth_client.get("/api/v1/runtimes/compat-matrix")
    assert resp.status_code == 200
    body = resp.json()

    assert "host_harnesses" in body, (
        "the wizard reads host harnesses from the compat matrix; dropping this "
        "key sends it back to a hardcoded list"
    )
    served = {entry["key"]: entry for entry in body["host_harnesses"]}
    assert set(served) == set(HOST_ADAPTERS)

    for harness, adapter in HOST_ADAPTERS.items():
        assert served[harness]["protocol"] == adapter.protocol
        assert served[harness]["singleton"] is (
            getattr(adapter, "singleton_slug", None) is not None
        )

    # The cli-bridge half must be untouched by the addition. The two lists are
    # independent registries, not partitions of one — `claude` and `kimi`
    # legitimately exist in both worlds (see KimiHostAdapter's docstring), while
    # `hermes`/`grok` are host-only and must stay out of the cli-bridge list
    # that feeds the runtime-switch harness selector.
    from app.services.harness_compat import HARNESSES

    assert [entry["key"] for entry in body["harnesses"]] == list(HARNESSES)
    assert "runtimes" in body
    for host_only in ("hermes", "grok"):
        assert host_only not in {entry["key"] for entry in body["harnesses"]}
