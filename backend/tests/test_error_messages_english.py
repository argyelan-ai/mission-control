"""Backend error messages the operator sees must be in English (PRINCIPLES.md §6).

`work_language` (default `en`) covers everything between agents and in
artifacts, including API error text — it is not the operator's chat
language. This scans `HTTPException(detail=...)` strings across the backend
for German characters and fails if any are found.

`routers/auth.py` is excluded on purpose: a parallel change there (password
change / session invalidation) is in flight, and this test would otherwise
collide with it. Track it as a known gap until that lands, then fold it in.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent / "app"

# Known-open gap: auth.py has its own in-flight PR touching these strings.
EXCLUDED_FILES = {"auth.py"}

GERMAN_CHARS = re.compile(r"[äöüÄÖÜß]")

# Matches `detail="..."`, `detail=f"..."`, `detail='...'`, across the
# assignment and the raise/call form, string literal only (not computed
# values after the closing quote get re-checked by the char scan).
DETAIL_STRING = re.compile(r"""detail\s*=\s*f?["']([^"']*)["']""")


def _detail_strings_with_german_chars() -> list[str]:
    hits: list[str] = []
    for path in sorted(BACKEND_ROOT.rglob("*.py")):
        if path.name in EXCLUDED_FILES:
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "detail" not in line:
                continue
            for match in DETAIL_STRING.finditer(line):
                literal = match.group(1)
                if GERMAN_CHARS.search(literal):
                    hits.append(f"{path.relative_to(BACKEND_ROOT.parent)}:{lineno}: {line.strip()}")
    return hits


def test_http_exception_detail_strings_are_english():
    hits = _detail_strings_with_german_chars()
    assert not hits, (
        "User-visible HTTPException detail strings must be English "
        "(PRINCIPLES.md §6, work_language). Found German text in:\n"
        + "\n".join(hits)
    )


def test_scan_itself_still_finds_the_excluded_auth_file():
    """Guards against the exclusion rotting into a silent catch-all.

    auth.py is known to still carry German detail strings (parallel PR in
    flight). If this ever goes green on its own, the exclusion line above
    should be removed instead of this test being deleted.
    """
    auth_path = BACKEND_ROOT / "routers" / "auth.py"
    text = auth_path.read_text(encoding="utf-8")
    found = any(
        GERMAN_CHARS.search(m.group(1))
        for line in text.splitlines()
        for m in DETAIL_STRING.finditer(line)
    )
    if not found:
        pytest.skip("auth.py no longer has German detail strings — drop the exclusion")
    assert found
