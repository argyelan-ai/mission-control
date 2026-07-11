"""Workstream W1-A: agent-facing docs/rules must teach the CORRECT mc-first
interaction protocol — not legacy/contradictory patterns.

Regression tests for three bugs found in an audit of the standing docs
agents read at provision time:

1. TOOLS.md (tools_md_builder.generate_tools_md) taught a broken two-step
   close (`mc comment --type reflection ...` + `mc done`) — `--type` is
   invalid syntax for `mc comment` (the type is positional), and the
   two-step flow is exactly what `mc finish` replaces atomically.
   It also documented checklist creation via raw curl in parallel to the
   `mc checklist` CLI.
2. `_CLI_BRIDGE_PROTOCOL` (cli_terminal.py) — appended to EVERY provisioned
   cli-bridge agent's system prompt — taught raw `PATCH status: in_progress`
   with no mention of the `mc` CLI, predating it entirely.
3. SOUL.md.j2's orchestrator "HARD GATE #1" showed a raw curl PATCH without
   the `X-Dispatch-Attempt-Id` header, which the backend rejects with 409
   by design — agents copy-pasting that example would hard-fail their very
   first action.
4. template_seeder.py's builtin AgentTemplates (seeded into the DB on every
   backend startup, overwriting soul_md when it differs) still taught the
   legacy protocol: "ACK: Immediately PATCH status: in_progress" and
   "Create a checkpoint ... (comment_type: \"checkpoint\")" — `checkpoint`
   isn't even a valid comment_type anymore, making the guidance actively
   harmful. Every agent created from "Add Agent → <builtin template>" got
   instructions contradicting the fixed SOUL/TOOLS.
"""
import uuid

import pytest

from app.models.agent import Agent
from app.routers.cli_terminal import _CLI_BRIDGE_PROTOCOL
from app.services.template_renderer import build_agent_context, render_agent_file
from app.services.template_seeder import BUILTIN_TEMPLATES
from app.services.tools_md_builder import generate_tools_md


# ── 1. TOOLS.md — mc finish is the documented close verb ────────────────────

def test_tools_md_documents_mc_finish():
    """TOOLS.md must document `mc finish` as the canonical close verb."""
    result = generate_tools_md(
        name="TestAgent",
        emoji="🤖",
        raw_token="tok",
        board_id="board-uuid-123",
        is_board_lead=False,
    )
    assert "mc finish" in result, "TOOLS.md must document `mc finish`"


def test_tools_md_has_no_invalid_comment_type_flag():
    """`mc comment --type reflection ...` is invalid syntax (type is a
    positional argument, not a --type flag) — must never appear."""
    result = generate_tools_md(
        name="TestAgent",
        emoji="🤖",
        raw_token="tok",
        board_id="board-uuid-123",
        is_board_lead=False,
    )
    assert "--type reflection" not in result
    assert "mc comment --type" not in result


def test_tools_md_has_no_raw_curl_checklist_creation():
    """Checklist creation must only be documented via `mc checklist add` —
    the raw curl POST .../checklist example (parallel, drifting path) is
    removed."""
    result = generate_tools_md(
        name="TestAgent",
        emoji="🤖",
        raw_token="tok",
        board_id="board-uuid-123",
        is_board_lead=False,
    )
    # No curl POST targeting /checklist anywhere (creation must go through
    # `mc checklist add` only).
    for line in result.splitlines():
        if "checklist" in line and "curl" in line and "POST" in line:
            raise AssertionError(f"raw curl checklist-creation line found: {line!r}")
    assert "mc checklist add" in result


def test_tools_md_documents_checklist_skip():
    """`mc checklist skip <id> --reason` must be documented for out-of-role
    items (exists since 2026-07-08, per _cmd_checklist action='skip')."""
    result = generate_tools_md(
        name="TestAgent",
        emoji="🤖",
        raw_token="tok",
        board_id="board-uuid-123",
        is_board_lead=False,
    )
    assert "mc checklist skip" in result


def test_tools_md_no_dead_checkpoint_command():
    """`mc checkpoint` does not exist as a CLI command (POST /checkpoint
    returns HTTP 410) — must not be taught as something agents can call."""
    result = generate_tools_md(
        name="TestAgent",
        emoji="🤖",
        raw_token="tok",
        board_id="board-uuid-123",
        is_board_lead=False,
    )
    # Robust against formatting: match the command itself, not a specific
    # trailing character. A prose mention like "`mc checkpoint` no longer
    # exists" is fine — an executable example line starting with the command
    # is not.
    example_lines = [
        line for line in result.splitlines()
        if line.strip().startswith("mc checkpoint")
    ]
    assert not example_lines, (
        f"`mc checkpoint` is a dead command — must not appear as an example: {example_lines}"
    )


def test_tools_md_blocked_uses_correct_flag():
    """`mc blocked` takes `--blocker-type`, not `--type` (verified against
    _add_blocked_args in scripts/mc-cli/mc_cli/commands.py)."""
    result = generate_tools_md(
        name="TestAgent",
        emoji="🤖",
        raw_token="tok",
        board_id="board-uuid-123",
        is_board_lead=False,
    )
    assert "blocked --type" not in result
    assert "mc blocked --blocker-type" in result


# ── 2. _CLI_BRIDGE_PROTOCOL — mc-first, no raw PATCH ─────────────────────────

def test_cli_bridge_protocol_teaches_mc_ack():
    assert "mc ack" in _CLI_BRIDGE_PROTOCOL


def test_cli_bridge_protocol_has_no_raw_patch_instruction():
    """The legacy block predates the mc CLI and told agents to
    'PATCH status: in_progress' directly — that fails with 409 without the
    X-Dispatch-Attempt-Id header. Must be gone."""
    assert "PATCH status:" not in _CLI_BRIDGE_PROTOCOL
    assert "POST /checklist" not in _CLI_BRIDGE_PROTOCOL
    assert "POST /deliverables" not in _CLI_BRIDGE_PROTOCOL


def test_cli_bridge_protocol_teaches_mc_finish():
    assert "mc finish" in _CLI_BRIDGE_PROTOCOL


# ── 3. SOUL.md.j2 — orchestrator hard gate uses mc ack, no header-less curl ──

def _render_boss_soul() -> str:
    """Render the Boss SOUL via the production template path (role=orchestrator)."""
    boss = Agent(
        id=uuid.uuid4(),
        name="Boss",
        role="Orchestrator",
        board_id=uuid.uuid4(),
        is_board_lead=False,
    )
    ctx = build_agent_context(boss, agents_on_board=[])
    return render_agent_file("SOUL.md.j2", ctx)


def test_orchestrator_soul_hard_gate_uses_mc_ack():
    """CORE RULES HARD GATE #1 ('ACK first. Always.') must show `mc ack`,
    not a raw curl PATCH missing the X-Dispatch-Attempt-Id header."""
    rendered = _render_boss_soul()
    assert "### 1. ACK first. Always." in rendered
    gate_section = rendered.split("### 1. ACK first. Always.", 1)[1]
    gate_section = gate_section.split("### 2.", 1)[0]
    assert "mc ack" in gate_section
    # No actual curl invocation left (prose may reference "curl" as a word
    # when explaining why mc ack exists — only a literal `curl ...` command
    # would mean the raw example is still there).
    assert "curl -X" not in gate_section and "curl -s" not in gate_section, (
        "HARD GATE #1 still shows a raw curl example instead of `mc ack`"
    )


def test_orchestrator_soul_has_no_headerless_status_patch():
    """No raw curl PATCH setting status without the X-Dispatch-Attempt-Id
    header should remain anywhere in the rendered orchestrator SOUL — that
    combination 409s by design."""
    rendered = _render_boss_soul()
    blocks = rendered.split("```")
    # Every fenced code block that PATCHes a task status must carry the
    # X-Dispatch-Attempt-Id header alongside it.
    for block in blocks:
        if "-X PATCH" in block and "/tasks/" in block and '"status"' in block:
            assert "X-Dispatch-Attempt-Id" in block, (
                f"found a header-less status-PATCH curl block:\n{block}"
            )


# ── 4. Builtin AgentTemplates (template_seeder) — mc-first protocol ──────────
#
# These soul_md strings are seeded/overwritten in the DB on EVERY backend
# startup (main.py → seed_builtin_templates), so every agent created from
# "Add Agent → <template>" inherits them. They must teach the same mc-first
# protocol as SOUL.md.j2 / TOOLS.md / _CLI_BRIDGE_PROTOCOL.

_TEMPLATE_IDS = [spec["name"] for spec in BUILTIN_TEMPLATES]


@pytest.mark.parametrize("spec", BUILTIN_TEMPLATES, ids=_TEMPLATE_IDS)
def test_builtin_template_has_no_raw_status_patch(spec):
    """No builtin template may instruct a raw `PATCH status:` — that path
    409s without the X-Dispatch-Attempt-Id header; `mc` verbs handle it."""
    assert "PATCH status:" not in spec["soul_md"], (
        f"template '{spec['name']}' still teaches raw PATCH status"
    )


@pytest.mark.parametrize("spec", BUILTIN_TEMPLATES, ids=_TEMPLATE_IDS)
def test_builtin_template_has_no_checkpoint_protocol(spec):
    """`checkpoint` is not a valid comment_type (POST /checkpoint is 410,
    the type is silent/audit-only where it survives) — the checklist is the
    progress mechanism. No template may instruct checkpoint comments."""
    soul = spec["soul_md"]
    assert 'comment_type: "checkpoint"' not in soul, (
        f"template '{spec['name']}' still teaches checkpoint comments"
    )
    for line in soul.splitlines():
        lowered = line.lower()
        if "checkpoint" in lowered:
            raise AssertionError(
                f"template '{spec['name']}' still references checkpoints: {line!r}"
            )


@pytest.mark.parametrize("spec", BUILTIN_TEMPLATES, ids=_TEMPLATE_IDS)
def test_builtin_template_teaches_mc_ack_and_mc_finish(spec):
    """Every builtin template handles tasks → must teach the mc-first ACK
    (`mc ack`) and the atomic close (`mc finish`)."""
    soul = spec["soul_md"]
    assert "mc ack" in soul, f"template '{spec['name']}' lacks `mc ack`"
    assert "mc finish" in soul, f"template '{spec['name']}' lacks `mc finish`"
