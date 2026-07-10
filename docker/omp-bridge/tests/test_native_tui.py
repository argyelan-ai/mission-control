#!/usr/bin/env python3
"""ADR-049 — tests for the NATIVE-TUI driver (bridge.py run_native_turn +
NativeTuiController).

Where test_bridge.py proves the NDJSON reducer/classifier and test_serve_loop.py
proves the poll loop, this file proves the piece that replaces the headless
one-shot: injecting a task into the persistent native omp TUI and folding the
turn-end HOOK SIGNAL into the SAME classify()/decide_lifecycle() taxonomy.

It uses the REAL NativeTuiController with:
  * a fake `_run` (records tmux argv, no tmux server),
  * a real temp signal file fed on a virtual clock via the `sleep` seam,
so the drain/offset/parse logic and the watchdog are exercised for real.

Covers:
  - stop + valid reflection            -> FINISH
  - stop without sentinel              -> SILENT_ABORT_NO_SENTINEL (blocker)
  - toolUse turns then stop            -> FINISH (agentic loop, not premature)
  - error / aborted turn               -> error family (blocker)
  - length then agent_end (truncated)  -> incomplete -> blocker
  - per-task deadline with no terminal -> watchdog -> ABORT_HANG + relaunch
  - TUI child death                    -> watchdog -> ABORT_HANG + relaunch
  - inject uses `@file` + separate Enter; relaunch rebinds --cwd (isolation)
  - drain: partial trailing line held back; truncate resets offset
  - full mapping through decide_lifecycle: FINISH->finish, aborts->blocker

Run:  python3 test_native_tui.py     (standalone)   OR   pytest -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # import bridge.py

import bridge  # noqa: E402
from bridge import Kind  # noqa: E402

REFLECTION = (
    "## Was wurde gemacht\nDatei erstellt und getestet, alles laeuft sauber.\n"
    "## Was hat funktioniert\nDer deterministische Fix, zweiter Lauf war gruen.\n"
    "## Was war unklar\nNichts Wesentliches, die Aufgabe war eindeutig genug.\n"
    "## Lesson fuer Agent-Memory\nErst reproduzieren, dann fixen, dann verifizieren.\n"
    "TASK_COMPLETE"
)


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, dt: float) -> None:
        self.t += dt or 0.001


def _te(stop_reason, *, text="", err=None, tool_error=False, idx=0):
    return {
        "kind": "turn_end", "turnIndex": idx, "stopReason": stop_reason,
        "errorMessage": err, "errorStatus": None, "toolError": tool_error, "text": text,
    }


class _Harness:
    """Real NativeTuiController + a fake tmux + a timeline of hook-record batches
    that get appended to the real signal file, one batch per sleep tick."""

    def __init__(self, batches, *, alive=True, alive_after=None):
        self.tmp = tempfile.mkdtemp(prefix="omp-native-")
        self.sig = os.path.join(self.tmp, "turn-signal.ndjson")
        open(self.sig, "w", encoding="utf-8").close()
        self.run_log: list[list[str]] = []
        self.batches = list(batches)
        self.ticks = 0
        # child liveness: `alive` initially, flips to `alive_after` after ready.
        self._alive = alive
        self._alive_after = alive_after
        self._drains_seen = 0
        self.clock = _Clock()

        def fake_run(args):
            self.run_log.append(args)
            if args and args[0] == "list-panes":
                return 0, "4242\n"
            return 0, ""

        def pid_alive(_pid):
            return self._alive

        self.ctrl = bridge.NativeTuiController(
            session="sparky", signal_file=self.sig, _run=fake_run, _pid_alive=pid_alive,
            _sleep=lambda _s: None,  # no real key delays in tests
        )

    def sleep(self, dt):
        self.clock.sleep(dt)
        self.ticks += 1
        if self.batches:
            batch = self.batches.pop(0)
            with open(self.sig, "a", encoding="utf-8") as fh:
                for rec in batch:
                    fh.write(json.dumps(rec) + "\n")
            # After the readiness batch is delivered, optionally flip liveness.
            if self._alive_after is not None and any(
                r.get("kind") in ("session_start", "hook_ready") for r in batch
            ):
                self._alive = self._alive_after

    def run(self, **kw):
        defaults = dict(
            cwd="/workspace/proj", prompt="Do the thing.\n" + REFLECTION,
            task_file_path=os.path.join(self.tmp, "task-1.md"), isolate=True,
            ready_timeout=1000, turn_deadline=1000, idle_timeout=1000,
            poll_interval=1.0, now=self.clock.now, sleep=self.sleep,
        )
        defaults.update(kw)
        return bridge.run_native_turn(self.ctrl, **defaults)

    def cmds(self, verb):
        return [a for a in self.run_log if a and a[0] == verb]


# ---------------------------------------------------------------------------
# run_native_turn -> RunOutcome (the hook-signal -> outcome mapping)
# ---------------------------------------------------------------------------

def test_stop_with_valid_reflection_is_finish():
    h = _Harness([
        [{"kind": "session_start"}],
        [_te("toolUse", idx=0)],
        [_te("stop", text=REFLECTION, idx=1)],
    ])
    outcome = h.run()
    assert outcome.final_stop_reason == "stop"
    assert outcome.saw_session and outcome.saw_agent_end
    cls = bridge.classify(outcome)
    assert cls.kind is Kind.FINISH, cls
    action = bridge.decide_lifecycle(cls, board_requires_review=True, retries_left=0)
    assert action.action == "finish"


def test_stop_without_sentinel_is_silent_abort_blocker():
    h = _Harness([[{"kind": "session_start"}], [_te("stop", text="Done.")]])
    outcome = h.run()
    cls = bridge.classify(outcome)
    assert cls.kind is Kind.SILENT_ABORT_NO_SENTINEL, cls
    action = bridge.decide_lifecycle(cls, board_requires_review=True, retries_left=0)
    assert action.action == "blocker"


def test_tooluse_turns_then_stop_is_finish_not_premature():
    # Several agentic toolUse turns MUST NOT be read as terminal; only the final
    # stop turn decides. Proves the loop waits through the agentic loop.
    h = _Harness([
        [{"kind": "session_start"}],
        [_te("toolUse", idx=0)],
        [{"kind": "progress", "at": "tool_execution_end"}],
        [_te("toolUse", idx=1)],
        [_te("stop", text=REFLECTION, idx=2)],
    ])
    outcome = h.run()
    assert outcome.turns == 3
    assert bridge.classify(outcome).kind is Kind.FINISH


def test_error_turn_is_error_family_blocker():
    h = _Harness([
        [{"kind": "session_start"}],
        [_te("error", err="Unable to connect. Is the computer able to access the url?")],
    ])
    outcome = h.run()
    assert outcome.final_stop_reason == "error"
    cls = bridge.classify(outcome)
    assert cls.kind in (Kind.ABORT_ERROR, Kind.ABORT_TRANSIENT_API), cls
    # transient network wording -> transient family
    assert cls.kind is Kind.ABORT_TRANSIENT_API
    action = bridge.decide_lifecycle(cls, board_requires_review=True, retries_left=0)
    assert action.action == "blocker"


def test_aborted_turn_maps_to_error_blocker():
    h = _Harness([[{"kind": "session_start"}], [_te("aborted")]])
    outcome = h.run()
    assert outcome.final_stop_reason == "error"
    assert outcome.error_message and "aborted" in outcome.error_message.lower()
    assert bridge.decide_lifecycle(
        bridge.classify(outcome), board_requires_review=True, retries_left=0
    ).action == "blocker"


def test_length_then_agent_end_is_incomplete_blocker():
    # Context truncated: a `length` turn (agent may auto-compact) followed by
    # agent_end with no clean stop -> incomplete -> blocker, never finish.
    h = _Harness([
        [{"kind": "session_start"}],
        [_te("length")],
        [{"kind": "agent_end"}],
    ])
    outcome = h.run()
    assert outcome.saw_agent_end
    assert outcome.final_stop_reason == "error"
    cls = bridge.classify(outcome)
    assert cls.kind is not Kind.FINISH
    assert bridge.decide_lifecycle(cls, board_requires_review=True, retries_left=0).action == "blocker"


# ---------------------------------------------------------------------------
# Watchdog (non-negotiable): never left in_progress; SIGKILL + relaunch the TUI
# ---------------------------------------------------------------------------

def test_deadline_watchdog_kills_and_relaunches():
    # Session comes up, then NOTHING terminal ever arrives -> the per-task
    # deadline must fire -> watchdog_killed + a relaunch (SIGKILL via respawn -k).
    h = _Harness([[{"kind": "session_start"}]])  # only readiness, no terminal turn
    outcome = h.run(turn_deadline=5, idle_timeout=1000, ready_timeout=100)
    assert outcome.watchdog_killed is True
    cls = bridge.classify(outcome)
    assert cls.kind is Kind.ABORT_HANG
    # relaunch happened at least twice: initial isolate + watchdog recovery.
    assert len(h.cmds("respawn-window")) >= 2
    assert bridge.decide_lifecycle(cls, board_requires_review=True, retries_left=0).action == "blocker"


def test_idle_watchdog_fires_on_no_progress():
    h = _Harness([[{"kind": "session_start"}]])
    outcome = h.run(turn_deadline=1000, idle_timeout=3, ready_timeout=100)
    assert outcome.watchdog_killed is True
    assert bridge.classify(outcome).kind is Kind.ABORT_HANG


def test_child_death_watchdog_fires_and_relaunches():
    # TUI child dies right after coming ready -> immediate watchdog + relaunch.
    h = _Harness([[{"kind": "session_start"}]], alive=True, alive_after=False)
    outcome = h.run(turn_deadline=1000, idle_timeout=1000, ready_timeout=100)
    assert outcome.watchdog_killed is True
    assert bridge.classify(outcome).kind is Kind.ABORT_HANG
    assert len(h.cmds("respawn-window")) >= 2


def test_tui_never_ready_is_watchdog_not_silent():
    # No session_start EVER and the child is dead -> a hang the supervisor must
    # recover from, never a silent in_progress.
    h = _Harness([], alive=False)
    outcome = h.run(ready_timeout=3, turn_deadline=1000, idle_timeout=1000)
    assert outcome.watchdog_killed is True
    assert bridge.classify(outcome).kind is Kind.ABORT_HANG


# ---------------------------------------------------------------------------
# Injection + isolation mechanics (the send-keys / respawn contract)
# ---------------------------------------------------------------------------

def test_inject_uses_at_file_escape_then_enter():
    h = _Harness([[{"kind": "session_start"}], [_te("stop", text=REFLECTION)]])
    tf = os.path.join(h.tmp, "task-xyz.md")
    h.run(task_file_path=tf)
    sends = h.cmds("send-keys")
    # the @file mention, then Escape (dismiss popup), then Enter (submit) — the
    # proven order (a bare Enter is eaten by the file-mention autocomplete).
    assert ["send-keys", "-t", "sparky:0", "--", f"@{tf}"] in sends
    assert ["send-keys", "-t", "sparky:0", "Escape"] in sends
    assert ["send-keys", "-t", "sparky:0", "Enter"] in sends
    order = [s[-1] for s in sends]
    assert order.index(f"@{tf}") < order.index("Escape") < order.index("Enter")
    # the wrapped prompt was written to the file, not typed.
    assert os.path.exists(tf)
    with open(tf, encoding="utf-8") as fh:
        assert "TASK_COMPLETE" in fh.read()


def test_relaunch_rebinds_cwd_for_isolation():
    h = _Harness([[{"kind": "session_start"}], [_te("stop", text=REFLECTION)]])
    h.run(cwd="/workspace/proj/.worktrees/task-9")
    respawns = h.cmds("respawn-window")
    assert respawns, "isolate=True must relaunch Window 0"
    joined = " ".join(respawns[0])
    assert "-k" in joined and "sparky:0" in joined
    assert "/workspace/proj/.worktrees/task-9" in joined
    assert "launch-omp.sh" in joined


def test_isolate_false_does_not_relaunch():
    h = _Harness([[{"kind": "session_start"}], [_te("stop", text=REFLECTION)]])
    h.run(isolate=False)
    assert h.cmds("respawn-window") == []  # slash-isolation path: no relaunch


# ---------------------------------------------------------------------------
# NativeTuiController drain/offset/truncate primitives
# ---------------------------------------------------------------------------

def test_drain_holds_back_partial_trailing_line():
    tmp = tempfile.mkdtemp(prefix="omp-drain-")
    sig = os.path.join(tmp, "s.ndjson")
    ctrl = bridge.NativeTuiController(session="s", signal_file=sig, _run=lambda a: (0, ""))
    with open(sig, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": "turn_end", "stopReason": "stop"}) + "\n")
        fh.write('{"kind":"partial"')  # no newline -> incomplete
    recs = ctrl.drain()
    assert len(recs) == 1 and recs[0]["stopReason"] == "stop"
    with open(sig, "a", encoding="utf-8") as fh:
        fh.write(',"stopReason":"error"}\n')  # complete the partial line
    recs2 = ctrl.drain()
    assert len(recs2) == 1 and recs2[0]["kind"] == "partial"


def test_truncate_resets_offset():
    tmp = tempfile.mkdtemp(prefix="omp-trunc-")
    sig = os.path.join(tmp, "s.ndjson")
    ctrl = bridge.NativeTuiController(session="s", signal_file=sig, _run=lambda a: (0, ""))
    with open(sig, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": "session_start"}) + "\n")
    ctrl.drain()
    ctrl.truncate_signal()
    assert ctrl._offset == 0
    with open(sig, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": "turn_end", "stopReason": "stop"}) + "\n")
    recs = ctrl.drain()
    assert len(recs) == 1 and recs[0]["kind"] == "turn_end"


def test_child_alive_reads_pane_pid():
    calls = []

    def fake_run(args):
        calls.append(args)
        if args[0] == "list-panes":
            return 0, "777\n"
        return 0, ""

    seen = {}

    def pid_alive(pid):
        seen["pid"] = pid
        return True

    ctrl = bridge.NativeTuiController(
        session="s", signal_file="/dev/null", _run=fake_run, _pid_alive=pid_alive,
    )
    assert ctrl.child_alive() is True
    assert seen["pid"] == 777


# ---------------------------------------------------------------------------
# End-to-end through drive_live_run (the UNCHANGED retry-then-blocker policy)
# ---------------------------------------------------------------------------

class _Recording(bridge.MCLifecycle):
    def __init__(self):
        self.calls = []

    def ack(self, task_id):
        self.calls.append(("ack", task_id))

    def finish(self, task_id, reflection, *, review):
        self.calls.append(("finish", task_id, review))

    def set_blocker(self, task_id, *, blocker_type, question):
        self.calls.append(("blocker", task_id, blocker_type))

    def comment(self, task_id, text):
        self.calls.append(("comment", task_id, text))


def test_native_finish_flows_through_drive_live_run():
    h = _Harness([
        [{"kind": "session_start"}],
        [_te("toolUse")],
        [_te("stop", text=REFLECTION)],
    ])
    lc = _Recording()
    action = bridge.drive_live_run(
        lc, lambda: h.run(), task_id="T1",
        board_requires_review=True, retries_left=0, pre_acked=True,
    )
    assert action.action == "finish"
    kinds = [c[0] for c in lc.calls]
    assert "finish" in kinds and "blocker" not in kinds


def test_native_hang_retries_then_blocks_terminally():
    # Each attempt hangs (deadline) -> retryable ABORT_HANG. With a budget it
    # retries (re-inject), and once exhausted resolves as a terminal blocker —
    # never left in_progress.
    attempts = {"n": 0}

    def run_once():
        attempts["n"] += 1
        h = _Harness([[{"kind": "session_start"}]])  # no terminal turn -> hang
        return h.run(turn_deadline=3, idle_timeout=1000, ready_timeout=50)

    lc = _Recording()
    action = bridge.drive_live_run(
        lc, run_once, task_id="T1",
        board_requires_review=True, retries_left=1, pre_acked=True,
    )
    assert action.action == "blocker"
    assert attempts["n"] == 2  # initial + one retry
    kinds = [c[0] for c in lc.calls]
    # one retry note + the Blocker-Qualität classification comment (Fix B §):
    # the bridge posts its OWN cause before blocking.
    assert kinds.count("comment") == 2
    classifications = [c[2] for c in lc.calls if c[0] == "comment"]
    assert any("omp-bridge Klassifikation" in t for t in classifications)
    assert "blocker" in kinds and "finish" not in kinds


# ---------------------------------------------------------------------------
# Fix B — Continue-Nudge (§4): the harmless self-completion aborts heal via a
# follow-up prompt in the SAME session instead of an immediate Blocker.
# ---------------------------------------------------------------------------

def _stop_outcome(text: str) -> "bridge.RunOutcome":
    o = bridge.RunOutcome()
    o.saw_session = True
    o.saw_agent_start = True
    o.saw_agent_end = True
    o.final_stop_reason = "stop"
    o.final_text = text
    return o


def _finish_outcome() -> "bridge.RunOutcome":
    return _stop_outcome(REFLECTION)                 # valid sentinel + reflection


def _silent_abort_outcome() -> "bridge.RunOutcome":
    return _stop_outcome("Done.")                    # stop, but NO sentinel


def _trailing_tool_err_outcome() -> "bridge.RunOutcome":
    o = _stop_outcome(REFLECTION)                     # sentinel + reflection OK,
    o.last_turn_had_tool_error = True                # but the last tool errored
    return o


def _crash_outcome() -> "bridge.RunOutcome":
    o = bridge.RunOutcome()
    o.saw_session = True
    o.saw_agent_start = True
    o.saw_agent_end = False                           # no agent_end -> ABORT_CRASH (retryable)
    return o


# -- decide_lifecycle policy (pure) -----------------------------------------

def test_decide_continue_on_silent_abort_with_budget():
    # (a) sentinel-loser stop -> action=continue with the correct nudge prompt.
    cls = bridge.classify(_silent_abort_outcome())
    assert cls.kind is Kind.SILENT_ABORT_NO_SENTINEL
    action = bridge.decide_lifecycle(
        cls, board_requires_review=True, retries_left=0, continues_left=2,
    )
    assert action.action == "continue"
    assert action.nudge_prompt == bridge.CONTINUE_NUDGE_PROMPTS[Kind.SILENT_ABORT_NO_SENTINEL]
    assert "TASK_COMPLETE" in action.nudge_prompt


def test_decide_continue_on_trailing_tool_error():
    # (e) TRAILING_TOOL_ERROR is continueable, with its own nudge prompt.
    cls = bridge.classify(_trailing_tool_err_outcome())
    assert cls.kind is Kind.TRAILING_TOOL_ERROR
    action = bridge.decide_lifecycle(
        cls, board_requires_review=True, retries_left=0, continues_left=1,
    )
    assert action.action == "continue"
    assert action.nudge_prompt == bridge.CONTINUE_NUDGE_PROMPTS[Kind.TRAILING_TOOL_ERROR]


def test_decide_continue_on_malformed_reflection():
    o = _stop_outcome("TASK_COMPLETE")               # sentinel, but reflection too short
    cls = bridge.classify(o)
    assert cls.kind is Kind.MALFORMED_REFLECTION
    action = bridge.decide_lifecycle(
        cls, board_requires_review=True, retries_left=0, continues_left=2,
    )
    assert action.action == "continue"
    assert action.nudge_prompt == bridge.CONTINUE_NUDGE_PROMPTS[Kind.MALFORMED_REFLECTION]


def test_decide_finish_stays_finish_even_with_continue_budget():
    # (c) FINISH is FINISH regardless of any budget.
    cls = bridge.classify(_finish_outcome())
    assert cls.kind is Kind.FINISH
    action = bridge.decide_lifecycle(
        cls, board_requires_review=True, retries_left=2, continues_left=2,
    )
    assert action.action == "finish"


def test_decide_continueable_without_budget_still_blocks():
    # Backward-compat: continues_left defaults to 0 -> the old straight-to-blocker.
    cls = bridge.classify(_silent_abort_outcome())
    action = bridge.decide_lifecycle(cls, board_requires_review=True, retries_left=0)
    assert action.action == "blocker"


# -- drive_live_run integration ---------------------------------------------

def test_continue_budget_exhausts_to_blocker():
    # (b) every turn stays sentinel-less -> nudged exactly `continues_left` times,
    # then a terminal blocker (never left in_progress).
    nudges: list[str] = []

    def continue_once(nudge):
        nudges.append(nudge)
        return _silent_abort_outcome()               # the nudge did not help

    lc = _Recording()
    action = bridge.drive_live_run(
        lc, _silent_abort_outcome, task_id="T1",
        board_requires_review=True, retries_left=0,
        continues_left=2, continue_once=continue_once, pre_acked=True,
    )
    assert action.action == "blocker"
    assert len(nudges) == 2                            # exactly the continue budget
    # Fix 1: the 1st nudge for a Kind stays the normal prompt; a 2nd nudge for
    # the SAME Kind (no progress) escalates to the minimal copy-paste template.
    assert nudges[0] == bridge.CONTINUE_NUDGE_PROMPTS[Kind.SILENT_ABORT_NO_SENTINEL]
    assert "Format falsch" in nudges[1]
    kinds = [c[0] for c in lc.calls]
    assert kinds.count("comment") == 3                # 2 nudge notes + 1 classification
    assert "blocker" in kinds and "finish" not in kinds


def test_continue_nudge_recovers_to_finish():
    # A single nudge lands the sentinel -> FINISH (proves the nudge is EXECUTED,
    # not just decided).
    def continue_once(nudge):
        return _finish_outcome()

    lc = _Recording()
    action = bridge.drive_live_run(
        lc, _silent_abort_outcome, task_id="T1",
        board_requires_review=True, retries_left=0,
        continues_left=2, continue_once=continue_once, pre_acked=True,
    )
    assert action.action == "finish"
    kinds = [c[0] for c in lc.calls]
    assert kinds.count("comment") == 1                # one nudge note, then finish
    assert "finish" in kinds and "blocker" not in kinds


def test_retry_and_continue_budgets_are_independent():
    # (d) a crash consumes a RETRY (fresh re-run); the follow-up silent-abort then
    # consumes the CONTINUE budget — two separate counters, both spent before block.
    outcomes = iter([_crash_outcome(), _silent_abort_outcome()])

    def run_once():
        return next(outcomes)

    cont = {"n": 0}

    def continue_once(nudge):
        cont["n"] += 1
        return _silent_abort_outcome()               # keep failing the continue path

    lc = _Recording()
    action = bridge.drive_live_run(
        lc, run_once, task_id="T1",
        board_requires_review=True, retries_left=1,
        continues_left=2, continue_once=continue_once, pre_acked=True,
    )
    assert action.action == "blocker"
    assert cont["n"] == 2                              # both continues spent AFTER the retry
    kinds = [c[0] for c in lc.calls]
    # 1 retry note + 2 nudge notes + 1 classification comment.
    assert kinds.count("comment") == 4


def test_continue_without_executor_collapses_to_blocker():
    # continues_left>0 but no continue_once wired -> must NOT strand as 'continue';
    # collapses to a terminal blocker (mirrors the retry-without-executor guard).
    lc = _Recording()
    action = bridge.drive_live_run(
        lc, _silent_abort_outcome, task_id="T1",
        board_requires_review=True, retries_left=0,
        continues_left=2, continue_once=None, pre_acked=True,
    )
    assert action.action == "blocker"


# ---------------------------------------------------------------------------
# Fix 1 — escalating, simpler second nudge: the 2nd+ nudge for the SAME Kind
# (i.e. the first nudge did not fix it) switches to a maximally minimal,
# copy-paste template instead of repeating the identical prose a weak model
# already misread once.
# ---------------------------------------------------------------------------

def test_continue_nudge_escalates_on_second_same_kind():
    nudges: list[str] = []

    def continue_once(nudge):
        nudges.append(nudge)
        return _silent_abort_outcome()          # same Kind every time -> no progress

    lc = _Recording()
    action = bridge.drive_live_run(
        lc, _silent_abort_outcome, task_id="T1",
        board_requires_review=True, retries_left=0,
        continues_left=2, continue_once=continue_once, pre_acked=True,
    )
    assert action.action == "blocker"
    assert len(nudges) == 2
    assert nudges[0] == bridge.CONTINUE_NUDGE_PROMPTS[Kind.SILENT_ABORT_NO_SENTINEL]
    assert nudges[1] != nudges[0]
    assert "Format falsch" in nudges[1]
    assert "TASK_COMPLETE" in nudges[1]
    for header in bridge.REFLECTION_HEADERS:
        assert header in nudges[1]


def test_continue_nudge_first_nudge_unchanged():
    # A single nudge (budget of 1) never escalates — it's Kind's FIRST nudge.
    nudges: list[str] = []

    def continue_once(nudge):
        nudges.append(nudge)
        return _silent_abort_outcome()

    lc = _Recording()
    bridge.drive_live_run(
        lc, _silent_abort_outcome, task_id="T1",
        board_requires_review=True, retries_left=0,
        continues_left=1, continue_once=continue_once, pre_acked=True,
    )
    assert len(nudges) == 1
    assert nudges[0] == bridge.CONTINUE_NUDGE_PROMPTS[Kind.SILENT_ABORT_NO_SENTINEL]


def test_continue_nudge_different_kind_sequence_does_not_escalate_wrongly():
    # A 2nd OVERALL nudge for a DIFFERENT Kind is still that Kind's first nudge
    # -> escalation is tracked per-Kind, not as a global nudge counter.
    nudges: list[str] = []

    def continue_once(nudge):
        nudges.append(nudge)
        if len(nudges) == 1:
            return _trailing_tool_err_outcome()  # switches Kind
        return _finish_outcome()

    lc = _Recording()
    action = bridge.drive_live_run(
        lc, _silent_abort_outcome, task_id="T1",
        board_requires_review=True, retries_left=0,
        continues_left=2, continue_once=continue_once, pre_acked=True,
    )
    assert action.action == "finish"
    assert len(nudges) == 2
    assert nudges[0] == bridge.CONTINUE_NUDGE_PROMPTS[Kind.SILENT_ABORT_NO_SENTINEL]
    assert nudges[1] == bridge.CONTINUE_NUDGE_PROMPTS[Kind.TRAILING_TOOL_ERROR]


# ---------------------------------------------------------------------------
# Fix 2 — partial-reflection salvage on budget exhaustion: a MALFORMED_REFLECTION
# collapse to blocker posts the agent's near-complete reflection as its own
# progress comment BEFORE the blocker, instead of discarding it.
# ---------------------------------------------------------------------------

def _malformed_partial_outcome() -> "bridge.RunOutcome":
    # 3 of 4 canonical headers ("Lesson fuer Agent-Memory" missing) + >=80 chars.
    text = (
        "## Was wurde gemacht\nDatei erstellt und ausfuehrlich getestet, lief sauber.\n"
        "## Was hat funktioniert\nDer deterministische Fix, zweiter Lauf war komplett gruen.\n"
        "## Was war unklar\nNichts Wesentliches, die Aufgabe war eindeutig genug beschrieben.\n"
        "TASK_COMPLETE"
    )
    return _stop_outcome(text)


def _malformed_garbage_outcome() -> "bridge.RunOutcome":
    # Sentinel present, but 0 recognised headers.
    return _stop_outcome("Fertig, denke ich. Kein Format befolgt.\nTASK_COMPLETE")


class _SalvageFailsRecording(_Recording):
    def comment(self, task_id, text):
        if text.startswith("Partielle Reflexion"):
            raise RuntimeError("comment backend down")
        super().comment(task_id, text)


def test_budget_exhaustion_salvages_partial_reflection():
    lc = _Recording()
    action = bridge.drive_live_run(
        lc, _malformed_partial_outcome, task_id="T1",
        board_requires_review=True, retries_left=0,
        continues_left=0, pre_acked=True,
    )
    assert action.action == "blocker"
    comment_calls = [(i, c[2]) for i, c in enumerate(lc.calls) if c[0] == "comment"]
    salvage = [
        (i, t) for i, t in comment_calls
        if t.startswith("Partielle Reflexion (auto-gerettet vor Blocker):")
    ]
    assert len(salvage) == 1
    assert "## Was wurde gemacht" in salvage[0][1]
    blocker_idx = next(i for i, c in enumerate(lc.calls) if c[0] == "blocker")
    assert salvage[0][0] < blocker_idx           # salvage lands BEFORE the blocker


def test_budget_exhaustion_no_salvage_for_garbage_output():
    lc = _Recording()
    action = bridge.drive_live_run(
        lc, _malformed_garbage_outcome, task_id="T1",
        board_requires_review=True, retries_left=0,
        continues_left=0, pre_acked=True,
    )
    assert action.action == "blocker"
    comments = [c[2] for c in lc.calls if c[0] == "comment"]
    assert not any(t.startswith("Partielle Reflexion") for t in comments)


def test_salvage_comment_failure_still_blocks():
    lc = _SalvageFailsRecording()
    action = bridge.drive_live_run(
        lc, _malformed_partial_outcome, task_id="T1",
        board_requires_review=True, retries_left=0,
        continues_left=0, pre_acked=True,
    )
    assert action.action == "blocker"
    kinds = [c[0] for c in lc.calls]
    assert "blocker" in kinds


# -- run_native_continue mechanics (no relaunch, keep context) ---------------

def test_run_native_continue_no_relaunch_injects_nudge():
    h = _Harness([[_te("stop", text=REFLECTION)]])    # terminal turn on the first tick
    tf = os.path.join(h.tmp, "task-cont.md")
    outcome = bridge.run_native_continue(
        h.ctrl, cwd="/workspace/proj", nudge_prompt="NUDGE-BODY\n" + REFLECTION,
        task_file_path=tf, turn_deadline=1000, idle_timeout=1000,
        poll_interval=1.0, now=h.clock.now, sleep=h.sleep,
    )
    assert outcome.saw_session is True                # claimed up front (no session_start)
    assert outcome.final_stop_reason == "stop"
    assert h.cmds("respawn-window") == []             # NO relaunch — context preserved
    sends = h.cmds("send-keys")
    assert ["send-keys", "-t", "sparky:0", "--", f"@{tf}"] in sends
    with open(tf, encoding="utf-8") as fh:
        body = fh.read()
    assert "NUDGE-BODY" in body


def test_run_native_continue_dead_child_is_watchdog_not_silent():
    # The TUI child died between turns -> a continue must recover (watchdog), never
    # inject into a corpse and hang.
    h = _Harness([[_te("stop", text=REFLECTION)]], alive=False)
    outcome = bridge.run_native_continue(
        h.ctrl, cwd="/workspace/proj", nudge_prompt="NUDGE\n" + REFLECTION,
        task_file_path=os.path.join(h.tmp, "t.md"),
        turn_deadline=1000, idle_timeout=1000, poll_interval=1.0,
        now=h.clock.now, sleep=h.sleep,
    )
    assert outcome.watchdog_killed is True
    assert bridge.classify(outcome).kind is Kind.ABORT_HANG
    assert h.cmds("respawn-window")                   # relaunched to recover


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

def _run_standalone() -> int:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = failed = 0
    print("=" * 70)
    print("omp-bridge NATIVE-TUI TEST (standalone runner)")
    print("=" * 70)
    for t in tests:
        try:
            t()
            passed += 1
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print("-" * 70)
    print(f"  {passed} passed, {failed} failed")
    print("=" * 70)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
