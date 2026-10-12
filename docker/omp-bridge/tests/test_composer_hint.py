"""omp 18.10.1 composer placeholder hint vs. the bridge's submit check.

Since omp 18.x (verified on 18.10.1, absent in 18.1.10) an EMPTY composer in
a fresh session shows a right-aligned, dim placeholder hint on its bottom
border line, e.g.

    ╰─                                         ⇧⇥ to change thinking effort

(omp `packages/tui/src/prompt/composer-hints.ts`: two hints, "effort" before
the conversation starts and "agents" while subagents run; each retires after
three uses of its gesture). `capture-pane -p` drops the dim styling, so the
hint reads like typed text. `_composer_state` treated it as an unrecognised
draft -> "pending" -> the injector re-pastes the task mention although the
first paste was submitted (double task injection).

The composer lines below are verbatim captures from omp 18.10.1 (throwaway
container, 160 columns, 12.10.2026); the "agents" variant is synthetic, built
from the same source-defined label.
"""
from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # import bridge.py

import bridge  # noqa: E402

STATUS_18101 = (
    " π > ◒ MC model > 📁 /workspace ▶───────5%──────────────────────────────"
    "────────────────────────────────────────────────────╎────────────┃────"
    "─────────────131K─"
)
EMPTY_WITH_EFFORT_HINT = (
    "╰─" + " " * 130 + "⇧⇥ to change thinking effort"
)
EMPTY_WITH_AGENTS_HINT = "╰─" + " " * 128 + "←← to see 2 running agents"
EMPTY_WITH_TEXT_KEY_HINT = "╰─" + " " * 120 + "Shift+Tab to change thinking effort"
EMPTY_18101 = "╰─"
DRAFT_18101 = "╰─ noch nicht abgeschickt"


def _controller(pane: str):
    def run(args):
        if args[0] == "capture-pane":
            return 0, pane
        return 0, ""

    tmp = tempfile.mkdtemp(prefix="omp-hint-")
    return bridge.NativeTuiController(
        session="probe", signal_file=os.path.join(tmp, "sig"), _run=run,
        _pid_alive=lambda _pid: True, _sleep=lambda _dt: None,
    )


def _pane(bottom: str) -> str:
    return f"{STATUS_18101}\n{bottom}\n"


def test_effort_hint_in_empty_composer_reads_as_submitted():
    ctrl = _controller(_pane(EMPTY_WITH_EFFORT_HINT))
    assert ctrl._composer_state("task.md", 1, 0.0) == "submitted"


def test_agents_hint_in_empty_composer_reads_as_submitted():
    ctrl = _controller(_pane(EMPTY_WITH_AGENTS_HINT))
    assert ctrl._composer_state("task.md", 1, 0.0) == "submitted"


def test_hint_with_a_spelled_out_key_reads_as_submitted():
    ctrl = _controller(_pane(EMPTY_WITH_TEXT_KEY_HINT))
    assert ctrl._composer_state("task.md", 1, 0.0) == "submitted"


def test_plain_empty_composer_18101_reads_as_submitted():
    ctrl = _controller(_pane(EMPTY_18101))
    assert ctrl._composer_state("task.md", 1, 0.0) == "submitted"


def test_pending_mention_still_reads_as_pending():
    ctrl = _controller(_pane("╰─ @/home/agent/.omp/tasks/task.md"))
    assert ctrl._composer_state("task.md", 1, 0.0) == "pending"


def test_unrecognised_draft_still_reads_as_pending():
    ctrl = _controller(_pane(DRAFT_18101))
    assert ctrl._composer_state("task.md", 1, 0.0) == "pending"


def test_typed_text_that_merely_ends_like_the_hint_stays_pending():
    """A draft right after the border (one space, not right-aligned) that
    happens to end with the hint words is still a draft."""
    ctrl = _controller(_pane("╰─ please x to change thinking effort"))
    assert ctrl._composer_state("task.md", 1, 0.0) == "pending"
