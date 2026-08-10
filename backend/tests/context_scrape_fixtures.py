"""Shared fixtures for the context%-scraper: same (harness, text, expected)
tuples feed BOTH the pure-Python test (test_context_detect_python.py) AND the
Python<->Bash equivalence test (test_context_detect_equivalence.py) — a
single source of truth so the two implementations can't quietly drift apart.

The bash-only smoke test (test_context_detect.sh, run via
test_context_detect.py) keeps its own literal fixtures — it existed first and
proves the canonical bash lib works standalone; these shared fixtures prove
the Python twin (scripts/context_detect.py) produces IDENTICAL results for
the same inputs.

Fixtures marked "real" are statuslines captured live from the running
agents (see the CTX-01 Nachzug task briefing); "synthetic" are constructed to
exercise specific regex branches (e.g. an isolated bar-percent with no
fraction nearby) that no single real capture happened to contain alone.
"""
from __future__ import annotations

# (description, harness_override_or_None, text, expected_pct_or_None)
FIXTURES: list[tuple[str, str | None, str, int | None]] = [
    (
        "hermes real: fraction + bar-percent 8%",
        None,
        " ⚕ deepseek-v4-flash-0731-... │ 21.3K/262.1K │ [█░░░░░░░░░] 8% │ 12m │ ⏲ 48s │ ✓ 0s │ ⚠ YOLO",
        8,
    ),
    (
        "hermes real: genuine 0 percent (not 'no value')",
        None,
        "│ 0/1M │ [░░░░░░░░░░] 0% │",
        0,
    ),
    (
        "hermes real: ctx -- (no value) must stay None",
        None,
        "│ ctx -- │ [░░░░░░░░░░] -- │",
        None,
    ),
    (
        "omp/sparky real: percent-before-slash, harness known",
        "openclaude",
        "╭── π  > ⬢ MC model · ◒ high > 📁 /workspace > ◫ 8.3%/262K ⟲ ▶───",
        8,
    ),
    (
        "omp/sparky real: percent-before-slash, harness unknown (fallback)",
        None,
        "╭── π  > ⬢ MC model · ◒ high > 📁 /workspace > ◫ 8.3%/262K ⟲ ▶───",
        8,
    ),
    ("claude: ctx NN%", "claude", "✻ ctx 12%", 12),
    ("claude: ctx: NN", "claude", "some text ctx: 45 more text", 45),
    ("kimi: context: NN%", "kimi", "context: 8% (21.3K/262.1K)", 8),
    ("unknown format entirely -> None", None, "some random shell prompt with no context info", None),
    (
        "fraction-only synthetic (no % rendered anywhere)",
        None,
        "used 21.3K/262.1K total",
        8,
    ),
    ("value >100 rejected", "claude", "ctx: 150", None),
    (
        "hermes synthetic: bar-percent isolated (no fraction present)",
        None,
        "[█░░░░░░░░░] 8% │ 12m │ ⏲ 48s",
        8,
    ),
    (
        "claude harness ignores stray percent-before-slash elsewhere in text",
        "claude",
        "ctx: 12  (unrelated 99%/300 noise)",
        12,
    ),
]
