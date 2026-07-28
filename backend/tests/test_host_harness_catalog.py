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

from app.services.harness_compat import HARNESS_LABELS, HARNESS_PROTOCOLS, HARNESSES
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

    # The catalog falls back to the key when an adapter forgets `label`, so
    # assert the adapter DECLARES one rather than that the string differs from
    # the key — "omp" is a legitimate display name that happens to equal it.
    assert hasattr(adapter, "label"), (
        f"host harness '{harness}' must declare a `label` on its adapter — the "
        f"wizard renders that string in the harness picker"
    )
    assert entry["label"] == adapter.label
    assert entry["label"].strip()

    # A harness that exists in BOTH worlds must not be called two different
    # things depending on which picker you are looking at.
    if harness in HARNESS_LABELS:
        assert entry["label"] == HARNESS_LABELS[harness], (
            f"'{harness}' is labelled '{entry['label']}' as a host harness but "
            f"'{HARNESS_LABELS[harness]}' as a cli-bridge harness"
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


@pytest.mark.parametrize("harness", ["claude", "openclaude", "omp"])
def test_generic_staged_harnesses_are_not_singletons(harness: str):
    """Arbitrarily many claude/openclaude/omp host agents are legitimate.

    They are staged by host_provisioning.stage_host_agent_files into
    ~/.mc/agents/<slug>/, so nothing is pinned to one slug. A UI that assumes
    "host ⇒ singleton" blocks every new one as soon as the first exists.
    """
    entry = _catalog_by_key()[harness]
    assert entry["singleton"] is False
    assert entry["singleton_slug"] is None
    # ...and they must NOT claim a bespoke bootstrap, or routers/agents.py
    # would call adapter.bootstrap() instead of the generic staging path.
    assert entry["supports_bootstrap"] is False


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


# ── 2b. ⭐ Every CLI type exists in BOTH worlds ───────────────────────────


@pytest.mark.parametrize("harness", list(HARNESSES))
def test_every_cli_bridge_harness_is_also_a_host_harness(harness: str):
    """INVARIANT: HARNESSES ⊆ HOST_ADAPTERS.

    Mark's requirement, made structural: every CLI type must be creatable BOTH
    as a container (cli-bridge) and as a host (launchd) agent. Registering a
    new harness in only one of the two registries is precisely the half-wiring
    that left `claude` invisible in the host wizard and `openclaude`/`omp`
    permanently runtime-locked, even though host_provisioning._HARNESS_BINARY
    had known them all along.

    This is a ONE-WAY containment on purpose. The reverse (HOST_ADAPTERS ⊆
    HARNESSES) must NOT hold: `hermes` (ADR-064) and `grok` (ADR-066) are host
    bridges with no cli-bridge form at all — they own their tmux session and
    their own plist, and listing them in the cli-bridge harness selector would
    offer containers that cannot exist. See the counter-test below, which pins
    that asymmetry so nobody "fixes" it into a symmetric assertion.
    """
    assert harness in HOST_ADAPTERS, (
        f"cli-bridge harness '{harness}' has no HostHarnessAdapter — it cannot "
        f"be created as a host agent. Add an adapter (see "
        f"_GenericStagedHostAdapter) so every CLI type exists in both worlds."
    )


def test_host_only_bridges_are_deliberately_absent_from_cli_bridge_harnesses():
    """The other direction of the invariant is NOT required — and must not be.

    Pins the asymmetry: hermes/grok are host-only. If one of them ever gains a
    cli-bridge form this test is the place that says so out loud.
    """
    for host_only in ("hermes", "grok"):
        assert host_only in HOST_ADAPTERS
        assert host_only not in HARNESSES


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
    # NOT partitions of one: every cli-bridge harness also has a host form
    # (invariant above), while hermes/grok are host-only and must stay out of
    # the cli-bridge list that feeds the runtime-switch harness selector.
    assert [entry["key"] for entry in body["harnesses"]] == list(HARNESSES)
    assert "runtimes" in body
    for host_only in ("hermes", "grok"):
        assert host_only not in {entry["key"] for entry in body["harnesses"]}
