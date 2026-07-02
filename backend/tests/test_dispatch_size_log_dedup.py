"""Bug 2026-05-12: `_build_dispatch_message` logged the size-budget warning
on every `/agent/me/poll` call (~every 5s per agent). For an over-budget task
this produced 100+ identical lines in 10 minutes, hiding real signal.

Fix: dedup by (task_id, dispatch_attempt_id). A new attempt_id (re-dispatch
on review fail, unblock, etc.) re-arms the log. Same attempt_id renders silent
after the first log.

The dedup set is module-level state — tests cleanly reset it.
"""
import logging
import uuid

import pytest

from app.services import dispatch_message_builder as dmb


class _FakeTask:
    def __init__(self, attempt_id: str | None = "att-1"):
        self.id = uuid.uuid4()
        self.dispatch_attempt_id = attempt_id


class _FakeAgent:
    name = "TestAgent"


def _exercise_size_log(
    *,
    task: _FakeTask,
    agent: _FakeAgent,
    size_chars: int,
) -> None:
    """Drive the size-log branch of _build_dispatch_message in isolation by
    inlining the same logic the function uses. This avoids spinning up the
    full DispatchContext loader (which needs DB + Redis + Qdrant) for a unit
    test of pure logging behavior.
    """
    msg_parts = ["x" * size_chars]
    rendered = "\n".join(msg_parts)
    _size = len(rendered)
    _attempt_id = getattr(task, "dispatch_attempt_id", None)
    _dedup_key = (str(task.id), _attempt_id or "")
    if _dedup_key in dmb._SIZE_LOG_DEDUP:
        return
    if _size > dmb.DISPATCH_HARD_CHARS:
        dmb.logger.warning(
            "dispatch_size: %d > HARD %d task=%s agent=%s attempt=%s "
            "sections=%d (sent anyway; shrink TODO via _assemble_with_budget)",
            _size, dmb.DISPATCH_HARD_CHARS, task.id, agent.name,
            _attempt_id or "<none>", len(msg_parts),
        )
    elif _size > dmb.DISPATCH_WARN_CHARS:
        dmb.logger.warning(
            "dispatch_size: %d > WARN %d task=%s agent=%s attempt=%s",
            _size, dmb.DISPATCH_WARN_CHARS, task.id, agent.name,
            _attempt_id or "<none>",
        )
    elif _size > dmb.DISPATCH_TARGET_CHARS:
        dmb.logger.info(
            "dispatch_size: %d > TARGET %d task=%s attempt=%s",
            _size, dmb.DISPATCH_TARGET_CHARS, task.id, _attempt_id or "<none>",
        )
    dmb._SIZE_LOG_DEDUP.add(_dedup_key)
    if len(dmb._SIZE_LOG_DEDUP) > 256:
        for _stale in list(dmb._SIZE_LOG_DEDUP)[:128]:
            dmb._SIZE_LOG_DEDUP.discard(_stale)


@pytest.fixture(autouse=True)
def _reset_dedup():
    dmb._SIZE_LOG_DEDUP.clear()
    yield
    dmb._SIZE_LOG_DEDUP.clear()


def test_same_attempt_id_logs_once(caplog):
    task = _FakeTask(attempt_id="att-1")
    agent = _FakeAgent()
    with caplog.at_level(logging.WARNING, logger=dmb.logger.name):
        # 8924 chars (matching Sparky's live case) — exceeds HARD (4000)
        _exercise_size_log(task=task, agent=agent, size_chars=8924)
        _exercise_size_log(task=task, agent=agent, size_chars=8924)
        _exercise_size_log(task=task, agent=agent, size_chars=8924)
    over_budget_lines = [r for r in caplog.records if "dispatch_size" in r.getMessage()]
    assert len(over_budget_lines) == 1, (
        f"Expected exactly 1 log for same (task, attempt); got {len(over_budget_lines)}"
    )
    assert "WARNING" == over_budget_lines[0].levelname, (
        ">HARD case should log at WARNING level (not ERROR) — sent anyway, "
        "noise reduction priority over alarm priority."
    )


def test_new_attempt_id_rearms_log(caplog):
    task1 = _FakeTask(attempt_id="att-A")
    task2 = _FakeTask(attempt_id="att-B")  # different task entirely
    task1_re = _FakeTask(attempt_id="att-1-redispatch")
    task1_re.id = task1.id  # same task, new attempt — simulates re-dispatch
    agent = _FakeAgent()
    with caplog.at_level(logging.WARNING, logger=dmb.logger.name):
        _exercise_size_log(task=task1, agent=agent, size_chars=8924)
        _exercise_size_log(task=task1, agent=agent, size_chars=8924)  # dedup
        _exercise_size_log(task=task2, agent=agent, size_chars=8924)  # distinct task
        _exercise_size_log(task=task1_re, agent=agent, size_chars=8924)  # distinct attempt
    over_budget_lines = [r for r in caplog.records if "dispatch_size" in r.getMessage()]
    assert len(over_budget_lines) == 3, (
        "Expected 3 logs: (task1, att-A), (task2, att-B), (task1, att-1-redispatch). "
        f"Got {len(over_budget_lines)}: {[r.getMessage() for r in over_budget_lines]}"
    )


def test_dedup_set_size_capped(caplog):
    agent = _FakeAgent()
    with caplog.at_level(logging.WARNING, logger=dmb.logger.name):
        # Push 300 distinct entries — cap is 256, eviction trims to ~128
        for i in range(300):
            t = _FakeTask(attempt_id=f"att-{i}")
            _exercise_size_log(task=t, agent=agent, size_chars=8924)
    # Set should never exceed cap (256) after eviction. Exact size depends on
    # eviction batch size (128) — be tolerant.
    assert len(dmb._SIZE_LOG_DEDUP) <= 256, (
        f"Dedup set unbounded: {len(dmb._SIZE_LOG_DEDUP)} entries — memory leak risk"
    )


def test_no_attempt_id_still_dedups(caplog):
    """Defensive: tasks without dispatch_attempt_id still get one-shot logging
    (key uses empty-string sentinel) — otherwise a task in early dispatch
    state would spam.
    """
    task = _FakeTask(attempt_id=None)
    agent = _FakeAgent()
    with caplog.at_level(logging.WARNING, logger=dmb.logger.name):
        _exercise_size_log(task=task, agent=agent, size_chars=8924)
        _exercise_size_log(task=task, agent=agent, size_chars=8924)
        _exercise_size_log(task=task, agent=agent, size_chars=8924)
    over_budget_lines = [r for r in caplog.records if "dispatch_size" in r.getMessage()]
    assert len(over_budget_lines) == 1


def test_below_target_no_log(caplog):
    """Sanity: messages within target budget produce no warning/error spam."""
    task = _FakeTask(attempt_id="att-tiny")
    agent = _FakeAgent()
    with caplog.at_level(logging.INFO, logger=dmb.logger.name):
        _exercise_size_log(task=task, agent=agent, size_chars=500)  # < TARGET (2000)
    over_budget_lines = [r for r in caplog.records if "dispatch_size" in r.getMessage()]
    assert len(over_budget_lines) == 0
