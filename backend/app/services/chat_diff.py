"""chat_diff — the one diff shape a chat tool card renders (chat package P1).

Harness-neutral on purpose: an ACP agent (omp over the bridge) reports an
edit as whole-file ``oldText``/``newText`` per path (ACP tool content
``type: "diff"``), Claude Code's transcript carries the edit's own
``old_string``/``new_string`` (Edit, MultiEdit) or the written ``content``
(Write). Both end up here and leave as the same structure, so the frontend
has exactly one diff component and no per-harness branches.

Shape (``ToolDiff`` in ``frontend-v2/src/lib/chatTypes.ts``)::

    {"path": str | None,
     "kind": "edit" | "create" | "write",
     "lines": [{"t": "hunk" | "ctx" | "add" | "del", "text": str}, ...],
     "additions": int, "deletions": int, "truncated": bool}

* ``create`` = the harness said the file did not exist (ACP ``oldText:
  null``). ``write`` = new content whose predecessor the transcript does not
  know (Claude ``Write``): shown as additions, but never claimed to be a new
  file — the card words it differently.
* ``additions``/``deletions`` count the WHOLE diff; ``lines`` is capped at
  ``MAX_DIFF_LINES`` (``truncated`` says so). The card's +/− badge and the
  diff therefore never disagree.

Pure, never raises on odd input (returns ``None``/``[]`` instead).
"""
from __future__ import annotations

import difflib
from typing import Any

#: Lines handed to the frontend per diff — a phone shows a dozen, "show all"
#: a few hundred; a 20k-line rewrite must not ride every SSE frame.
MAX_DIFF_LINES = 400
#: Characters per rendered line.
_MAX_LINE_CHARS = 500
#: Input texts beyond this are cut before diffing (difflib is quadratic in
#: the worst case; the bridge already caps at 60k).
_MAX_INPUT_CHARS = 200_000
#: Unchanged lines kept around each change.
_CONTEXT = 2

_CLAUDE_EDIT_TOOLS = ("Edit", "MultiEdit", "Write")


def _clip(line: str) -> str:
    return line if len(line) <= _MAX_LINE_CHARS else line[:_MAX_LINE_CHARS] + "…"


def build_diff(
    path: Any,
    old: str | None,
    new: str | None,
    *,
    kind: str | None = None,
    truncated: bool = False,
) -> dict[str, Any] | None:
    """Unified diff of ``old`` -> ``new``. ``old is None`` = created file
    (unless ``kind`` says otherwise). Identical texts -> ``None``."""
    if new is not None and not isinstance(new, str):
        return None
    if old is not None and not isinstance(old, str):
        return None
    new_text = new or ""
    old_text = old or ""
    if old is not None and old_text == new_text:
        return None
    if old is None and not new_text:
        return None
    if len(old_text) > _MAX_INPUT_CHARS or len(new_text) > _MAX_INPUT_CHARS:
        old_text, new_text = old_text[:_MAX_INPUT_CHARS], new_text[:_MAX_INPUT_CHARS]
        truncated = True

    lines: list[dict[str, str]] = []
    additions = deletions = 0
    raw = difflib.unified_diff(
        old_text.splitlines(), new_text.splitlines(), lineterm="", n=_CONTEXT
    )
    for i, line in enumerate(raw):
        if i < 2 and (line.startswith("---") or line.startswith("+++")):
            continue  # file headers: the card shows the path itself
        if line.startswith("@@"):
            entry = {"t": "hunk", "text": line}
        elif line.startswith("+"):
            additions += 1
            entry = {"t": "add", "text": _clip(line[1:])}
        elif line.startswith("-"):
            deletions += 1
            entry = {"t": "del", "text": _clip(line[1:])}
        else:
            entry = {"t": "ctx", "text": _clip(line[1:])}
        if len(lines) < MAX_DIFF_LINES:
            lines.append(entry)
        else:
            truncated = True
    if additions == 0 and deletions == 0:
        return None
    return {
        "path": path.strip() if isinstance(path, str) and path.strip() else None,
        "kind": kind or ("create" if old is None else "edit"),
        "lines": lines,
        "additions": additions,
        "deletions": deletions,
        "truncated": bool(truncated),
    }


def diffs_from_acp(raw: Any) -> list[dict[str, Any]]:
    """The bridge's ``details.diffs`` ([{path, oldText, newText,
    truncated?}]) — or omp's NATIVE edit details (``path``/``oldText``/
    ``newText`` directly, or per file in ``perFileResults``) — as diffs."""
    out: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        per_file = raw.get("perFileResults")
        raw = per_file if isinstance(per_file, list) else [raw]
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict) or item.get("isError") is True:
            continue
        if "oldText" not in item and "newText" not in item:
            continue
        diff = build_diff(
            item.get("path"),
            item.get("oldText") if isinstance(item.get("oldText"), str) else None,
            item.get("newText") if isinstance(item.get("newText"), str) else "",
            truncated=bool(item.get("truncated")),
        )
        if diff is not None:
            out.append(diff)
    return out


def diffs_from_claude_tool(name: str, tool_input: Any) -> list[dict[str, Any]]:
    """Claude Code Edit / MultiEdit / Write inputs -> diffs (empty for any
    other tool or an input without the strings)."""
    if name not in _CLAUDE_EDIT_TOOLS or not isinstance(tool_input, dict):
        return []
    path = tool_input.get("file_path")
    out: list[dict[str, Any]] = []
    if name == "Edit":
        old, new = tool_input.get("old_string"), tool_input.get("new_string")
        if isinstance(old, str) and isinstance(new, str):
            diff = build_diff(path, old, new)
            if diff is not None:
                out.append(diff)
    elif name == "MultiEdit":
        edits = tool_input.get("edits")
        for edit in edits if isinstance(edits, list) else []:
            if not isinstance(edit, dict):
                continue
            old, new = edit.get("old_string"), edit.get("new_string")
            if isinstance(old, str) and isinstance(new, str):
                diff = build_diff(path, old, new)
                if diff is not None:
                    out.append(diff)
    else:  # Write
        content = tool_input.get("content")
        if isinstance(content, str):
            diff = build_diff(path, None, content, kind="write")
            if diff is not None:
                out.append(diff)
    return out


def stats_of(diffs: list[dict[str, Any]]) -> dict[str, int] | None:
    """The card's +/− badge, summed from the SAME diffs it shows.

    A ``write`` diff does not count: what it replaced is unknown, so "+12"
    would claim 12 added lines for what may have been a rewrite. Only diffs
    with a known before (``edit``) or none at all (``create``) feed it."""
    known = [d for d in diffs or [] if d.get("kind") != "write"]
    if not known:
        return None
    return {
        "additions": sum(int(d.get("additions") or 0) for d in known),
        "deletions": sum(int(d.get("deletions") or 0) for d in known),
    }
