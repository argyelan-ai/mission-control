"""The run record as a handful of keys, not a markdown blob (docs/specs/
head-launcher.md §7 ``GET /heads/{run_id}/summary``; bauplan `heads-sichtbar`
PR 2 §3.1).

``HeadRunRecordCard`` on the frontend wants to show "did the tests go
red → green", "was there a sabotage probe", "who reviewed it" as small facts
next to the state the chat header already carries — not the operator reading
raw markdown inside a chat bubble. This module is the one place that knows
how a run record (``templates/run-record.md``, ``~/.claude/skills/
head-procedure/templates/run-record.md``) is laid out; the frontend never
parses markdown itself.

Every field is **extracted, never guessed**: a line or table cell the
template's own wording did not produce comes back ``None``, the same rule
``services/heads/transcript.py`` applies to a missing transcript. A run
record that is just frontmatter plus a lone ``## Result`` bullet (both PR 1
fixtures, and most real runs today) is a legitimate, fully-formed summary
with nine of its eleven fields ``None`` — that is not a parsing failure, it
is the file telling the truth about what it was never asked to record.

``result_line`` and the two ``tests`` strings are the only fields that can
carry prose copied out of a transcript-adjacent file, so — same rule as
``transcript.py`` — they go through ``redact.mask_text`` with this run's own
``head.env`` secret values folded in before this module hands them to the
router. ``branch``/``pr_url``/``status``/``review``/the three counters never
touch free text; they are either read straight off ``HeadRun``/its spec
(already-trusted fields the rest of the API returns unmasked today) or a
single word/number lifted out of a fixed template phrase.
"""
from __future__ import annotations

import re
from typing import Any

from app.services.heads.redact import head_env_values, mask_text

#: ``## Heading`` lines split the body into named sections. Matches exactly
#: what `templates/run-record.md`/the head-procedure skill's own template
#: produce — a line starting with two ``#`` and a space, nothing fancier
#: (no ATX closing hashes, no setext headings appear in either template).
_SECTION_RE = re.compile(r"(?m)^##[ \t]+(.+?)[ \t]*$")

#: ``Status: running|passed|failed`` — the one line every template revision
#: has carried since PR 1's own fixtures (`run-record.md`'s own "Heartbeat …
#: Status: passed" line), independent of the surrounding Heartbeat text.
_STATUS_RE = re.compile(r"(?mi)^Status:[ \t]*(running|passed|failed)[ \t]*$")


def _sections(text: str) -> dict[str, str]:
    """``{lowercased heading: body text}`` — body runs to the next heading or
    the end of the document. A record with no ``##`` heading at all (just
    frontmatter + the title + the Heartbeat/Status lines, e.g. a run that
    ended before step 1) yields an empty mapping, never an exception."""
    matches = list(_SECTION_RE.finditer(text))
    out: dict[str, str] = {}
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out[m.group(1).strip().lower()] = text[start:end]
    return out


def _bullet(section: str, label: str) -> str | None:
    """The remainder of a ``- <Label>: <value>`` bullet (case-insensitive
    label), or ``None`` when that bullet is not there at all. Matches the
    exact "``- Field: value``" shape every template bullet uses — a record
    that instead free-writes the result as plain prose (no leading ``- X:``)
    is a record this function correctly does not understand, rather than one
    it mis-reads."""
    m = re.search(rf"(?mi)^-[ \t]*{re.escape(label)}[ \t]*:[ \t]*(.+)$", section)
    return m.group(1).strip() or None if m else None


def _after_arrow(value: str | None) -> str | None:
    """"`<command>` → <key line>" → just the key line. A bullet with no
    arrow at all is returned whole (some runs write the result directly, no
    arrow needed) rather than discarded."""
    if value is None:
        return None
    return value.rsplit("→", 1)[-1].strip() or None


#: Bullet values the template itself uses for "this step was skipped" —
#: distinct from the bullet being ABSENT (→ ``None``, unknown) which is the
#: far more common case for a run that never reached this step.
_NEGATIVE = {"none", "n/a", "na", "not performed", "not done", "skipped", "no"}


def _sabotage(section: str) -> bool | None:
    value = _bullet(section, "Sabotage probe")
    if value is None:
        return None
    return value.strip().lower() not in _NEGATIVE


def _review(section: str) -> str | None:
    """"``Reviewer (fresh subagent): PASSED — …``" → ``"helper"``;
    "``Reviewer (self): …``" (a record that says so explicitly, never the
    template's own default wording) → ``"self"``. Whether that review
    PASSED or FAILED is not this field's job — ``result_line``/``status``
    already carry the run's outcome; this one only answers "who looked"."""
    m = re.search(r"(?mi)^-[ \t]*Reviewer[ \t]*\(([^)]*)\)[ \t]*:", section)
    if not m:
        return None
    return "self" if "self" in m.group(1).lower() else "helper"


def _bypass(section: str) -> int | None:
    value = _bullet(section, "Bypass")
    if value is None:
        return None
    m = re.match(r"(\d+)", value)
    return int(m.group(1)) if m else None


def _table_int(section: str, row_label: str, after: str) -> int | None:
    """The run record's two counters live in the ``## Numbers`` markdown
    table, each as free text inside one cell (`"head estimate: <n> min (…)"`,
    `"<n> subagents · <m> local steps"`) rather than their own column — so
    the row is found by its label, and the number by the fixed word
    immediately in front of it, on that same line (a markdown table row is
    always one line)."""
    m = re.search(rf"(?mi)^.*{re.escape(row_label)}.*$", section)
    if not m:
        return None
    n = re.search(rf"{re.escape(after)}[ \t]*(\d+)", m.group(0), re.IGNORECASE)
    return int(n.group(1)) if n else None


def _kz_ok(sections: dict[str, str]) -> bool | None:
    """The run's own context brief, not a separate field: step 0 of the head
    procedure either pastes the real ``kz brief`` output in, or — when the
    tool was unavailable — copies the literal line "``kz brief
    unavailable: …``" into this same section (`head-AGENTS.md` §0's own
    instruction). A record with no context-brief-named section at all
    (older runs, or one that ended before step 0 finished) answers
    ``None`` — there is nothing here to call ok or not."""
    for name, body in sections.items():
        if "context brief" in name:
            return "kz brief unavailable" not in body.lower()
    return None


def build_summary(run: Any) -> dict[str, Any]:
    """``run.run_record_text`` → the dict ``docs/specs/head-launcher.md`` §7
    documents for ``GET /heads/{run_id}/summary``. Caller (the router)
    already turned "no run record at all" into a 404 before this is called —
    every field here can still be ``None``, which is the record being
    legitimately silent about that one fact, not an error."""
    text = run.run_record_text or ""
    sections = _sections(text)
    result = sections.get("result", "")
    evidence = sections.get("evidence", "")
    numbers = sections.get("numbers", "")

    extra = head_env_values(run)
    status_m = _STATUS_RE.search(text)

    # The Result section's own first bullet is the one-sentence summary the
    # template asks for ("<What is different now, in plain sentences>") —
    # the FIRST ``- …`` line, not a labelled one (unlike every other field
    # here, this section has no fixed label to search for).
    m = re.search(r"(?m)^-[ \t]*(.+)$", result)
    result_line = mask_text(m.group(1).strip(), extra) if m else None

    failed_before = mask_text(t, extra) if (t := _after_arrow(_bullet(evidence, "Red test before"))) else None
    passed_after = mask_text(t, extra) if (t := _after_arrow(_bullet(evidence, "Green after"))) else None

    return {
        "run_id": run.run_id,
        "status": status_m.group(1).lower() if status_m else None,
        "result_line": result_line,
        "tests": {"failed_before": failed_before, "passed_after": passed_after},
        "sabotage": _sabotage(evidence),
        "kz_ok": _kz_ok(sections),
        "review": _review(evidence),
        "bypass": _bypass(evidence),
        "operator_minutes": _table_int(numbers, "Operator minutes", "estimate:"),
        "helpers": _table_int(numbers, "Helpers", ""),
        "branch": run.spec.get("branch") if isinstance(run.spec, dict) else None,
        "pr_url": run.pr_url,
    }
