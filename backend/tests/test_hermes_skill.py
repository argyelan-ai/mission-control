"""Phase 25 — Hermes mission-control SKILL.md frontmatter + readonly tests.

Validates the agentskills.io-conformant skill written by Plan 25-03.
Tests are RED until 25-03 deploys the skill files.
"""
import os
import re
import stat
from pathlib import Path

import pytest
import yaml  # PyYAML — already a transitive dep via FastAPI/SQLModel test extras


# Use HOME_HOST when running inside a container (CLAUDE.md: HOME_HOST pattern)
_HOME = Path(os.environ.get("HOME_HOST") or os.path.expanduser("~"))
SKILL_DIR = _HOME / ".hermes" / "skills" / "mission-control"
SKILL_MD = SKILL_DIR / "SKILL.md"
REF_API = SKILL_DIR / "references" / "api-endpoints.md"
REF_FMT = SKILL_DIR / "references" / "comment-format.md"

REQUIRED_FRONTMATTER_KEYS = {
    "name",
    "description",
    "version",
    "author",
    "tags",
    "related_skills",
}
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")

# The skill files are a DEPLOYED host artifact (written by Plan 25-03), not
# part of the repo. On machines without a provisioned Hermes (fresh clones,
# CI) these tests have nothing to validate — skip instead of failing.
pytestmark = pytest.mark.skipif(
    not SKILL_MD.exists(),
    reason=f"Hermes skill not deployed on this host ({SKILL_MD} missing)",
)


def _read_frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        pytest.fail(f"{path}: no YAML frontmatter (must start with '---\\n')")
    end = text.find("\n---\n", 4)
    if end == -1:
        pytest.fail(f"{path}: frontmatter not closed with '\\n---\\n'")
    return yaml.safe_load(text[4:end]) or {}


def test_skill_md_exists():
    assert SKILL_MD.exists(), f"missing: {SKILL_MD}"


def test_yaml_valid():
    fm = _read_frontmatter(SKILL_MD)
    assert isinstance(fm, dict), "frontmatter must parse to a dict"


def test_frontmatter_complete():
    fm = _read_frontmatter(SKILL_MD)
    missing = REQUIRED_FRONTMATTER_KEYS - set(fm.keys())
    assert not missing, f"missing required keys: {missing}"
    for k in REQUIRED_FRONTMATTER_KEYS:
        assert fm[k], f"key '{k}' is empty/falsy: {fm[k]!r}"


def test_frontmatter_values():
    fm = _read_frontmatter(SKILL_MD)
    assert fm["name"] == "mission-control", f"name must be 'mission-control', got {fm['name']!r}"
    assert isinstance(fm["tags"], list) and len(fm["tags"]) >= 2, "tags must be a non-trivial list"
    assert isinstance(fm["related_skills"], list), "related_skills must be a list"
    assert SEMVER_RE.match(str(fm["version"])), f"version not semver: {fm['version']!r}"


def test_references_present():
    assert REF_API.exists(), f"missing: {REF_API}"
    assert REF_FMT.exists(), f"missing: {REF_FMT}"


def test_chmod_readonly():
    """D-16: chmod 444 protects against Hermes' self-improving-loop overwriting our skill."""
    for p in (SKILL_MD, REF_API, REF_FMT):
        mode = stat.S_IMODE(p.stat().st_mode)
        assert mode == 0o444, f"{p}: expected mode 0o444, got {oct(mode)}"


def test_comment_format_examples_present():
    text = REF_FMT.read_text(encoding="utf-8")
    # Positive examples: at least 2 occurrences of literal 'Update:' AND 'Evidence:' AND 'Next:'
    assert text.count("Update:") >= 2, "need >=2 'Update:' marker (positive examples)"
    assert text.count("Evidence:") >= 2, "need >=2 'Evidence:' marker (positive examples)"
    assert text.count("Next:") >= 2, "need >=2 'Next:' marker (positive examples)"
    # Negative examples: marker for anti-pattern section
    text_lower = text.lower()
    assert ("anti-pattern" in text_lower) or ("anti pattern" in text_lower) or ("do not" in text_lower), \
        "comment-format.md must include an anti-pattern / 'do not' section"
