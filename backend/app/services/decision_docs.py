"""Ground truth for "what is a decision document" (ADR-Gate, 2026-09-16 incident).

Incident: PR #602 added `docs/decisions/084-omp-acp-harness-property.md`
(supersedes ADR-081) and was squash-merged on a green *code* verdict while
operator approval of the ADR *text* was still pending. The rule "a PR touching
a decision document needs explicit operator approval" existed only as prose.

Three questions this module answers, each with one definition:

  1. Is this path a *candidate*?      → `decision_doc_number(path)`
  2. Does this text *carry* the ADR signature? → `adr_signature_number(content)`
  3. Is this file a decision document? → `is_decision_doc(content, path)`

The discriminator is the **body signature** (`# ADR-NNN` as the first
non-empty line), NOT the path. Falling back to the filename would be wrong in
two directions: an ADR can be relocated outside `docs/decisions/` without
losing its meaning, and a file *inside* `docs/decisions/` is not automatically
a decision document (`README.md`, `_template.md` are index/scaffold, and a
half-written draft without the heading is not yet a decision).

Measured ground truth at the incident revision (2026-09-16): 85 `.md` files
under `docs/decisions/`, of which 83 carry the signature. The path rule and
the content rule currently select **the same 83**, but the path rule is NOT a
filter: the gate asks `is_decision_doc` about every changed file, because a
`# ADR-NNN` outside `docs/decisions/` (or under a non-`.md` name) also carries
the rule's meaning. The path matters in one further case — a file *named* like
an ADR whose signature is gone (heading rewritten, or renamed out of the
folder) counts as a change, the same as a deletion. Zero documents with the
signature exist anywhere else in the repo today, so keying on the body is not
lossy; it is the constraint that keeps the rule correct if a decision document
ever moves.

An unreadable/deleted candidate still counts as a decision document: deleting
an ADR is exactly as decision-relevant as adding one.
"""

from __future__ import annotations

import re
from pathlib import Path

# Repo-root-relative location of the decision documents.
DECISION_DOCS_DIR = "docs/decisions"

# `NNN-slug.md` — exactly three digits (the numbering used by 001..084).
DECISION_DOC_NAME_RE = re.compile(r"^(\d{3})-[\w.\-]+\.md$")

# The body signature: `# ADR-084 ...` as the first non-empty line.
ADR_HEADING_RE = re.compile(r"^#\s*ADR-(\d{3})\b")

# Directories never worth walking when scanning for decision documents.
_SCAN_IGNORED_DIRS = frozenset({
    ".git", "node_modules", ".venv", "venv", "__pycache__",
    "dist", "build", ".next", "coverage", ".mypy_cache", ".ruff_cache",
})


def normalize_repo_path(path: str) -> str:
    """PR file lists arrive as `docs/decisions/084-....md` (no leading `./`).

    A PR created from a workspace, however, can carry absolute paths when the
    agent ran the CLI outside its clone (`/workspace/repo/docs/decisions/...`).
    Strip the noise so one comparison works for both shapes.
    """
    cleaned = (path or "").strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.lstrip("/")


def decision_doc_number(path: str) -> str | None:
    """The `NNN` of a decision-document *path*, or None if the path is not one.

    Candidate test only — necessary, NOT sufficient. `docs/decisions/README.md`
    and `docs/decisions/999-bad.md` are candidates by path and still fail
    `is_decision_doc` if their body carries no signature.
    """
    candidate = normalize_repo_path(path)
    # Keep only the final two components: `docs/decisions/<name>`.
    parts = candidate.split("/")
    if len(parts) < 2:
        return None
    if "/".join(parts[-2:-1]) != "decisions":
        return None
    match = DECISION_DOC_NAME_RE.match(parts[-1])
    if not match:
        return None
    return match.group(1)


def adr_signature_number(content: str) -> str | None:
    """The `NNN` of `# ADR-NNN` on the first non-empty line, else None."""
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = ADR_HEADING_RE.match(stripped)
        return match.group(1) if match else None
    return None


def is_decision_doc(content: str | None, path: str) -> bool:
    """The full test. `content=None` means "could not be read at this revision".

    Body signature is authoritative; the filename only has to *agree* with it
    when both are present. A heading number that contradicts the filename
    (`085-x.md` starting with `# ADR-084`) is a broken document, not a decision
    document — treating it as one would let a rename silently re-target an
    approval.
    """
    path_number = decision_doc_number(path)
    if content is None:
        # Deleted or unreadable: still decision-relevant (conservative).
        return path_number is not None
    signature = adr_signature_number(content)
    if signature is None:
        return False  # no signature → not a decision document, whatever the path
    if path_number is None:
        return True  # signature at a non-standard path → still a decision document
    return signature == path_number


def scan_decision_docs(repo_root: Path) -> dict[str, str]:
    """Map repo-relative path → `NNN` for every real decision document.

    Walks the whole repo, not just `docs/decisions/`: the body signature is the
    discriminator, so a relocated decision document must still be found. Used
    by tests/audits as the ground-truth population.
    """
    root = Path(repo_root)
    found: dict[str, str] = {}
    for candidate in root.rglob("*.md"):
        parts = candidate.relative_to(root).parts
        if any(part in _SCAN_IGNORED_DIRS for part in parts):
            continue
        rel = candidate.relative_to(root).as_posix()
        try:
            content = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            content = None
        if is_decision_doc(content, rel):
            found[rel] = decision_doc_number(rel) or (adr_signature_number(content or "") or "")
    return found


def find_repo_root(start: Path | None = None) -> Path:
    """Walk up from `start` (default: this file) to the repo root."""
    current = (start or Path(__file__)).resolve()
    if current.is_file():
        current = current.parent
    for parent in [current, *current.parents]:
        if (parent / DECISION_DOCS_DIR).is_dir() or (parent / ".git").exists():
            return parent
    return current
