"""Context-economy Stage 2 — contract + sync tests for the L1 Operating Card.

CARD.md.j2 (<=5KB, backend/templates/CARD.md.j2) is the pilot replacement
for the full ~29KB SOUL.md --append-system-prompt (Migration 0151,
agent.use_operating_card). Mirrors test_agent_docs_contract.py's structure:
denylist, verb-syntax dry-run, and here additionally the byte budget across
the full role x lead-status x harness matrix, plus the sync write/remove
lifecycle (test_host_agent_sync.py / test_docker_agent_sync_runtime.py
pattern).
"""
from __future__ import annotations

import re
import shlex
import sys
import uuid
from pathlib import Path

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.agent_doc_constants import FORBIDDEN_VERB_PATTERNS
from app.models.agent import Agent
from app.services.docker_agent_sync import sync_docker_agent_files, sync_host_agent_files
from app.services.template_renderer import build_agent_context, render_agent_file
from tests.conftest import test_engine

REPO_ROOT = Path(__file__).resolve().parents[2]
MC_CLI_PATH = REPO_ROOT / "scripts" / "mc-cli"
if str(MC_CLI_PATH) not in sys.path:
    sys.path.insert(0, str(MC_CLI_PATH))

CARD_BYTE_BUDGET = 5120  # 5KB

ROLES = ["orchestrator", "reviewer", "researcher", "deployer", "tester", "writer", "designer", "developer"]


def _make_agent(role: str, *, is_lead: bool, harness: str) -> Agent:
    return Agent(
        id=uuid.uuid4(),
        name=f"Test{role.capitalize()}",
        role=role.capitalize(),
        board_id=uuid.uuid4(),
        is_board_lead=is_lead,
        harness=harness,
    )


def _render_card(agent: Agent) -> str:
    ctx = build_agent_context(agent, agents_on_board=[])
    return render_agent_file("CARD.md.j2", ctx)


# ── (a) Byte budget ──────────────────────────────────────────────────────

@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("is_lead", [True, False])
@pytest.mark.parametrize("harness", ["omp", "claude"])
def test_card_respects_byte_budget(role, is_lead, harness):
    agent = _make_agent(role, is_lead=is_lead, harness=harness)
    card = _render_card(agent)
    size = len(card.encode("utf-8"))
    assert size <= CARD_BYTE_BUDGET, (
        f"CARD.md for role={role} is_lead={is_lead} harness={harness} "
        f"is {size} bytes, over the {CARD_BYTE_BUDGET} budget"
    )


# ── (b) Denylist ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("is_lead", [True, False])
def test_card_has_no_forbidden_pattern(role, is_lead):
    agent = _make_agent(role, is_lead=is_lead, harness="claude")
    card = _render_card(agent)
    violations = []
    for name, pattern in FORBIDDEN_VERB_PATTERNS.items():
        m = pattern.search(card)
        if m:
            violations.append(f"{name} matched {m.group(0)!r}")
    assert not violations, "\n".join(violations)


# ── (c) Safety-marker presence ───────────────────────────────────────────

SAFETY_MARKERS = [
    "ACK first",
    "mc ack",
    "Never guess",
    "--private",
    "Vault, never inline",
    "5-Minute-Blocker Rule",
    "mc blocked",
    "/deliverables/<task_id>/",
]


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("is_lead", [True, False])
def test_card_contains_core_safety_gates(role, is_lead):
    agent = _make_agent(role, is_lead=is_lead, harness="claude")
    card = _render_card(agent)
    missing = [marker for marker in SAFETY_MARKERS if marker not in card]
    assert not missing, f"CARD.md missing safety markers {missing} (role={role}, is_lead={is_lead})"


# ── (d) omp excludes the close example, claude includes it ──────────────

def test_card_omp_harness_excludes_close_example():
    agent = _make_agent("developer", is_lead=False, harness="omp")
    card = _render_card(agent)
    # "mc finish" legitimately appears in the verb table's `done` description
    # ("prefer `mc finish`") — the close-protocol section header is the
    # actual thing omp must not carry (bridge.py injects its own
    # COMPLETION_INSTRUCTIONS just-in-time per prompt).
    assert "## Close protocol" not in card


def test_card_claude_harness_includes_close_example():
    agent = _make_agent("developer", is_lead=False, harness="claude")
    card = _render_card(agent)
    assert "## Close protocol" in card
    assert "`mc finish [--review]`" in card


# ── (e) `mc` examples parse through the real argparse ────────────────────

_INLINE_MC_SPAN_RE = re.compile(r"`(mc [^`\n]+)`")
_VERB_SLOT_RE = re.compile(r"^[a-z][a-z-]*$")


def _inline_mc_examples_worth_checking(markdown: str) -> list[str]:
    worth_checking = []
    for span in _INLINE_MC_SPAN_RE.findall(markdown):
        try:
            tokens = shlex.split(span)
        except ValueError:
            continue
        if len(tokens) < 2:
            continue
        verb_slot = tokens[1]
        if not _VERB_SLOT_RE.match(verb_slot):
            continue
        if not any(t.startswith("-") for t in tokens[2:]):
            continue
        worth_checking.append(span)
    return worth_checking


def _dry_run_parse(argv_line: str) -> None:
    from mc_cli.__main__ import build_parser

    tokens = shlex.split(argv_line)
    assert tokens and tokens[0] == "mc", f"unexpected extracted line: {argv_line!r}"
    build_parser().parse_args(tokens[1:])


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("is_lead", [True, False])
@pytest.mark.parametrize("harness", ["omp", "claude"])
def test_card_mc_examples_parse_with_real_argparse(role, is_lead, harness):
    agent = _make_agent(role, is_lead=is_lead, harness=harness)
    card = _render_card(agent)
    failures = []
    for line in _inline_mc_examples_worth_checking(card):
        try:
            _dry_run_parse(line)
        except SystemExit as e:
            if e.code:
                failures.append(f"{line!r} -> argparse exit {e.code}")
        except ValueError as e:
            failures.append(f"{line!r} -> shlex error: {e}")
    assert not failures, "\n".join(failures)


# ── (f) Sync: flag on writes CARD.md, flag off removes it ───────────────

def _make_docker_agent(*, use_operating_card: bool) -> Agent:
    return Agent(
        id=uuid.uuid4(),
        name="CardSyncAgent",
        role="developer",
        emoji="🧪",
        agent_runtime="cli-bridge",
        harness="omp",
        use_operating_card=use_operating_card,
        scopes=["vault:read", "vault:write", "chat:write", "heartbeat"],
        tools_md="# TOOLS\n\nfake tools content",
    )


@pytest.mark.asyncio
async def test_sync_docker_writes_card_when_flag_on(tmp_path: Path, monkeypatch):
    slug = "cardsyncagent"
    agents_dir = tmp_path / "agents"
    (agents_dir / slug / "claude-config").mkdir(parents=True)
    monkeypatch.setattr("app.services.docker_agent_sync.AGENTS_DIR", agents_dir)

    agent = _make_docker_agent(use_operating_card=True)
    agent.name = "CardSyncAgent"
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
        results = await sync_docker_agent_files(s, agent)

    card_path = agents_dir / slug / "claude-config" / "CARD.md"
    assert card_path.exists()
    assert len(card_path.read_bytes()) <= CARD_BYTE_BUDGET
    assert results.get("CARD.md", "").startswith("written")


@pytest.mark.asyncio
async def test_sync_docker_removes_card_when_flag_off(tmp_path: Path, monkeypatch):
    slug = "cardsyncagentoff"
    config_dir = tmp_path / "agents" / slug / "claude-config"
    config_dir.mkdir(parents=True)
    (config_dir / "CARD.md").write_text("stale card content", encoding="utf-8")
    monkeypatch.setattr("app.services.docker_agent_sync.AGENTS_DIR", tmp_path / "agents")

    agent = _make_docker_agent(use_operating_card=False)
    agent.name = "CardSyncAgentOff"
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
        results = await sync_docker_agent_files(s, agent)

    assert not (config_dir / "CARD.md").exists()
    assert results.get("CARD.md") == "removed (use_operating_card=false)"


@pytest.mark.asyncio
async def test_sync_host_writes_card_when_flag_on(tmp_path: Path):
    workspace = tmp_path / "boss"
    workspace.mkdir()
    agent = Agent(
        id=uuid.uuid4(),
        name="CardHostAgent",
        role="orchestrator",
        emoji="🧪",
        is_board_lead=True,
        agent_runtime="host",
        harness="claude",
        workspace_path=str(workspace),
        use_operating_card=True,
        scopes=["vault:read", "vault:write", "chat:write", "heartbeat"],
        tools_md="# TOOLS\n\nfake tools content",
    )
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
        results = await sync_host_agent_files(s, agent)

    card_path = workspace / "claude-config" / "CARD.md"
    assert card_path.exists()
    assert results.get("CARD.md", "").startswith("written")


@pytest.mark.asyncio
async def test_sync_host_removes_card_when_flag_off(tmp_path: Path):
    workspace = tmp_path / "boss-off"
    config_dir = workspace / "claude-config"
    config_dir.mkdir(parents=True)
    (config_dir / "CARD.md").write_text("stale", encoding="utf-8")
    agent = Agent(
        id=uuid.uuid4(),
        name="CardHostAgentOff",
        role="orchestrator",
        emoji="🧪",
        is_board_lead=True,
        agent_runtime="host",
        harness="claude",
        workspace_path=str(workspace),
        use_operating_card=False,
        scopes=["vault:read", "vault:write", "chat:write", "heartbeat"],
        tools_md="# TOOLS\n\nfake tools content",
    )
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
        results = await sync_host_agent_files(s, agent)

    assert not (config_dir / "CARD.md").exists()
    assert results.get("CARD.md") == "removed (use_operating_card=false)"
