"""The Operating Card's verb list must be filtered by the agent's scopes.

Measured problem (2026-07-28): the worst-case Operating Card (orchestrator +
is_board_lead=True + harness=claude) is 5106 bytes, and 2563 of those are
the '## Verbs' section — half the card. It rendered ALL 37 CANONICAL_VERBS
for every agent, including board-lead-only admin verbs (`plugin-assign`,
`plugin-unassign`, `plugin-list`, `plugin-show`, `worker-restart`) that a
developer agent (e.g. Sparky) cannot call — the backend answers 403 on all
of them (`require_scope(Scope.AGENTS_MANAGE)` in agent_scoped.py). The
agent burns context budget on verbs it is not allowed to use and is
tempted to try them anyway.

Fix: filter CANONICAL_VERBS through the agent's effective scopes, mirroring
the pattern _generate_tools_md/tools_md_builder.generate_tools_md already
uses for TOOLS.md sections — a verb whose CANONICAL_VERB_SCOPES entry is a
real Scope only appears when that scope is in the agent's effective scopes.
Verbs with a None entry (task-agnostic, no require_scope: `me`, `recover`,
`inbox`, `docs`) are base verbs and always appear.

Empty scopes list on the DB row means "ALL_SCOPES" (backward compat, see
scopes.get_agent_effective_scopes) — such an agent must keep seeing every
verb, or existing agents lose tools out from under them on next sync.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

from app.agent_doc_constants import CANONICAL_VERB_SCOPES, CANONICAL_VERBS
from app.models.agent import Agent
from app.scopes import ALL_SCOPES, Scope
from app.services.template_renderer import build_agent_context, render_agent_file

REPO_ROOT = Path(__file__).resolve().parents[2]
MC_CLI_PATH = REPO_ROOT / "scripts" / "mc-cli"
if str(MC_CLI_PATH) not in sys.path:
    sys.path.insert(0, str(MC_CLI_PATH))

PLUGIN_ADMIN_VERBS = {"plugin-list", "plugin-show", "plugin-assign", "plugin-unassign", "worker-restart"}


def _make_agent(*, scopes: list[str] | None, is_board_lead: bool = False, role: str = "developer") -> Agent:
    return Agent(
        id=uuid.uuid4(),
        name="TestAgent",
        role=role.capitalize(),
        board_id=uuid.uuid4(),
        is_board_lead=is_board_lead,
        harness="claude",
        scopes=scopes,
    )


def _card(agent: Agent) -> str:
    ctx = build_agent_context(agent, agents_on_board=[])
    return render_agent_file("CARD.md.j2", ctx)


def _verbs_section(card: str) -> str:
    return card.split("## Verbs")[1].split("## ")[0]


# ── Contract: CANONICAL_VERB_SCOPES agrees with the mc-cli REGISTRY ──────
#
# scripts/mc-cli/mc_cli/commands.py::REGISTRY already carries a `scope`
# field per verb — CANONICAL_VERB_SCOPES is a second source of the same
# fact, duplicated because backend/ has no runtime import path to
# scripts/mc-cli/ (not shipped into the backend Docker image; verified: no
# scripts/mc-cli under /app in the container). Two unlinked sources of the
# same truth is exactly the class of bug that let Boss run a 3-month-stale
# SOUL.md (sync + launcher pointed at different paths, nobody noticed) —
# so this test binds the two dicts together at test time, the only time
# both are importable in the same process.
def test_verb_scopes_match_the_mc_cli_registry_values():
    """CANONICAL_VERB_SCOPES[verb] must equal REGISTRY[verb].scope for
    every verb — "" (REGISTRY's no-scope convention, e.g. `docs`) and None
    (this dict's no-scope convention) both mean "no require_scope beyond
    require_agent" and compare equal.

    inbox / me / recover were verified by reading the actual endpoints
    (2026-07-28) and are `""` in both dicts as of this commit — no
    exceptions needed. If a future REGISTRY edit reintroduces a mismatch,
    it must be a deliberate, verified change to CANONICAL_VERB_SCOPES too.
    """
    from mc_cli.commands import REGISTRY

    mismatches = []
    for verb, our_scope in CANONICAL_VERB_SCOPES.items():
        registry_scope = REGISTRY[verb].scope or None  # "" -> None, same convention
        ours = our_scope or None
        if ours != registry_scope:
            mismatches.append(f"{verb}: CANONICAL_VERB_SCOPES={ours!r} REGISTRY={registry_scope!r}")
    assert not mismatches, "verb scope drift between the two sources:\n" + "\n".join(mismatches)


# ── Contract: every verb has a scope mapping ─────────────────────────────

def test_every_canonical_verb_has_a_scope_mapping():
    """CANONICAL_VERB_SCOPES must cover exactly the CANONICAL_VERBS keys —
    a new verb added to one without the other is a silent leak (either an
    undocumented gate, or a verb nobody can ever see filtered out)."""
    missing = set(CANONICAL_VERBS) - set(CANONICAL_VERB_SCOPES)
    extra = set(CANONICAL_VERB_SCOPES) - set(CANONICAL_VERBS)
    assert not missing, f"CANONICAL_VERB_SCOPES missing entries for: {sorted(missing)}"
    assert not extra, f"CANONICAL_VERB_SCOPES has stale entries not in CANONICAL_VERBS: {sorted(extra)}"


def test_scope_mappings_use_real_scope_values():
    """Every non-None mapping must be a real Scope enum value — a typo'd
    string would silently filter a verb out for everybody."""
    valid = set(ALL_SCOPES)
    bad = {
        verb: scope
        for verb, scope in CANONICAL_VERB_SCOPES.items()
        if scope is not None and scope not in valid
    }
    assert not bad, f"CANONICAL_VERB_SCOPES has unknown scope values: {bad}"


# ── Behavioural: developer vs board lead ─────────────────────────────────

def test_developer_agent_does_not_see_plugin_admin_verbs():
    """Sparky (developer, no agents:manage) must not see verbs the backend
    answers 403 on — Scope.AGENTS_MANAGE gates all five."""
    from app.scopes import get_default_scopes, AgentRole

    agent = _make_agent(scopes=get_default_scopes(AgentRole.DEVELOPER))
    card = _card(agent)
    verbs = _verbs_section(card)
    present = {v for v in PLUGIN_ADMIN_VERBS if f"`{v}`" in verbs}
    assert not present, f"developer card leaks board-lead-only verbs: {present}"


def test_board_lead_sees_plugin_admin_verbs():
    """A board lead (ALL_SCOPES via orchestrator/lead defaults) keeps the
    admin verbs — the filter must not over-remove."""
    agent = _make_agent(scopes=ALL_SCOPES, is_board_lead=True, role="orchestrator")
    card = _card(agent)
    verbs = _verbs_section(card)
    missing = {v for v in PLUGIN_ADMIN_VERBS if f"`{v}`" not in verbs}
    assert not missing, f"board-lead card is missing admin verbs it may call: {missing}"


def test_empty_scopes_is_backward_compatible_and_sees_every_verb():
    """scopes=[] in the DB means ALL_SCOPES (get_agent_effective_scopes) —
    an agent provisioned before this feature must not silently lose verbs
    on its next sync-config."""
    agent = _make_agent(scopes=[])
    card = _card(agent)
    verbs = _verbs_section(card)
    missing = [v for v in CANONICAL_VERBS if f"`{v}`" not in verbs]
    assert not missing, f"scopes=[] card is missing verbs (backward-compat break): {missing}"


def test_developer_card_is_smaller_than_board_lead_card():
    """The measured effect: fewer verbs -> fewer bytes."""
    from app.scopes import get_default_scopes, AgentRole

    dev = _make_agent(scopes=get_default_scopes(AgentRole.DEVELOPER))
    lead = _make_agent(scopes=ALL_SCOPES, is_board_lead=True, role="orchestrator")
    dev_size = len(_card(dev).encode("utf-8"))
    lead_size = len(_card(lead).encode("utf-8"))
    assert dev_size < lead_size, f"developer card ({dev_size}B) is not smaller than lead card ({lead_size}B)"


# ── Generic guarantee: nobody loses a verb their scopes allow ────────────

@pytest.mark.parametrize("scope", sorted(ALL_SCOPES))
def test_agent_with_exactly_one_scope_sees_every_verb_gated_by_it(scope):
    """The strongest guard: for EVERY real scope, an agent whose only scope
    is that one must see every verb mapped to it (and every base verb —
    scope=None — which is always visible), and must not need any other
    scope to see them. Written generically over app.scopes.ALL_SCOPES so a
    newly added scope/verb pair is covered automatically, not by
    enumeration."""
    agent = _make_agent(scopes=[scope])
    card = _card(agent)
    verbs = _verbs_section(card)

    expected = {v for v, s in CANONICAL_VERB_SCOPES.items() if s is None or s == scope}
    missing = {v for v in expected if f"`{v}`" not in verbs}
    assert not missing, f"agent with only scope={scope!r} lost verb(s) it may call: {missing}"


def test_base_verbs_always_present_even_with_a_single_unrelated_scope():
    """Base verbs (no require_scope beyond require_agent: me, recover,
    inbox, docs) must survive even the narrowest possible agent."""
    base_verbs = {v for v, s in CANONICAL_VERB_SCOPES.items() if s is None}
    assert base_verbs, "expected at least one base (scope=None) verb"
    agent = _make_agent(scopes=[Scope.HEARTBEAT.value])
    card = _card(agent)
    verbs = _verbs_section(card)
    missing = {v for v in base_verbs if f"`{v}`" not in verbs}
    assert not missing, f"base verbs missing for a HEARTBEAT-only agent: {missing}"
