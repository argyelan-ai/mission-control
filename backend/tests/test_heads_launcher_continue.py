"""``launcher.render_job``'s "previous run" transcript-summary section
(bauplan `heads-sichtbar` PR 4 §5) — unit-level, no DB, no disk.
"""
from __future__ import annotations

from app.services.heads.launcher import render_job


class _FakeTask:
    def __init__(self, title: str, description: str | None = None) -> None:
        self.title = title
        self.description = description


_BASE_PREVIOUS = {
    "pair": "omp × box-slot",
    "state": "passed",
    "reason": None,
    "branch": "mc-head/2026-10-04-fix-ab12",
    "base": "main",
    "run_record": None,
    "question": None,
}

HEADING = "### What the previous run did (transcript summary)"


def test_render_job_includes_the_summary_section_when_present():
    task = _FakeTask("Fix the thing")
    job = render_job(task, answer=None, previous={**_BASE_PREVIOUS, "transcript_summary": "- Tools used: 3\n"})
    assert HEADING in job
    assert "- Tools used: 3" in job
    # Appears AFTER the run-record section and BEFORE the open question —
    # same ordering the bauplan table lists ("Run record so far" → "What
    # the previous run did" → "Open question").
    assert job.index(HEADING) > job.index("## Previous run")


def test_render_job_omits_the_section_when_transcript_summary_is_none():
    task = _FakeTask("Fix the thing")
    job = render_job(task, answer=None, previous={**_BASE_PREVIOUS, "transcript_summary": None})
    assert HEADING not in job


def test_render_job_omits_the_section_when_the_key_is_missing_entirely():
    task = _FakeTask("Fix the thing")
    job = render_job(task, answer=None, previous=dict(_BASE_PREVIOUS))
    assert HEADING not in job


def test_render_job_omits_the_section_when_transcript_summary_is_an_empty_string():
    """`transcript.summarize()`'s own "nothing to say" return value — must
    read exactly like "not set", never render an empty heading."""
    task = _FakeTask("Fix the thing")
    job = render_job(task, answer=None, previous={**_BASE_PREVIOUS, "transcript_summary": ""})
    assert HEADING not in job


def test_render_job_with_no_previous_run_at_all_has_neither_block():
    task = _FakeTask("Fix the thing")
    job = render_job(task, answer=None, previous=None)
    assert "## Previous run" not in job
    assert HEADING not in job
