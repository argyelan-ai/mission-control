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

The same three prose fields also go through ``_strip_backticks``: the card
has no markdown renderer, so a run record's own `` `inline code` `` or
``**bold**``/``__bold__`` wrapper would otherwise show as literal backtick/
asterisk/underscore characters (review finding on PR #756 rounds 3 and 4).
The two ``tests`` strings additionally go through ``_shorten_to_key_result``,
which cuts a bullet's remainder at its first aside marker (``;``, `` — ``,
``(``) and strips a surviving leading/trailing quote character — a real
"Green after" bullet's full explanation is ~500 characters, unreadable as a
one-line fact on the phone; ``result_line`` is left at its full sentence
length (the template already asks for "1–2 sentences" there), only its
backticks/bold markers are removed.
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


#: A handful of literal placeholder WORDS a head writes instead of the
#: template's own ``<…>`` bracket shape — "pending" (the ``## Result``/
#: ``## Evidence`` scaffold a run record starts with, before the head has
#: written anything real), "tbd"/"todo" (same idea, spelled differently) and
#: a bare dash ("nothing to say yet"). Exact, case-insensitive matches of
#: the WHOLE (stripped) value only — never a substring test, which would
#: also flag a real bullet that merely CONTAINS one of these words.
_PLACEHOLDER_WORDS = {"pending", "tbd", "todo", "-", "–", "—"}


def _is_placeholder(value: str) -> bool:
    """``<…>`` — the template's own unfilled-bullet shape (``- Review:
    <fresh helper PASSED/FAILED | self-review>``, ``- Sabotage check: <what
    was broken> → red · restored → green``). A run record that still carries
    this text never actually filled the bullet in, so it must read exactly
    like the bullet being absent (``None``), never like a real answer
    (review finding on PR #756 round 3: an unfilled ``Sabotage check``
    placeholder parsed as ``sabotage=True``, an unfilled ``Review``
    placeholder as ``review='helper'``).

    Also ``_PLACEHOLDER_WORDS`` — a real head writes "pending" into a
    freshly-scaffolded ``## Result``/``## Evidence`` bullet at step 0, not
    the template's own ``<…>`` shape, so the check above alone let it
    through as if it were real content (review finding on PR #756 round 4,
    reproduced on real run 6bcf49ea: a ``- pending`` Result bullet and a
    ``- Green after: pending`` Evidence bullet both survived into the
    summary card as believable-looking facts)."""
    return value.startswith("<") or value.strip().lower() in _PLACEHOLDER_WORDS


def _bullet(section: str, label: str) -> str | None:
    """The remainder of a ``- <Label>: <value>`` bullet (case-insensitive
    label), or ``None`` when that bullet is not there at all, or is still the
    template's own unfilled ``<…>`` placeholder. Matches the exact
    "``- Field: value``" shape every template bullet uses — a record that
    instead free-writes the result as plain prose (no leading ``- X:``) is a
    record this function correctly does not understand, rather than one it
    mis-reads. Anchored at the START of the bullet (``^-\\s*Label\\s*:``),
    never a bare "contains" search — a label that is merely MENTIONED inside
    an earlier, unrelated bullet's free text (e.g. "``- Quota: … no
    helpers``" ahead of the real "``- Helpers: 2``" bullet) must never be
    mistaken for the labelled bullet itself (review finding on PR #756)."""
    m = re.search(rf"(?mi)^-[ \t]*{re.escape(label)}[ \t]*:[ \t]*(.+)$", section)
    if not m:
        return None
    value = m.group(1).strip()
    if not value or _is_placeholder(value):
        return None
    return value


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


def _strip_backticks(value: str) -> str:
    """Markdown inline-code backticks (`` `x` ``, or doubled ```` ``x`` ````
    when ``x`` itself contains a backtick) read as literal backtick
    characters once a value is lifted out of the record and printed as a
    plain-text fact — the card has no markdown renderer for these one-line
    facts (review finding on PR #756 round 3: a real card showed
    "`` `backend/app/services/pr_merge_monitor.py` ``" and
    "```` ``changed_by=\"system\"`` ````" verbatim, backticks and all).
    Removing every backtick is enough: none of this module's fields depend
    on the backtick itself to stay readable once unquoted.

    Also strips a markdown bold wrapper (``**x**``/``__x__``) around the
    WHOLE (stripped) value — never a blanket ``value.replace("**", "")``,
    which would also eat a literal double-underscore inside real test
    output that is not markdown at all (``tests/__init__.py``,
    ``__pycache__``, a parametrized id like ``test_x[a__b]``). Review
    finding on PR #756 round 4: a real, fully-bolded "Green after" bullet
    printed "**9305 passed, 24 failed, 50 skipped**" verbatim — the
    backtick-only strip left its asterisks untouched."""
    value = value.replace("`", "")
    stripped = value.strip()
    for marker in ("**", "__"):
        if len(stripped) > 2 * len(marker) and stripped.startswith(marker) and stripped.endswith(marker):
            stripped = stripped[len(marker):-len(marker)].strip()
    return stripped


#: After the arrow, a real run's bullet often keeps going — a parenthetical
#: aside, a second clause after an em dash, a second sentence after a
#: semicolon (every one of these appears in a real, scrubbed run record's
#: "Green after" bullet). None of that is the "key result" a one-line fact
#: card should show; left in, it printed as ~500 characters across 10 lines
#: on the phone (review finding on PR #756 round 3). Cut at the FIRST of
#: these three markers the bullet actually uses.
_ASIDE_MARKERS = (";", "—", "(")


def _shorten_to_key_result(value: str) -> str | None:
    cut = len(value)
    for marker in _ASIDE_MARKERS:
        idx = value.find(marker)
        if idx != -1:
            cut = min(cut, idx)
    cleaned = _strip_backticks(value[:cut]).strip()
    # A quote character survives at either end of a SHORTENED fact even when
    # the source bullet never meant to quote it at all — the aside-marker
    # cut above can slice a balanced pair down to just its opening quote
    # (real runs 5a7269a3/708843ce: `"12 keys`, `"0 new, 96 known…`), and
    # even a genuinely balanced pair (`"11 failed"`) adds nothing but noise
    # once it is the card's own one-line fact rather than a quoted aside
    # inside a longer sentence (review finding on PR #756 round 4 — visible
    # in the round-3 screenshots as literal quote marks around "11 failed").
    # `str.strip` removes either side independently, so a lone opening quote
    # with no closing partner is handled the same way as a balanced pair.
    cleaned = cleaned.strip("\"'")
    return cleaned or None


def _after_arrow(value: str | None) -> str | None:
    """"`<command>` → <key line>" → just the key line, split at the FIRST
    arrow (not the last): a real run's bullet often carries more than one
    "``<command>`` → ``<result>``" pair on the same line (e.g. a quick
    command's result, then "full suite `...` → ..." after it) — splitting
    on the LAST arrow silently discarded the actual first result and kept
    only a trailing aside (review finding on PR #756, reproduced on a real
    "Green after" bullet). A bullet with no arrow at all is returned whole
    (some runs write the result directly, no arrow needed) rather than
    discarded. The key line itself is then shortened to its first clause and
    stripped of backticks (``_shorten_to_key_result``) — the full remainder
    of a real bullet is often a run-on explanation, not the result itself."""
    if value is None:
        return None
    rest = value.split("→", 1)[-1].strip()
    if not rest or _is_placeholder(rest):
        return None
    return _shorten_to_key_result(rest)


#: Bullet values the template itself uses for "this step was skipped" —
#: distinct from the bullet being ABSENT (→ ``None``, unknown) which is the
#: far more common case for a run that never reached this step. Matched as a
#: regex anchored at the START of the (stripped, lowercased) value, so
#: whatever punctuation a real bullet puts after the verdict — the
#: template's own `` — ``, or a bare ``;``/``,``/``:``/``(`` a real run
#: wrote instead — ends the word/clause just as well.
_SABOTAGE_NEGATIVE_RE = re.compile(r"^(n/?a|none|no|skipped|not (?:performed|done))\b")


def _sabotage(section: str) -> bool | None:
    """``False`` only when the bullet's own LEADING word or clause says no
    sabotage probe ran; ``True`` for everything else a present, non-
    placeholder bullet says (an empty string cannot reach here — ``_bullet``/
    ``_bullet_any`` already turned a missing or ``<…>``-placeholder bullet
    into ``None`` above, so a present value always has non-blank text).

    Round 3 matched the whole value against a fixed word list only after
    splitting on the template's own `` — `` separator — so
    ``"n/a (docs-only change)"``, ``"none; docs only"``, ``"skipped, no code
    change"`` and ``"N/A: docs only"`` all fell through to "not an exact
    match" → ``True``, even though every one of them explicitly says no
    sabotage probe ran (review finding on PR #756 round 4, probed directly
    against a real bullet of each shape). A regex anchored at the start
    catches the leading word regardless of what punctuation — or none —
    follows it."""
    value = _bullet_any(section, "Sabotage check", "Sabotage probe")
    if value is None:
        return None
    return not _SABOTAGE_NEGATIVE_RE.match(value.strip().lower())


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


def _kz_ok(evidence: str) -> bool | None:
    """The in-repo template's own ``- kz check: …`` bullet (Evidence
    section) is the ONLY signal — "a pasted output, last line e.g. 'OK (4
    skipped)' | red lines + 'Known limits' in the PR | no .kohaerenz.yaml |
    unavailable: …" (`head-AGENTS.md`'s own template line). No bullet at all
    → ``None``, full stop.

    This used to fall back to the unrelated "kz brief: …" line in the
    Context brief when Evidence had no ``kz check:`` bullet (review finding
    on PR #756 round 1: that line answers "was step 0's research brief
    available", not "did the push-time kz check pass"). Round 2 narrowed the
    fallback instead of removing it, which still guessed ``True`` for every
    run that never reached step 6 at all — aborted, failed, question-open or
    still running, as long as a Context brief existed without the words "kz
    brief unavailable" (review finding on PR #756 round 2, reproduced live on
    a real ``running`` run with an empty Evidence section). Extracted, never
    guessed: a record that never recorded a kz check has nothing to report.

    ``unavailable``/``no .kohaerenz.yaml`` → ``None`` (nothing to call ok or
    not). Otherwise OK only when the pasted output actually SAYS so (an
    "OK" token, typically at the end of a "``-> OK``"/"``→ OK``" line) —
    never the default: a red kz check with no "OK" anywhere in the pasted
    text must read as failed, not silently "Yes" (the exact PR #756
    finding)."""
    value = _bullet(evidence, "kz check")
    if value is None:
        return None
    v = value.strip().lower()
    if "unavailable" in v or "no .kohaerenz.yaml" in v or "no kohaerenz.yaml" in v:
        return None
    return bool(re.search(r"(?:^|[\s(>-])ok\b", v)) and "not ok" not in v


def build_summary(run: Any) -> dict[str, Any] | None:
    """``run.run_record_text`` → the dict ``docs/specs/head-launcher.md`` §7
    documents for ``GET /heads/{run_id}/summary``. Caller (the router)
    already turned "no run record at all" into a 404 before this is called —
    every field here can still be ``None``, which is the record being
    legitimately silent about that one fact, not an error.

    Returns ``None`` — the same "nothing to show yet" the router already
    gives a run with no record at all — when the record's own ``Status:``
    line still says ``running``: a head's wrapper writes the record's
    scaffold (frontmatter, title, this Heartbeat/Status line) at step 0,
    before it has written a single real bullet, so a record caught at
    exactly that moment carries nothing but placeholder text. Every
    ``_is_placeholder``/``_PLACEHOLDER_WORDS`` check above already keeps an
    individual unfilled field out of its own dict entry, but the counters
    (``operator_minutes``/``helpers``) are legitimately numeric — a
    scaffolded ``- Helpers: 0`` is indistinguishable, field by field, from a
    run that genuinely used zero helpers. ``Status: running`` is the one
    signal that says the WHOLE record is still just that scaffold (review
    finding on PR #756 round 4, reproduced on real run 6bcf49ea: ended
    `failed`/`no_progress` before ever reaching a later step, so its record
    was still exactly this scaffold — the card showed a believable-looking
    "pending" result line and "0"/"0" counters for a run that did nothing).
    A record with NO ``Status:`` line at all is unaffected — that is the
    "no sections at all" shape every field already reports as ``None`` on
    its own, not this scaffold-specific case."""
    text = run.run_record_text or ""
    sections = _sections(text)
    result = sections.get("result", "")
    evidence = sections.get("evidence", "")
    numbers = sections.get("numbers", "")

    extra = head_env_values(run)
    status_m = _STATUS_RE.search(text)
    if status_m and status_m.group(1).lower() == "running":
        return None

    # The Result section's own first bullet is the one-sentence summary the
    # template asks for ("<What is different now, in plain sentences>") —
    # the FIRST ``- …`` line, not a labelled one (unlike every other field
    # here, this section has no fixed label to search for).
    m = re.search(r"(?m)^-[ \t]*(.+)$", result)
    result_raw = m.group(1).strip() if m else None
    if result_raw and _is_placeholder(result_raw):
        result_raw = None
    if result_raw:
        result_raw = _strip_backticks(result_raw).strip() or None
    result_line = mask_text(result_raw, extra) if result_raw else None

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
        "kz_ok": _kz_ok(evidence),
        "review": _review(evidence),
        "bypass": _bypass(evidence),
        "operator_minutes": _first_int(numbers, _OPERATOR_MINUTES_RE, _OPERATOR_MINUTES_TABLE_RE),
        "helpers": _first_int(numbers, _HELPERS_RE, _HELPERS_TABLE_RE),
        "branch": run.spec.get("branch") if isinstance(run.spec, dict) else None,
        "pr_url": run.pr_url,
    }
