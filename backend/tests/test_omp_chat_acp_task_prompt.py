"""ACP task sessions in the agent chat (incident 19.10.2026, bench task).

The omp bridge runs a task over ACP and writes its own sink file; the chat
view follows the newest session file, so the task run shows up in the
agent's chat. Its first line is the dispatch prompt — Operating Card plus
"# New Task: …" — which the parser rendered as a plain USER turn: the
operator saw 8000 characters of briefing as a bubble they had "sent". Over
the native TUI the same prompt arrives as an ``@task-….md`` mention and
renders as a task event; the ACP line must look the same.

Also covered: the ``chat_notice`` line the chat daemon/bridge write when the
pinned model is no longer offered (docker/omp-bridge/acp_model.py).
"""
from __future__ import annotations

import json

from app.services.omp_chat import OmpLineParser

PROMPT = (
    "# Operating Card — Agent\n\nRole line\n\n---\n\n"
    "# New Task: [Bench] Pagoda — Model-X\n\nBuild one index.html.\n"
)


def _line(obj: dict) -> str:
    return json.dumps(obj)


def _header(**extra) -> str:
    return _line({"type": "session", "version": 3, "id": "s1",
                  "timestamp": "2026-10-19T11:05:57.675Z", "cwd": "", **extra})


def _user(text: str, **message_extra) -> str:
    return _line({
        "type": "message", "id": "acp00001", "parentId": None,
        "timestamp": "2026-10-19T11:05:57.664Z",
        "message": {"role": "user", "content": [{"type": "text", "text": text}],
                    "attribution": "user", **message_extra},
    })


def test_mc_attributed_dispatch_prompt_is_a_task_event_not_an_operator_bubble():
    p = OmpLineParser()
    p(_header(bridge="acp", taskId="T1"))
    [ev] = p(_user(PROMPT, attribution="mc", origin={"kind": "task", "taskId": "T1"}))
    assert ev["role"] == "teammate"
    assert ev["source"] == {"kind": "task", "title": "[Bench] Pagoda — Model-X"}


def test_legacy_task_sink_file_without_attribution_is_recognised_by_its_header():
    """Files the bridge wrote before it stamped ``attribution: "mc"`` — the
    live one from the incident is such a file."""
    p = OmpLineParser()
    p(_header(bridge="acp", taskId="T1"))
    [ev] = p(_user(PROMPT))
    assert ev["role"] == "teammate"
    assert ev["source"]["kind"] == "task"
    assert ev["source"]["title"] == "[Bench] Pagoda — Model-X"


def test_continue_nudge_in_a_task_session_is_a_nudge_event():
    p = OmpLineParser()
    p(_header(bridge="acp", taskId="T1"))
    [ev] = p(_user("Please finish with the sentinel.", attribution="mc",
                   origin={"kind": "nudge", "taskId": "T1"}))
    assert ev["role"] == "teammate"
    assert ev["source"] == {"kind": "nudge", "title": None}


def test_operator_turn_in_the_chat_daemon_session_stays_a_user_bubble():
    """The chat daemon's own file (bridge=acp, NO taskId): what the operator
    typed must stay theirs."""
    p = OmpLineParser()
    p(_header(bridge="acp"))
    [ev] = p(_user("sparky?"))
    assert ev["role"] == "user"
    assert ev["text"] == "sparky?"


def test_reset_forgets_the_task_session():
    p = OmpLineParser()
    p(_header(bridge="acp", taskId="T1"))
    p.reset()
    [ev] = p(_user("hello"))
    assert ev["role"] == "user"


def test_chat_notice_line_becomes_a_notice_event_with_its_values():
    p = OmpLineParser()
    [ev] = p(_line({
        "type": "custom_message", "customType": "chat_notice",
        "content": "Previously chosen model Old-1 is not available anymore — using New-2.",
        "display": True, "attribution": "agent", "id": "acpn1", "parentId": None,
        "timestamp": "2026-10-19T11:08:22.052Z",
        "data": {"code": "model_fallback", "previous": "Old-1", "current": "New-2"},
    }))
    assert ev["role"] == "teammate"
    assert ev["source"] == {"kind": "notice", "title": "model_fallback"}
    assert ev["notice"] == {"code": "model_fallback", "previous": "Old-1", "current": "New-2"}
    assert "New-2" in ev["text"]
