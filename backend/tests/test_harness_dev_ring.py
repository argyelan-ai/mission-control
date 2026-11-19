"""Harness development ring — capability declarations + two-way guard.

Marks directive (2026-09-16): every agent, regardless of kind, harness, host
or container, must work exactly as before after a runtime switch — and the
dev ring must show what to do when integrating a NEW harness.

This guard has TWO directions:

1. FORWARD: a harness appearing in HARNESS_CAPABILITIES (backend) or
   HOST_ADAPTERS (backend) without a capability declaration → red.
2. BACKWARD: a "cannot, deliberately" exception that has gone stale — the
   claim no longer matches a verifiable code fact → red. A one-way
   exception list rots; this one is forced to re-verify.

The declarations live in docs/harness-matrix.json ("ring" section) — the one
source the human matrix docs/harness-matrix.md is rendered from
(tools/render-harness-matrix.py; its own guard: test_harness_matrix.py).

Declaration values per (harness, place, capability):
  "yes"   — capability present, `proof` cites file:line or a live
            measurement (see mc-cli-onboarding §7a). Formerly "da".
  "no"    — absent (deliberately or as a known gap). MUST carry `since`
            (ISO date) and `recheck` (how to notice it went stale). The
            guard re-derives the fact each run: if the cited recheck
            contradicts the claim, red. Formerly "fehlt-bewusst".
  "open"  — unproven. Counts as a gap; keep few. Formerly "offen".
"""
from __future__ import annotations

import importlib
import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.services.harness_compat import HARNESSES, HARNESS_CAPABILITIES  # noqa: E402
from app.services import host_harness_adapter as hha  # noqa: E402
from app.services.compose_renderer import HARNESS_IMAGES  # noqa: E402

# ── One source: docs/harness-matrix.json ─────────────────────────────────────
# The capability universe and every (harness, place, capability) declaration
# live in the "ring" section of docs/harness-matrix.json — the same file the
# harness matrix (docs/harness-matrix.md, tools/render-harness-matrix.py) is
# rendered from, so the dev ring and the matrix cannot drift apart.
#
# Capability universe — derived from mechanism, not wish lists:
#  - Layer A (mechanical dispatch): what the backend does TO an agent.
#  - Layer B (task contract): what an agent must call to run a task.
#  - Layer C (place): ssh-push + container/host placement.
# Each entry: id -> (layer, what proves it). Proof style per
# mc-cli-onboarding §7a: file:line or live measurement, never config trust.
#
# place: "container" or "host". Every (harness, place) pair the system can
# run MUST have a statement for EVERY capability. status ∈ {yes, no, open}
# (formerly da / fehlt-bewusst / offen). `proof` = file:line or measurement;
# `since`/`recheck` mandatory for "no" (stale-exception guard, direction 2).
# Rows surfaced in the human matrix also carry checkable `evidence`
# (test_harness_matrix.py checks the paths and line ranges).

_SOURCE = REPO_ROOT.parent / "docs" / "harness-matrix.json"
_RING = json.loads(_SOURCE.read_text(encoding="utf-8"))["ring"]
CAPABILITIES: dict[str, tuple[str, str]] = {
    cap: (layer, why) for cap, (layer, why) in _RING["capabilities"].items()
}
D: list[dict] = _RING["rows"]
STATUSES = ("yes", "no", "open")


def _decl(harness: str, place: str, cap: str) -> dict | None:
    for row in D:
        if row["harness"] == harness and row["place"] == place and row["cap"] == cap:
            return row
    return None


@pytest.mark.parametrize("harness", sorted(HARNESS_CAPABILITIES))
@pytest.mark.parametrize("place", ["container", "host"])
def test_every_harness_place_has_a_statement_for_every_capability(harness: str, place: str):
    """Direction 1: no harness appears without a full statement column."""
    missing = [cap for cap in CAPABILITIES if _decl(harness, place, cap) is None]
    assert not missing, (
        f"Harness '{harness}' ({place}) lacks capability statements for: {missing}. "
        "Add a row to the 'ring' section of docs/harness-matrix.json with status "
        "yes/no/open + proof, then run: python3 tools/render-harness-matrix.py. "
        "See the module docstring for the contract."
    )


@pytest.mark.parametrize("row", D, ids=lambda r: f"{r['harness']}/{r['place']}/{r['cap']}")
def test_declaration_shape(row: dict):
    assert row["status"] in STATUSES
    assert row.get("proof"), "every statement carries a proof (file:line or measurement)"
    if row["status"] == "no":
        assert row.get("since"), "'no' needs since=ISO date"
        assert row.get("recheck"), "'no' needs recheck= (how staleness would show)"


# Every registry a harness can enter must be patrolled. HARNESS_CAPABILITIES
# alone is not enough: runtimes.py iterates the cli-bridge matrix over
# HARNESSES, and host_harness_adapter.HOST_ADAPTERS gates host offers — a
# harness added to either without HARNESS_CAPABILITIES previously slipped
# past the guard (Rex review 2026-09-16, bypass "phantom2").
REGISTRY_SOURCES = {
    "HARNESS_CAPABILITIES": set(HARNESS_CAPABILITIES),
    "HARNESSES": set(HARNESSES),
    "HOST_ADAPTERS": set(hha.HOST_ADAPTERS),
}
# jarvis is the voice worker (single-agent voice binding, no task loop) —
# deliberate exemption, reason recorded here per ADR-084 discipline.
REGISTRY_EXEMPT = {"jarvis": "voice-worker: no task loop, no memory surface"}


def test_harness_registries_stay_in_sync():
    """Direction 1b: no harness enters ANY registry without capabilities."""
    caps = set(HARNESS_CAPABILITIES)
    for source, members in REGISTRY_SOURCES.items():
        unpatrolled = members - caps - set(REGISTRY_EXEMPT)
        assert not unpatrolled, (
            f"{source} contains harnesses without a HARNESS_CAPABILITIES entry: "
            f"{sorted(unpatrolled)}. Add them to HARNESS_CAPABILITIES (or, only "
            "with a recorded reason, to REGISTRY_EXEMPT)."
        )
    dead_exempt = set(REGISTRY_EXEMPT) - set().union(*REGISTRY_SOURCES.values()) if REGISTRY_SOURCES else set()
    assert not dead_exempt, f"exemptions for harnesses in no registry: {dead_exempt}"


def test_registry_and_declarations_stay_in_sync():
    """A harness removed from the registry must not keep dead rows here."""
    declared = {(r["harness"]) for r in D}
    ghosts = declared - set(HARNESS_CAPABILITIES)
    assert not ghosts, f"declaration rows for unknown harnesses: {ghosts}"


def test_declarations_are_unique():
    """_decl() takes the first row; a second copy would be silently ignored."""
    keys = [(r["harness"], r["place"], r["cap"]) for r in D]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    assert not dupes, f"duplicate ring declarations: {dupes}"


# ── Direction 2: stale-exception re-derivation ─────────────────────────────
# A "no" claim must MATCH a verifiable fact. We re-derive the facts the system
# actually controls:

def test_stale_exceptions_container_images():
    """container-place must match compose_renderer.HARNESS_IMAGES, both ways.

    The first version looked for a docker/mc-<harness>-agent directory. That
    missed openclaude (image mc-agent-base) and omp (built from
    docker/omp-bridge/): openclaude's container column stayed "no image" for
    weeks while the image shipped. The image registry is the fact.
    """
    for row in D:
        if row["place"] != "container" or row["cap"] != "container-place":
            continue
        has_image = row["harness"] in HARNESS_IMAGES
        if row["status"] == "no":
            assert not has_image, (
                f"stale exception: {row['harness']} declared container-place 'no' "
                f"(since {row.get('since')}) but compose_renderer.HARNESS_IMAGES maps it to "
                f"{HARNESS_IMAGES.get(row['harness'])} — update the declaration (yes with proof)"
            )
        if row["status"] == "yes":
            assert has_image, (
                f"{row['harness']} declares container-place 'yes' but has no entry in "
                "compose_renderer.HARNESS_IMAGES"
            )


def test_stale_exceptions_host_adapters():
    """host container-place must match host_harness_adapter.HOST_ADAPTERS, both ways."""
    for row in D:
        if row["place"] != "host" or row["cap"] != "container-place":
            continue
        has_adapter = row["harness"] in hha.HOST_ADAPTERS
        if row["status"] == "no":
            assert not has_adapter, (
                f"stale exception: {row['harness']} declared host container-place 'no' "
                f"(since {row.get('since')}) but HOST_ADAPTERS has an adapter for it — "
                "update the declaration (yes with proof)"
            )
        if row["status"] == "yes":
            assert has_adapter, f"{row['harness']} declares a host place but has no HOST_ADAPTERS entry"


def test_stale_exceptions_ssh_scope_claim():
    """ssh-push 'no' must still match the credential's scope reality."""
    for row in D:
        if row["cap"] != "ssh-push" or row["status"] != "no":
            continue
        # The lock applies to HTTPS/OAuth pushes only; the SSH path exists.
        # Re-derived from the TRACKED marker docs/credential-scope-notes.md —
        # an untracked operator file made this check vacuous in CI (Rex review
        # 2026-09-16): missing basis must FAIL, never pass silently.
        marker = REPO_ROOT.parent / "docs" / "credential-scope-notes.md"
        assert marker.exists(), (
            "docs/credential-scope-notes.md (tracked basis of the ssh-push "
            "'no' claim) is missing — restore it, the guard refuses "
            "to run on an unprovable claim"
        )
        text = marker.read_text(encoding="utf-8")
        # The marker must still record the canonical claim: agent credentials
        # carry NO workflow scope. If the operator re-verified and the claim
        # flipped, they update the marker's claim line — then this goes red
        # and the declarations must follow.
        if not re.search(r"carry[^\n]*\bno\b[^\n]*workflow", text, re.IGNORECASE):
            pytest.fail(
                "stale exception: ssh-push 'no' but docs/"
                "credential-scope-notes.md no longer records 'agent "
                "credentials carry no workflow scope' — re-verify the "
                "credential and flip the ssh-push declarations to yes"
            )


def test_dev_ring_doc_exists():
    """The dev-ring handreichung must exist and name every layer-A..C item."""
    doc = REPO_ROOT.parent / "docs" / "harness-dev-ring.md"
    assert doc.exists(), "docs/harness-dev-ring.md missing — the dev ring needs its handbook"
    text = doc.read_text(encoding="utf-8")
    for cap in CAPABILITIES:
        assert cap in text, f"docs/harness-dev-ring.md does not mention capability '{cap}'"
