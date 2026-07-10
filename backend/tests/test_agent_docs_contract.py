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
import shlex
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


# ── Flag/subcommand syntax (dry-run argparse) ────────────────────────────
#
# Verb-existence (above) only checks that `mc <verb>` names a real command —
# it would NOT have caught `mc memory --query "..."` (real syntax: `mc
# memory search "..."`, a required subparser) because "memory" IS a real
# verb. This extracts every literal `mc ...` invocation from ```bash fenced
# blocks and dry-runs it through the actual argparse parser, so a wrong
# flag/subcommand fails loudly instead of being copy-pasted by an agent.

def _mc_argv_lines_from_bash_blocks(markdown: str) -> list[str]:
    """Extracts complete, parseable `mc ...` invocations from ```bash fenced
    code blocks.

    Joins backslash line-continuations and splits on `|` (for piped
    examples like `cat foo | mc pdf ...`). Deliberately SKIPS anything that
    crosses a heredoc (`<<`) or command-substitution (`$(`) boundary — those
    aren't flat, single-line argv strings (e.g. `mc finish [--review]
    "$(cat <<'EOF' ...)"` mixes bracket-optional documentation notation with
    multi-line shell composition) and can't be tokenized without a real
    shell parser. Coverage gap is intentional and narrow: it only affects
    doc lines that were never going to be literal copy-paste examples to
    begin with.
    """
    commands: list[str] = []
    in_bash_block = False
    in_heredoc = False
    heredoc_terminator: str | None = None
    pending = ""

    for raw_line in markdown.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            in_bash_block = (stripped == "```bash") if not in_bash_block else False
            continue
        if not in_bash_block:
            continue
        if in_heredoc:
            if stripped == heredoc_terminator:
                in_heredoc = False
                heredoc_terminator = None
            continue

        line = f"{pending} {stripped}".strip() if pending else stripped
        pending = ""
        if line.endswith("\\"):
            pending = line[:-1].strip()
            continue

        for segment in (s.strip() for s in line.split("|")):
            if not segment.startswith("mc "):
                continue
            heredoc_match = re.search(r"<<\s*['\"]?(\w+)['\"]?", segment)
            if "$(" in segment or heredoc_match:
                if heredoc_match:
                    in_heredoc = True
                    heredoc_terminator = heredoc_match.group(1)
                continue
            commands.append(segment)
    return commands


_INLINE_MC_SPAN_RE = re.compile(r"`(mc [^`\n]+)`")
_VERB_SLOT_RE = re.compile(r"^[a-z][a-z-]*$")


def _inline_mc_examples_worth_checking(markdown: str) -> list[str]:
    """Extracts single-backtick inline `mc ...` spans that look like complete
    invocations, e.g. `` `mc memory --query "<text>"` `` in running prose
    (NOT inside a fenced code block — that's _mc_argv_lines_from_bash_blocks).

    This is the span type that actually contained the memory.md bug — a
    fenced-block-only scan would have missed it entirely.

    Deliberately narrow filter (two conditions, both required) to avoid
    flooding on the many legitimate bare/partial verb *references* that
    aren't meant as literal copy-paste commands (`` `mc pdf` ``, `` `mc
    comment handoff` ``, `` `mc <verb> --help` ``):
    1. the verb slot (token right after "mc") must look like a real verb
       identifier (`^[a-z][a-z-]*$`) — excludes placeholder notation like
       `<verb>`/`<topic>`.
    2. the span must contain at least one `-`-prefixed flag token — this is
       exactly the shape of the class of bug found in review (a wrong/
       invented flag used in place of the real subcommand or flag name).
       Bare verb mentions and positional-only fragments (`` `mc checklist
       add "..."` ``) are skipped; a dedicated flag is required to trigger
       the check.
    """
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
    """Parses one `mc ...` line through the real mc-cli argparse parser.

    Raises SystemExit (code 2) on an unrecognized flag/subcommand or a
    missing required argument — exactly what an agent copy-pasting a broken
    doc example would hit.
    """
    from mc_cli.__main__ import build_parser

    tokens = shlex.split(argv_line)
    assert tokens and tokens[0] == "mc", f"unexpected extracted line: {argv_line!r}"
    build_parser().parse_args(tokens[1:])


def test_l2_docs_mc_examples_parse_with_real_argparse():
    """Every `mc ...` example that reads as a complete invocation — whether
    in a fenced ```bash block or an inline single-backtick span with a flag
    — must be valid against the real mc-cli parser (flags/subcommand
    structure — not values; placeholders like <uuid> are fine).

    Regression test for the memory.md `mc memory --query` bug (real syntax:
    `mc memory search "<text>"`, a required subparser) — that bug lived in
    inline prose, not a fenced block, which is why both extraction paths
    are needed. A hardcoded example with wrong flags must fail this test,
    not silently teach broken syntax.
    """
    docs = generate_reference_docs({"operator_name": "Mark"})
    failures = []
    for topic, content in docs.items():
        examples = _mc_argv_lines_from_bash_blocks(content) + _inline_mc_examples_worth_checking(content)
        for line in examples:
            try:
                _dry_run_parse(line)
            except SystemExit as e:
                # exit 0 = argparse's own `--help` (prints usage, not a
                # syntax error); only a non-zero exit means a bad flag/
                # subcommand/missing-required-argument.
                if e.code:
                    failures.append(f"docs/{topic}.md: {line!r} -> argparse exit {e.code}")
            except ValueError as e:
                failures.append(f"docs/{topic}.md: {line!r} -> shlex error: {e}")
    assert not failures, "\n".join(failures)


def test_every_l2_doc_starts_with_h1_title():
    docs = generate_reference_docs({"operator_name": "Mark"})
    for topic, content in docs.items():
        assert content.startswith("# "), f"docs/{topic}.md must start with '# <Title>', got: {content[:40]!r}"
