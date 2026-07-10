"""Tolerant close-contract parsing — the SINGLE in-container source of truth.

Both enforcement sites of the omp/mc close contract share THIS logic:
  * the `mc` CLI  (mc_cli/commands.py: _validate_reflection / _cmd_finish), and
  * the omp bridge (docker/omp-bridge/bridge.py: sentinel_present /
    extract_reflection / validate_reflection).

bridge.py runs from ``/opt/omp-bridge`` while the mc CLI lives under
``/home/agent/.mc-cli`` in the omp image — they are NOT on the same sys.path,
so bridge.py cannot cleanly ``import mc_cli``. It therefore MIRRORS the small
normalizer below with a loud comment, and a repo-level parity test
(``docker/omp-bridge/tests/test_close_parity.py``) feeds a shared case matrix
through BOTH copies and asserts they agree byte-for-byte. Any future drift
fails that test loudly. This module is also the single place the canonical
German field names live for the whole in-container fleet — a drift-guard test
asserts they equal ``backend/app/constants.py`` REFLECTION_REQUIRED_FIELDS.

DESIGN INVARIANT: every tolerance here is ADDITIVE. A strict, canonical input
(exact 4 German ``## `` headers, ``TASK_COMPLETE`` alone as the last line,
>= 80 chars) parses byte-identical to the pre-tolerance behaviour. The
tolerance only rescues trivial local-model variance (lowercase sentinel,
``### `` headers, English headers, trailing punctuation, ``ü`` vs ``ue``) that
the backend gate would have accepted anyway (it checks existence + length, not
headers) but the old strict client/bridge gates wrongly rejected.
"""
from __future__ import annotations

import re
from typing import Optional

# Canonical German field names — the single source of truth in the container.
# MUST equal backend/app/constants.py REFLECTION_REQUIRED_FIELDS (drift-guard
# test: test_reflection.test_canonical_matches_backend_constants).
REFLECTION_REQUIRED_FIELDS = [
    "Was wurde gemacht",
    "Was hat funktioniert",
    "Was war unklar",
    "Lesson fuer Agent-Memory",
]
REFLECTION_MIN_CHARS = 80
SENTINEL = "TASK_COMPLETE"

# A harmless "closing rule" trailer (e.g. a Markdown horizontal rule) the model
# sometimes emits AFTER the sentinel — tolerated as the very last line so the
# sentinel still counts as the (second-to-)last meaningful line.
_TRAILER_RE = re.compile(r"^[-=*_`]{3,}$")

# A reflection header: 1-3 leading ``#`` then the field label (any case, an
# optional trailing colon). Level > 3 is NOT a header (stays body content).
_HEADER_RE = re.compile(r"^\s{0,3}#{1,3}\s+(.+?)\s*$")

# English aliases -> canonical German (so the backend + memory pipeline always
# see canonical German headers, i.e. we NORMALIZE the block, not just accept it).
_ENGLISH_ALIASES = {
    "what was done": "Was wurde gemacht",
    "what worked": "Was hat funktioniert",
    "what was unclear": "Was war unklar",
    "lesson for agent memory": "Lesson fuer Agent-Memory",
}


def _fold(s: str) -> str:
    """Normalize a header label for tolerant matching.

    lowercases, folds German umlauts (ü->ue, ö->oe, ä->ae, ß->ss), treats
    hyphens as spaces, collapses whitespace, drops a trailing colon.
    """
    s = s.strip().rstrip(":").strip().lower()
    for a, b in (("ü", "ue"), ("ö", "oe"), ("ä", "ae"), ("ß", "ss")):
        s = s.replace(a, b)
    s = s.replace("-", " ")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


# folded label -> canonical German field
_CANON_BY_FOLD = {}
for _f in REFLECTION_REQUIRED_FIELDS:
    _CANON_BY_FOLD[_fold(_f)] = _f
for _k, _v in _ENGLISH_ALIASES.items():
    _CANON_BY_FOLD[_fold(_k)] = _v


def match_header(line: str) -> Optional[str]:
    """Return the canonical German field name if ``line`` is a recognised
    reflection header (any level 1-3, any case, EN alias, ü/ue, trailing colon),
    else ``None``."""
    m = _HEADER_RE.match(line)
    if not m:
        return None
    return _CANON_BY_FOLD.get(_fold(m.group(1)))


def _is_sentinel_line(line: str) -> bool:
    """True if ``line`` is a TASK_COMPLETE sentinel, tolerating case, wrapping
    markdown (``**``/backticks/``__``) and trailing punctuation."""
    s = line.strip()
    if not s:
        return False
    # strip wrapping emphasis / code markers, then trailing punctuation.
    s = s.strip("*`_~ ")
    s = re.sub(r"[.\!:;,\s]+$", "", s)
    return s.upper() == SENTINEL


def sentinel_present(text: str) -> bool:
    """Anti-echo: the sentinel counts only as the LAST meaningful line (alone
    on its line, modulo markdown/punctuation), OR the second-to-last when the
    final line is a harmless trailer (``---`` / ``***``)."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False
    if _is_sentinel_line(lines[-1]):
        return True
    if len(lines) >= 2 and _TRAILER_RE.match(lines[-1].strip()) and _is_sentinel_line(lines[-2]):
        return True
    return False


def _normalise_header_line(canon: str) -> str:
    """Render a recognised header at the canonical ``## <German>`` form."""
    return f"## {canon}"


def extract_reflection(text: str) -> Optional[str]:
    """Slice the reflection block from the FIRST recognised header to the line
    before the sentinel, with all recognised headers normalised to canonical
    German ``## `` form. Returns ``None`` if no recognised header is present.
    """
    lines = text.splitlines()
    start = -1
    for i, line in enumerate(lines):
        if match_header(line) is not None:
            start = i
            break
    if start == -1:
        return None
    kept: list[str] = []
    for line in lines[start:]:
        if _is_sentinel_line(line):
            break
        canon = match_header(line)
        if canon is not None:
            kept.append(_normalise_header_line(canon))
        else:
            kept.append(line)
    return "\n".join(kept).strip()


def normalize_reflection(text: str) -> str:
    """Rewrite recognised headers in a full reflection body to canonical German
    ``## `` form (idempotent on already-canonical input). Non-header lines are
    left untouched. Used by ``mc finish`` so the POSTed reflection (and thus the
    memory pipeline) always sees canonical German headers."""
    out: list[str] = []
    for line in text.splitlines():
        canon = match_header(line)
        out.append(_normalise_header_line(canon) if canon is not None else line)
    return "\n".join(out)


def validate_reflection(block: Optional[str]) -> bool:
    """True iff ``block`` carries all 4 canonical German headers and is at least
    ``REFLECTION_MIN_CHARS`` long. Expects a block already run through
    ``extract_reflection`` / ``normalize_reflection`` (headers canonicalised)."""
    if not block:
        return False
    if len(block) < REFLECTION_MIN_CHARS:
        return False
    return all(f"## {f}" in block for f in REFLECTION_REQUIRED_FIELDS)


def missing_fields(text: str) -> list[str]:
    """Return the canonical fields NOT present in ``text`` (after tolerant
    header recognition). Empty list == all four present."""
    present = set()
    for line in text.splitlines():
        canon = match_header(line)
        if canon is not None:
            present.add(canon)
    return [f for f in REFLECTION_REQUIRED_FIELDS if f not in present]
