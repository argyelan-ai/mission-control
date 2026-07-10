"""Context-economy Stage 1 — contract tests for the L2 reference docs.

Protects three invariants:
1. Denylist: no rendered agent-facing doc (SOUL.md, TOOLS.md, or any L2
   topic doc, across roles/lead-status) ever re-teaches a pattern W1 fixed
   (see test_agent_docs_protocol_coherence.py for the bugs themselves).
2. Every `mc <verb>` mentioned in a rendered doc is a real, registered
   CLI command — and CANONICAL_VERBS never drifts from the real REGISTRY.
3. Byte budget + INDEX<->topic bijection stay intact, so L2 docs remain
   genuinely on-demand-sized rather than growing into a second SOUL.md.
"""
from __future__ import annotations

import re
import sys
import uuid
from pathlib import Path

import pytest

from app.agent_doc_constants import CANONICAL_VERBS, DOC_TOPICS, FORBIDDEN_VERB_PATTERNS
from app.models.agent import Agent
from app.services.reference_docs_builder import generate_docs_index, generate_reference_docs
from app.services.template_renderer import build_agent_context, render_agent_file
from app.services.tools_md_builder import generate_tools_md

REPO_ROOT = Path(__file__).resolve().parents[2]
MC_CLI_PATH = REPO_ROOT / "scripts" / "mc-cli"
if str(MC_CLI_PATH) not in sys.path:
    sys.path.insert(0, str(MC_CLI_PATH))


# ── Denylist ──────────────────────────────────────────────────────────────

def _check_no_forbidden_patterns(text: str, source: str) -> None:
    violations = []
    for name, pattern in FORBIDDEN_VERB_PATTERNS.items():
        m = pattern.search(text)
        if m:
            violations.append(f"{name} matched {m.group(0)!r} in {source}")
    assert not violations, "\n".join(violations)


ROLES = ["orchestrator", "reviewer", "developer", "lead"]


def _make_agent(role: str) -> Agent:
    is_lead = role == "lead"
    return Agent(
        id=uuid.uuid4(),
        name="TestAgent",
        # build_agent_context derives role_type from the first word of
        # `role` (unless the agent NAME is one of the well-known ones) —
        # "Lead" (not "Board Lead") is what maps to role_type == "lead".
        role="Lead" if is_lead else role,
        board_id=uuid.uuid4(),
        is_board_lead=is_lead,
    )


@pytest.mark.parametrize("role", ROLES)
def test_rendered_soul_has_no_forbidden_pattern(role):
    agent = _make_agent(role)
    ctx = build_agent_context(agent, agents_on_board=[])
    soul = render_agent_file("SOUL.md.j2", ctx)
    _check_no_forbidden_patterns(soul, f"SOUL.md (role={role})")


@pytest.mark.parametrize("is_board_lead", [True, False])
def test_rendered_tools_md_has_no_forbidden_pattern(is_board_lead):
    result = generate_tools_md(
        name="TestAgent",
        emoji="🤖",
        raw_token="tok",
        board_id="board-uuid-123",
        is_board_lead=is_board_lead,
    )
    _check_no_forbidden_patterns(result, f"TOOLS.md (is_board_lead={is_board_lead})")


def test_all_l2_docs_have_no_forbidden_pattern():
    docs = generate_reference_docs({"operator_name": "Mark"})
    for topic, content in docs.items():
        _check_no_forbidden_patterns(content, f"docs/{topic}.md")


# ── Verb existence ────────────────────────────────────────────────────────

def _cli_registry():
    from mc_cli.commands import REGISTRY
    return REGISTRY


def test_canonical_verbs_are_registered():
    """Every CANONICAL_VERBS key is a real mc_cli REGISTRY command."""
    registry = _cli_registry()
    missing = set(CANONICAL_VERBS) - set(registry)
    assert not missing, f"CANONICAL_VERBS has verbs not in REGISTRY: {sorted(missing)}"


def test_registry_verbs_all_documented_or_intentionally_absent():
    """Every REGISTRY verb should be documented in CANONICAL_VERBS.

    A verb missing here isn't a hard bug (some are purely mechanical /
    board-lead-admin), but it should be a deliberate omission, not a
    forgotten one — this test lists exactly what's missing so a reviewer
    can decide.
    """
    registry = _cli_registry()
    undocumented = set(registry) - set(CANONICAL_VERBS)
    # Board-lead plugin/worker administration verbs — not part of the
    # agent-facing task lifecycle vocabulary these docs teach.
    intentionally_absent = {
        "plugin-list", "plugin-show", "plugin-assign", "plugin-unassign",
        "worker-restart",
    }
    unexpected = undocumented - intentionally_absent
    assert not unexpected, (
        f"REGISTRY verbs missing from CANONICAL_VERBS with no justification: "
        f"{sorted(unexpected)}"
    )


def _mc_verbs_in_text(text: str) -> set[str]:
    return set(re.findall(r"\bmc ([a-z][a-z-]*)\b", text))


# NOTE: intentionally scoped to the L2 docs only, not the legacy SOUL.md.
# SOUL.md legitimately references *non-existent* verbs in negative-example
# prose (e.g. "No `mc docx`/`mc xlsx` primitive needed", "FORBIDDEN: `mc
# task-create` + `mc blocked` as separate calls") — a plain `\bmc
# ([a-z-]+)\b` scan can't distinguish "here's how to call it" from "this
# does NOT exist" without negation-aware parsing, which is out of scope for
# Stage 1. The L2 docs are new, curated, and controlled by this contract, so
# the same regex is a reliable signal there.
def test_l2_docs_verbs_exist_in_canonical_verbs():
    docs = generate_reference_docs({"operator_name": "Mark"})
    registry = _cli_registry()
    for topic, content in docs.items():
        verbs = _mc_verbs_in_text(content)
        unknown = {v for v in verbs if v not in registry}
        assert not unknown, f"docs/{topic}.md references unknown `mc` verbs: {sorted(unknown)}"


# ── Byte budget ───────────────────────────────────────────────────────────

def test_l2_docs_respect_byte_budget():
    docs = generate_reference_docs({"operator_name": "Mark"})
    over_budget = []
    for topic, content in docs.items():
        spec = DOC_TOPICS[topic]
        size = len(content.encode("utf-8"))
        if size > spec.max_bytes:
            over_budget.append(f"{topic}: {size} > {spec.max_bytes}")
    assert not over_budget, "\n".join(over_budget)


# ── Index <-> topics bijection ───────────────────────────────────────────

def test_index_lists_every_topic_and_no_extra():
    docs = generate_reference_docs({"operator_name": "Mark"})
    index = generate_docs_index(docs)
    for topic in docs:
        assert f"`{topic}`" in index, f"docs/INDEX.md is missing an entry for '{topic}'"
    # Every topic slug mentioned in the index must be a real DOC_TOPICS key —
    # guards against stray/typo'd entries surviving a refactor.
    mentioned = set(re.findall(r"`([a-z][a-z-]*)`", index))
    unknown_in_index = mentioned - set(DOC_TOPICS)
    assert not unknown_in_index, f"INDEX.md references unknown topics: {sorted(unknown_in_index)}"


def test_doc_topics_have_matching_templates():
    """Every DOC_TOPICS entry has a backend/templates/docs/<topic>.md.j2."""
    templates_dir = REPO_ROOT / "backend" / "templates" / "docs"
    missing = [
        topic for topic in DOC_TOPICS
        if not (templates_dir / f"{topic}.md.j2").is_file()
    ]
    assert not missing, f"DOC_TOPICS entries with no template file: {missing}"


def test_every_l2_doc_starts_with_h1_title():
    docs = generate_reference_docs({"operator_name": "Mark"})
    for topic, content in docs.items():
        assert content.startswith("# "), f"docs/{topic}.md must start with '# <Title>', got: {content[:40]!r}"
