#!/usr/bin/env python3
"""A correction re-dispatch is a task, not a nudge (fix 19.10.2026).

The bridge guessed the origin of a task-session prompt from its text:
``"task" if "# New Task:" in prompt else "nudge"``. A re-dispatch after a
review "Change" starts with ``# CORRECTION NEEDED: <title>``
(``dispatch_message_builder``) and therefore showed up in the chat as an
untitled nudge — the operator could not see that a correction run started.

Now the CALL SITE says what it sends (dispatch/retry = task, continue = nudge)
and the title comes from either header, with ``redispatch`` marking the
correction.

  pytest docker/omp-bridge/tests/test_task_prompt_origin.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import acp_chat_events  # noqa: E402
import bridge  # noqa: E402
from test_acp_model_freshness import TaskClient, session_result  # noqa: E402

CARD = "# Operating Card — Agent\n\nRole line\n\n---\n\n"
NEW = CARD + "# New Task: Build the page\n\nDo it.\n"
CORRECTION = CARD + "# CORRECTION NEEDED: Build the page\n\nReviewer says: fix X.\n"


def test_origin_of_a_new_task():
    assert bridge.task_prompt_origin(NEW, "task") == {
        "kind": "task", "title": "Build the page", "redispatch": False,
    }


def test_origin_of_a_correction_is_a_task_with_its_title():
    assert bridge.task_prompt_origin(CORRECTION, "task") == {
        "kind": "task", "title": "Build the page", "redispatch": True,
    }


def test_a_nudge_stays_a_nudge_even_if_it_quotes_a_header():
    assert bridge.task_prompt_origin("Continue. " + NEW, "nudge") == {"kind": "nudge"}


def test_without_a_call_site_the_text_still_decides_both_headers():
    assert bridge.task_prompt_origin(CORRECTION, None)["kind"] == "task"
    assert bridge.task_prompt_origin("Please finish.", None) == {"kind": "nudge"}


def test_mapper_writes_title_and_redispatch_into_the_line():
    mapper = acp_chat_events.ACPEventMapper()
    [entry] = mapper.map_user_prompt(CORRECTION, origin="task", task_id="T1",
                                     title="Build the page", redispatch=True)
    assert entry["message"]["origin"] == {
        "kind": "task", "taskId": "T1", "title": "Build the page", "redispatch": True,
    }


def _run(tmp_path, prompt, **kw):
    lines: list[str] = []
    client = TaskClient(session_result=session_result("mc-openai/M"), turns=[{"chunks": ["ok"]}])
    bridge.run_acp_once(
        prompt, cwd=str(tmp_path), model="mc-openai/M", max_time=10,
        permission_policy="yolo", task_id="T1", client_factory=lambda: client,
        transcript_sink=lambda out: lines.extend(out), **kw,
    )
    users = [json.loads(l) for l in lines]
    return [e["message"] for e in users if (e.get("message") or {}).get("role") == "user"]


def test_run_acp_once_marks_a_correction_dispatch_as_task(tmp_path):
    [msg] = _run(tmp_path, CORRECTION, origin_kind="task")
    assert msg["origin"]["kind"] == "task"
    assert msg["origin"]["title"] == "Build the page"
    assert msg["origin"]["redispatch"] is True


def test_run_acp_once_marks_a_continue_as_nudge(tmp_path):
    [msg] = _run(tmp_path, "Please finish with the sentinel.", origin_kind="nudge")
    assert msg["origin"] == {"kind": "nudge", "taskId": "T1"}


def test_serve_loop_tells_the_factory_who_sends_what():
    """Tripwire on the two call sites in serve_loop's ACP branch."""
    src = (HERE.parent / "bridge.py").read_text(encoding="utf-8")
    assert 'return acp_run(_p, origin_kind="task")' in src
    assert 'return acp_run(nudge, origin_kind="nudge")' in src
    assert '"# New Task:" in prompt else "nudge"' not in src
