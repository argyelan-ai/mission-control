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
    it mis-reads. Anchored at the START of the bullet (``^-\\s*Label\\s*:``),
    never a bare "contains" search — a label that is merely MENTIONED inside
    an earlier, unrelated bullet's free text (e.g. "``- Quota: … no
    helpers``" ahead of the real "``- Helpers: 2``" bullet) must never be
    mistaken for the labelled bullet itself (review finding on PR #756)."""
    m = re.search(rf"(?mi)^-[ \t]*{re.escape(label)}[ \t]*:[ \t]*(.+)$", section)
    return m.group(1).strip() or None if m else None


def _bullet_any(section: str, *labels: str) -> str | None:
    """``_bullet`` over several labels, first match wins — the in-repo
    template (`backend/templates/heads/head-AGENTS.md`) renamed several
    fields from the older personal-skill template
    (``~/.claude/skills/head-procedure/templates/run-record.md``); both
    wordings are accepted so neither a pre-rename run record nor a future
    template edit silently goes back to all-``None`` (review finding on
    PR #756: every real run on disk used the NEW labels, which the old
    parser never matched at all)."""
    for label in labels:
        value = _bullet(section, label)
        if value is not None:
            return value
    return None


def _after_arrow(value: str | None) -> str | None:
    """"`<command>` → <key line>" → just the key line, split at the FIRST
    arrow (not the last): a real run's bullet often carries more than one
    "``<command>`` → ``<result>``" pair on the same line (e.g. a quick
    command's result, then "full suite `...` → ..." after it) — splitting
    on the LAST arrow silently discarded the actual first result and kept
    only a trailing aside (review finding on PR #756, reproduced on a real
    "Green after" bullet). A bullet with no arrow at all is returned whole
    (some runs write the result directly, no arrow needed) rather than
    discarded."""
    if value is None:
        return None
    return value.split("→", 1)[-1].strip() or None


#: Bullet values the template itself uses for "this step was skipped" —
#: distinct from the bullet being ABSENT (→ ``None``, unknown) which is the
#: far more common case for a run that never reached this step.
_NEGATIVE = {"none", "n/a", "na", "not performed", "not done", "skipped", "no"}


def _sabotage(section: str) -> bool | None:
    value = _bullet_any(section, "Sabotage check", "Sabotage probe")
    if value is None:
        return None
    return value.strip().lower() not in _NEGATIVE


def _review(section: str) -> str | None:
    """Two template generations, two shapes:

    - in-repo (`backend/templates/heads/head-AGENTS.md`): a free-text
      ``- Review: <fresh helper PASSED/FAILED | self-review> — …`` bullet.
      "starts with self" → ``"self"`` (real runs write "self, no helper
      available …" — the word "helper" also appears LATER in that same
      sentence, so this must be checked before the "helper" substring
      check below, not instead of it); otherwise "helper" anywhere in the
      bullet → ``"helper"``.
    - older personal-skill template: "``Reviewer (fresh subagent):
      PASSED — …``" → ``"helper"``; "``Reviewer (self): …``" → ``"self"``.

    Either way, whether that review PASSED or FAILED is not this field's
    job — ``result_line``/``status`` already carry the run's outcome; this
    one only answers "who looked"."""
    value = _bullet(section, "Review")
    if value is not None:
        v = value.strip().lower()
        if v.startswith("self"):
            return "self"
        if "helper" in v:
            return "helper"
        return None
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


#: In-repo template: plain bullets, anchored at the start of the line —
#: ``- Helpers: <n>`` / ``- Operator minutes (estimate): <n>`` (the
#: "(estimate)" qualifier is part of the label, optional only because an
#: older run record may have been written before it was added to the
#: template). A leading ``~`` ("~25") is allowed before the digits — the
#: template's own "estimate" wording invites an approximate number.
_HELPERS_RE = re.compile(r"(?mi)^-[ \t]*Helpers[ \t]*:[ \t]*~?[ \t]*(\d+)")
_OPERATOR_MINUTES_RE = re.compile(
    r"(?mi)^-[ \t]*Operator minutes(?:[ \t]*\(estimate\))?[ \t]*:[ \t]*~?[ \t]*(\d+)"
)
#: Older personal-skill template: a ``## Numbers`` MARKDOWN TABLE, one row
#: per fact, the number free-text inside the cell rather than its own
#: column (`"<n> subagents · <m> local steps"`, `"head estimate: <n> min
#: (…)"`) — so the row is found by its label, anchored at the start of the
#: table row (never a bare "contains" search: an earlier, unrelated row can
#: mention the same word in its own free text, e.g. a "Quota" row that ends
#: "… no helpers" ahead of the real "Helpers" row — review finding on
#: PR #756, reproduced on a real run record), and the number by the fixed
#: word immediately in front of it, on that same line.
_HELPERS_TABLE_RE = re.compile(r"(?mi)^\|[ \t]*Helpers[ \t]*\|[ \t]*(\d+)")
_OPERATOR_MINUTES_TABLE_RE = re.compile(
    r"(?mi)^\|[ \t]*Operator minutes[ \t]*\|[ \t]*.*?estimate:[ \t]*(\d+)"
)


def _first_int(section: str, *patterns: re.Pattern[str]) -> int | None:
    for pattern in patterns:
        m = pattern.search(section)
        if m:
            return int(m.group(1))
    return None


def _kz_ok(evidence: str, sections: dict[str, str]) -> bool | None:
    """The in-repo template's own ``- kz check: …`` bullet (Evidence
    section) is the real signal — "a pasted output, last line e.g. 'OK (4
    skipped)' | red lines + 'Known limits' in the PR | no .kohaerenz.yaml |
    unavailable: …" (`head-AGENTS.md`'s own template line). The card used to
    read this from the unrelated "kz brief: …" line in the Context brief
    instead (a review finding on PR #756: that line answers "was step 0's
    research brief available", not "did the push-time kz check pass" — a
    run whose Context brief had a real brief but whose later kz check
    actually failed still showed "kz check: Yes").

    ``unavailable``/``no .kohaerenz.yaml`` → ``None`` (nothing to call ok or
    not). Otherwise OK only when the pasted output actually SAYS so (an
    "OK" token, typically at the end of a "``-> OK``"/"``→ OK``" line) —
    never the default: a red kz check with no "OK" anywhere in the pasted
    text must read as failed, not silently "Yes" (the exact PR #756
    finding)."""
    value = _bullet(evidence, "kz check")
    if value is not None:
        v = value.strip().lower()
        if "unavailable" in v or "no .kohaerenz.yaml" in v or "no kohaerenz.yaml" in v:
            return None
        return bool(re.search(r"(?:^|[\s(>-])ok\b", v)) and "not ok" not in v

    # Older personal-skill template never had a dedicated kz-check bullet —
    # fall back to the pre-existing heuristic (a Context-brief section
    # present, without its own "kz brief unavailable: …" line) so a record
    # written before this field existed does not regress to `None`.
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

    failed_before_raw = _bullet_any(evidence, "Failing test before", "Red test before")
    passed_after_raw = _bullet_any(evidence, "Green after")
    failed_before = mask_text(t, extra) if (t := _after_arrow(failed_before_raw)) else None
    passed_after = mask_text(t, extra) if (t := _after_arrow(passed_after_raw)) else None

    return {
        "run_id": run.run_id,
        "status": status_m.group(1).lower() if status_m else None,
        "result_line": result_line,
        "tests": {"failed_before": failed_before, "passed_after": passed_after},
        "sabotage": _sabotage(evidence),
        "kz_ok": _kz_ok(evidence, sections),
        "review": _review(evidence),
        "bypass": _bypass(evidence),
        "operator_minutes": _first_int(numbers, _OPERATOR_MINUTES_RE, _OPERATOR_MINUTES_TABLE_RE),
        "helpers": _first_int(numbers, _HELPERS_RE, _HELPERS_TABLE_RE),
        "branch": run.spec.get("branch") if isinstance(run.spec, dict) else None,
        "pr_url": run.pr_url,
    }
