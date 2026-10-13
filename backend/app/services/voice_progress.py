"""Live voice — progress context lines for the voice (voice PR 3, bauplan B4).

While a call is open, the browser watches the chat's own live stream (the
same SSE the chat renders) and decides WHEN something happened: the agent
started on the voice request, a todo step changed, a tool started or
finished, an approval or a question is pending, the turn ended, an error.
It then asks MC for the ONE line the realtime model may read
(``POST /api/v1/voice/live/progress``) and hands it over as a system item.

This module decides WHAT that line says — one place, Python, pinned by
``tests/test_voice_progress.py``, so the redaction rules exist exactly once:

- secrets out (``log_redaction.redact_secrets`` plus the voice's own token
  shapes), paths cut to their last segment, one line, capped;
- a tool contributes its name and title only, never its output (the request
  model refuses an output field outright);
- the agent's final reply is shortened to its first one or two sentences,
  without code blocks or markdown;
- ONLY a ``final`` line carries ``agent_final=true``. Every other line says
  ``agent_final=false`` — the persona tells the voice never to report success
  without a true one (bauplan B2 rule 3, no-success guard layer a);
- ``premature_success`` is the correction the browser asks for when the
  voice's own words claimed success before the agent reported it (layer b).
  Each one is counted (``voice_premature_success``) for the PR 6 evals.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.services.voice_live_tools import clean_line, redact_short

logger = logging.getLogger("mc.voice_live")

PROGRESS_KINDS = (
    "snapshot",  # where the agent stands when the call starts (context only)
    "started",  # the agent started working on what the voice passed on
    "working",  # the agent started working on its own (task, schedule)
    "step",  # the todo step changed
    "tool_started",
    "tool_finished",
    "approval",  # waiting for an approval (tap in the chat)
    "question",  # the agent asks the operator something
    "final",  # the turn ended — the agent's own reply, shortened
    "error",  # the chat daemon reported an error
    "still_working",  # periodic heartbeat while busy
    "premature_success",  # correction: the voice claimed success too early
)

FINAL_MAX_CHARS = 240
QUESTION_MAX_CHARS = 120
TITLE_MAX_CHARS = 60

_premature_success = 0


def premature_success_count() -> int:
    """How often the guard fired in this process (``voice_premature_success``)."""
    return _premature_success


def _note_premature_success(agent_id: str) -> None:
    global _premature_success
    _premature_success += 1
    logger.info("voice_premature_success agent=%s count=%d", agent_id, _premature_success)


# ── the agent's reply → 1–2 spoken sentences ─────────────────────────────

_FENCE = re.compile(r"```.*?(?:```|\Z)|~~~.*?(?:~~~|\Z)", re.S)
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_INLINE_CODE = re.compile(r"`([^`]*)`")
_LINE_LEAD = re.compile(r"^\s*(?:#{1,6}\s+|>\s*|[-*+]\s+|\d+[.)]\s+)")
_EMPHASIS = re.compile(r"(\*\*|__|\*|_|~~)(?=\S)(.+?)(?<=\S)\1")
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


def summarize_reply(text: str) -> str:
    """First one or two sentences of the agent's reply, read-aloud safe:
    no code blocks, no headings, tables or markdown marks, redacted, capped."""
    raw = _FENCE.sub(" ", text or "")
    lines: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("|") or set(stripped) <= set("-=*_ "):
            continue
        if re.match(r"^#{1,6}\s", stripped):
            continue  # a heading is a label, not a sentence
        line = _LINE_LEAD.sub("", stripped)
        line = _LINK.sub(r"\1", line)
        line = _INLINE_CODE.sub(r"\1", line)
        line = _EMPHASIS.sub(r"\2", line)
        lines.append(line)
    flat = clean_line(" ".join(lines))
    if not flat:
        return ""
    sentences = [s for s in _SENTENCE_END.split(flat) if s.strip()]
    picked = " ".join(sentences[:2])
    return redact_short(picked, FINAL_MAX_CHARS)


# ── the line ─────────────────────────────────────────────────────────────


def _tool_label(tool: dict[str, Any] | None) -> str:
    if not tool:
        return ""
    name = clean_line(str(tool.get("name") or ""))[:30]
    title = str(tool.get("title") or "")
    label = f"{name} · {title}" if title and clean_line(title) != name else name
    return redact_short(label, TITLE_MAX_CHARS)


def _step_label(step: dict[str, Any] | None) -> str:
    if not step:
        return ""
    try:
        index = max(0, int(step.get("index") or 0))
        total = max(0, int(step.get("total") or 0))
    except (TypeError, ValueError):
        return ""
    content = redact_short(str(step.get("content") or ""), TITLE_MAX_CHARS)
    if step.get("all_done"):
        return f"all {total} steps checked off"
    return f"step {index}/{total}: {content}" if content else f"step {index}/{total}"


def _elapsed(seconds: int | None) -> str:
    if not seconds or seconds < 1:
        return ""
    if seconds < 60:
        return f" for {int(seconds)} s"
    return f" for {int(seconds) // 60} min"


def _error_code(code: str) -> str:
    m = re.match(r"[a-z][a-z0-9_]{0,39}", (code or "").strip().lower())
    return m.group(0) if m else "unknown"


def build_item(
    kind: str,
    agent_name: str,
    *,
    step: dict[str, Any] | None = None,
    tool: dict[str, Any] | None = None,
    text: str = "",
    question: str = "",
    error_code: str = "",
    elapsed_s: int | None = None,
) -> dict[str, Any]:
    """The context line for one milestone. Returns ``{"text", "agent_final"}``.
    Pure; the router does guards and logging."""
    if kind not in PROGRESS_KINDS:
        raise ValueError(kind)
    name = clean_line(agent_name or "the agent")[:80] or "the agent"
    final = kind == "final"
    head = (
        f"[MC progress · from Mission Control, not from the operator · agent_final={'true' if final else 'false'}]"
    )
    step_l = _step_label(step)
    tool_l = _tool_label(tool)
    where = "; ".join(p for p in (step_l, f"current tool: {tool_l}" if tool_l else "") if p)

    if kind == "snapshot":
        body = f"At call start {name} is busy" + (f" — {where}." if where else ".") + " NOT done."
    elif kind == "started":
        body = f"{name} started working on what you passed on. NOT done yet."
    elif kind == "working":
        body = f"{name} started working" + (f" — {where}." if where else ".") + " NOT done yet."
    elif kind == "step":
        if step and step.get("all_done"):
            body = f"{name}: {step_l} — NOT the final report yet, wait for it."
        else:
            body = f"{name} is now on {step_l or 'a new step'}. NOT done yet."
    elif kind == "tool_started":
        body = f"{name} is running: {tool_l or 'a tool'}. NOT done yet."
    elif kind == "tool_finished":
        failed = bool(tool and tool.get("failed"))
        body = f"{name} finished a tool{' (it failed)' if failed else ''}: {tool_l or 'a tool'}. NOT done yet."
    elif kind == "approval":
        q = redact_short(question, QUESTION_MAX_CHARS)
        body = (
            f"{name} waits for an approval{': ' + q if q else ''}. The operator must tap it in the chat; "
            "a spoken yes does not count. Tell them in one short sentence what he wants and ask for the "
            "tap (\"Bitte im Chat antippen.\"); never pass a yes or no on."
        )
    elif kind == "question":
        q = redact_short(question, QUESTION_MAX_CHARS)
        body = f"{name} asks the operator{': ' + q if q else ' something'}. The answer goes in the chat. NOT done."
    elif kind == "final":
        summary = summarize_reply(text)
        if summary:
            body = f"{name} finished this turn. His reply, shortened: „{summary}“ Details are in the chat."
        else:
            body = f"{name} finished this turn without a text reply. Details are in the chat."
    elif kind == "error":
        body = f"{name} hit an error ({_error_code(error_code)}). Say so plainly; do not guess the cause. NOT done."
    elif kind == "still_working":
        body = f"{name} is still working{_elapsed(elapsed_s)}" + (f" — {where}." if where else ".") + " NOT done yet."
    else:  # premature_success
        head = "[MC correction · from Mission Control · agent_final=false]"
        body = (
            f"You just implied the work is done. It is NOT: {name} has not reported a result yet. "
            "Correct yourself now in one short sentence, for example "
            "\"Korrektur: noch nicht fertig, er arbeitet noch.\" or \"Correction: not done yet, he is still working.\""
        )
    return {"text": f"{head} {body}", "agent_final": final}


def progress_line(kind: str, agent_name: str, agent_id: str, **fields: Any) -> dict[str, Any]:
    """``build_item`` plus the bookkeeping the router needs (counter, log
    line with the kind only — never the words)."""
    item = build_item(kind, agent_name, **fields)
    if kind == "premature_success":
        _note_premature_success(agent_id)
    logger.info("voice_live progress kind=%s agent=%s len=%d", kind, agent_id, len(item["text"]))
    return item
