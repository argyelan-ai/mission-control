"""Helpers (subagents) from an omp transcript — the data behind the chat's
"3 helpers · 2 working" line.

omp starts helpers with its ``task`` tool: ONE call can start several
(``arguments.tasks``), its result lists them under ``details.progress`` with
their state, and they report back later in an ``async-result`` system line
whose ``details.jobs`` names each job (``type: "task"``) with its measured
``durationMs`` and whose ``<task-result … status="…">`` tag carries the
outcome. Fixture lines keep the real structure (omp 16.09.2026) with neutral
content.
"""
import json

from app.services.omp_chat import OmpLineParser

TASK_CALL = json.dumps({
    "type": "message", "id": "c1", "parentId": "p0", "timestamp": "2026-09-16T07:04:23.164Z",
    "message": {"role": "assistant", "content": [{
        "type": "toolCall", "id": "call_1", "name": "task",
        "arguments": {"i": "Scouting paths", "context": "ctx", "tasks": [
            {"agent": "scout", "name": "ScoutA", "task": "find a"},
            {"agent": "scout", "name": "ScoutB", "task": "find b"},
        ]},
    }]},
})

TASK_RESULT = json.dumps({
    "type": "message", "id": "r1", "parentId": "c1", "timestamp": "2026-09-16T07:04:23.171Z",
    "message": {"role": "toolResult", "toolCallId": "call_1", "toolName": "task",
                "content": [{"type": "text", "text": "Spawned 2 background agents using scout."}],
                "details": {"async": True, "results": [], "totalDurationMs": 1, "progress": [
                    {"index": 0, "id": "ScoutA", "agent": "scout", "status": "pending", "durationMs": 0},
                    {"index": 1, "id": "ScoutB", "agent": "scout", "status": "running", "durationMs": 0},
                ]}},
})


def _async_result(jobs: list[dict], body: str) -> str:
    return json.dumps({
        "type": "custom_message", "customType": "async-result", "display": True,
        "content": f"<system-notice>\n{len(jobs)} background jobs have completed.\n\n{body}</system-notice>",
        "details": {"jobs": jobs}, "attribution": "agent", "id": "n1", "parentId": "r1",
        "timestamp": "2026-09-16T07:06:27.514Z",
    })


DONE_LINE = _async_result(
    [{"jobId": "ScoutA", "type": "task", "label": "ScoutA", "durationMs": 125965},
     {"jobId": "ScoutB", "type": "task", "label": "ScoutB", "durationMs": 3969}],
    '── Job ScoutA (ScoutA) ──\n<task-result id="ScoutA" agent="scout" status="completed" '
    'duration="2m 6s">\n<output>found it</output>\n</task-result>\n'
    '── Job ScoutB (ScoutB) ──\n<task-result id="ScoutB" agent="scout" status="failed (exit 1)" '
    'duration="4.0s">\n<output>401</output>\n</task-result>\n',
)


def _tool(parser: OmpLineParser) -> dict:
    (ev,) = [e for e in parser(TASK_CALL) if e["kind"] == "tool"]
    return ev


def test_task_call_lists_its_helpers_as_working_before_any_result():
    ev = _tool(OmpLineParser())
    assert [(h["id"], h["agent"], h["state"]) for h in ev["helpers"]] == [
        ("ScoutA", "scout", "working"), ("ScoutB", "scout", "working"),
    ]


def test_task_result_progress_becomes_the_helpers():
    parser = OmpLineParser()
    _tool(parser)
    (res,) = parser(TASK_RESULT)
    assert [(h["id"], h["state"]) for h in res["helpers"]] == [("ScoutA", "working"), ("ScoutB", "working")]


def test_unknown_progress_state_is_not_guessed():
    parser = OmpLineParser()
    line = TASK_RESULT.replace('"status": "running"', '"status": "teleporting"')
    (res,) = parser(line)
    assert "helpers" not in res


def test_async_result_becomes_one_notification_per_helper():
    parser = OmpLineParser()
    _tool(parser)
    parser(TASK_RESULT)
    evs = [e for e in parser(DONE_LINE) if e["kind"] == "notification"]
    a, b = evs
    assert (a["taskId"], a["toolUseId"], a["status"], a["durationMs"]) == ("ScoutA", "call_1", "completed", 125965)
    assert (b["taskId"], b["status"], b["durationMs"]) == ("ScoutB", "failed", 3969)


def test_async_result_without_the_call_in_view_still_names_the_helper():
    """A history page can start AFTER the call — then the job is matched by
    its id in the frontend; the tool id stays unknown, never invented."""
    (a, b) = [e for e in OmpLineParser()(DONE_LINE) if e["kind"] == "notification"]
    assert a["taskId"] == "ScoutA" and a["toolUseId"] is None


def test_background_bash_result_stays_a_system_message():
    line = _async_result([{"jobId": "bg_1", "type": "bash"}], "Job bg_1 done.\n")
    (ev,) = OmpLineParser()(line)
    assert ev["kind"] == "message" and ev["role"] == "teammate"


def test_mixed_result_keeps_the_message_and_adds_the_helper_report():
    line = _async_result(
        [{"jobId": "bg_1", "type": "bash"}, {"jobId": "ScoutA", "type": "task", "durationMs": 5}],
        'Job bg_1 done.\n<task-result id="ScoutA" agent="scout" status="completed" duration="0s">'
        "<output>x</output></task-result>\n",
    )
    kinds = [e["kind"] for e in OmpLineParser()(line)]
    assert kinds == ["message", "notification"]


def test_reset_forgets_the_job_map():
    parser = OmpLineParser()
    _tool(parser)
    parser.reset()
    (a, _b) = [e for e in parser(DONE_LINE) if e["kind"] == "notification"]
    assert a["toolUseId"] is None


def test_helper_only_async_result_keeps_what_the_helpers_returned():
    """omp has no subagent transcript to open — the async-result text IS the
    only place the operator can read a helper's findings or failure reason.
    The notifications close the helpers; the message with the output stays."""
    evs = OmpLineParser()(DONE_LINE)
    messages = [e for e in evs if e["kind"] == "message"]
    assert len(messages) == 1
    assert messages[0]["role"] == "teammate"
    assert "found it" in messages[0]["text"] and "401" in messages[0]["text"]
    assert [e["kind"] for e in evs].count("notification") == 2


# The `job` tool (poll / await) — the other way an omp helper's end is
# written: ``details.jobs[{id, type, status, durationMs, resultText}]`` (real
# shape, omp 10/2026; neutral content). 10 of 72 real helpers ended ONLY here.
def _job_result(jobs: list[dict], call_id: str = "call_job") -> str:
    return json.dumps({
        "type": "message", "id": "j1", "parentId": "r1", "timestamp": "2026-09-16T07:10:00.000Z",
        "message": {"role": "toolResult", "toolCallId": call_id, "toolName": "job",
                    "content": [{"type": "text", "text": "## Completed (1)"}],
                    "details": {"jobs": jobs}, "isError": False, "timestamp": 1791105836524},
    })


def test_job_tool_result_closes_a_finished_helper():
    parser = OmpLineParser()
    _tool(parser)
    evs = parser(_job_result([
        {"id": "ScoutA", "type": "task", "status": "completed", "label": "ScoutA",
         "durationMs": 415882, "resultText": "<task-result id=\"ScoutA\">ok</task-result>"},
        {"id": "ScoutB", "type": "task", "status": "running", "label": "ScoutB"},
        {"id": "bg_1", "type": "bash", "status": "completed"},
    ]))
    notes = [e for e in evs if e["kind"] == "notification"]
    assert [(n["taskId"], n["toolUseId"], n["status"], n["durationMs"]) for n in notes] == [
        ("ScoutA", "call_1", "completed", 415882),
    ]
    assert notes[0]["ts"] == "2026-09-16T07:10:00.000Z"
    # The tool row itself (with its readable result text) is still there.
    assert [e["kind"] for e in evs].count("_tool_result") == 1


def test_job_tool_failed_helper_reports_failed():
    evs = OmpLineParser()(_job_result([{"id": "ScoutB", "type": "task", "status": "failed", "durationMs": 9}]))
    (n,) = [e for e in evs if e["kind"] == "notification"]
    assert (n["taskId"], n["status"], n["toolUseId"]) == ("ScoutB", "failed", None)


def test_job_tool_unknown_status_is_not_guessed():
    evs = OmpLineParser()(_job_result([{"id": "ScoutB", "type": "task", "status": "teleporting"}]))
    assert [e for e in evs if e["kind"] == "notification"] == []
