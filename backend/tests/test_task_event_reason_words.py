"""Every reason code a status step can carry has words in the UI (round-8
review SHOULD): the timeline / History tab show ``tasks.detail.statusReason
.<code>``; a code without words was humanized ("manual update") — English in
the German UI. This scans the backend for the codes it writes into
``TaskEvent.reason`` and checks both message files, so a new code without
words turns this red instead of mixing UI languages."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"
MESSAGES = Path(__file__).resolve().parents[2] / "frontend-v2" / "messages"

#: Calls whose ``reason=`` ends up in ``TaskEvent.reason``.
_SINKS = {
    "record_task_event", "system_finalize_task_done", "retire_blocked_entry_task",
    "start_head", "hold_and_move", "_hold_and_move", "move_task",
}


def _strings(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):
        return _strings(node.body) | _strings(node.orelse)
    return set()


def _backend_codes() -> set[str]:
    codes: set[str] = set()
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name in _SINKS:
                    for kw in node.keywords:
                        if kw.arg == "reason":
                            codes |= _strings(kw.value)
            elif isinstance(node, ast.Assign):
                # handle_review_rejection picks its code into ``event_reason``.
                if any(isinstance(t, ast.Name) and t.id == "event_reason" for t in node.targets):
                    codes |= _strings(node.value)
    # The head mirror writes ``head_<state>``; start_head defaults to head_start.
    from app.services.heads.mirror import HEAD_TO_TASK

    codes |= {f"head_{state}" for state in HEAD_TO_TASK} | {"head_start"}
    return {c for c in codes if c and c.replace("_", "").isalnum() and c.islower()}


@pytest.mark.skipif(not MESSAGES.is_dir(), reason="frontend-v2 messages not in this checkout")
@pytest.mark.parametrize("lang", ["en", "de"])
def test_every_status_step_reason_has_words(lang):
    words = json.loads((MESSAGES / f"{lang}.json").read_text(encoding="utf-8"))
    known = words["tasks"]["detail"]["statusReason"]
    codes = _backend_codes()
    assert {"manual_update", "clarification_answered", "head_passed", "review_rejection_agent_paused"} <= codes
    missing = sorted(c for c in codes if not str(known.get(c, "")).strip())
    assert not missing, f"{lang}.json tasks.detail.statusReason lacks words for: {missing}"
