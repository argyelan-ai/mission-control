"""Visual-Proof Evidence Validation — Phase 5B Browser Lane Hardening.

Validates that visual_proof tasks have real, verifiable evidence
from allowed browser-evidence paths only. No arbitrary file access.

Security:
- Only paths under ALLOWED_EVIDENCE_ROOTS are accepted
- Path traversal (../) is blocked via realpath normalization
- Symlink targets must resolve inside allowed roots
- Only image file extensions are accepted
"""
import logging
import os
import re

logger = logging.getLogger("mc.visual_proof")

# ── Configuration ─────────────────────────────────────────────────────────────

# Minimum file size for a valid screenshot (below = empty/black/error)
MIN_SCREENSHOT_BYTES = 5_000  # 5 KB

# Allowed image extensions for visual proof evidence
ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}

# Host home: in Docker the backend runs with HOME=/home/mcuser, but the
# evidence dirs are bind-mounted at the host path (HOME_HOST). Resolving via
# expanduser("~") would point at the non-existent container home, so follow the
# codebase idiom (config.py / agent_scoped.py) and prefer HOME_HOST.
_EVIDENCE_HOME = os.environ.get("HOME_HOST") or os.path.expanduser("~")

# Allowed root directories for evidence files.
# Only files whose realpath resolves under one of these roots are accepted.
# Canonical home is ~/.mc (decoupling migration 2026-06-01). The legacy
# ~/.openclaw roots stay whitelisted during the transition so evidence written
# by the external browser tool to either location keeps validating; they are
# removed in Stage 3 together with the legacy folder.
ALLOWED_EVIDENCE_ROOTS = [
    os.path.join(_EVIDENCE_HOME, ".mc/media/browser"),
    os.path.join(_EVIDENCE_HOME, ".mc/shared-artifacts"),
    os.path.join(_EVIDENCE_HOME, ".openclaw/media/browser"),
    os.path.join(_EVIDENCE_HOME, ".openclaw/shared-artifacts"),
]

# ── Path Extraction ───────────────────────────────────────────────────────────

# Match MEDIA: prefix or paths under known evidence directories
_MEDIA_PREFIX_PATTERN = re.compile(
    r"MEDIA:([^\s]+)",
    re.IGNORECASE,
)
_EVIDENCE_PATH_PATTERN = re.compile(
    r"((?:/[^\s]*|~)[^\s]*\.(?:png|jpg|jpeg|webp))",
    re.IGNORECASE,
)


def extract_evidence_paths(comments: list) -> list[str]:
    """Extract screenshot/media file paths from task comments.

    Only returns paths that match known evidence patterns.
    Paths are expanded (~) and normalized but NOT yet root-validated
    (that happens in validate_evidence_file).
    """
    paths = []
    for comment in comments:
        content = getattr(comment, "content", "") or ""

        # 1. MEDIA: prefixed paths (highest priority)
        for match in _MEDIA_PREFIX_PATTERN.finditer(content):
            raw = match.group(1).strip()
            expanded = os.path.expanduser(raw)
            paths.append(expanded)

        # 2. Bare paths with image extensions (fallback)
        for match in _EVIDENCE_PATH_PATTERN.finditer(content):
            raw = match.group(1).strip()
            if raw.startswith("MEDIA:"):
                continue  # Already captured above
            expanded = os.path.expanduser(raw)
            if expanded not in paths:
                paths.append(expanded)

    return paths


# ── File Validation ───────────────────────────────────────────────────────────

def _is_under_allowed_root(path: str) -> bool:
    """Check if the resolved real path is under an allowed evidence root.

    Uses os.path.realpath to resolve symlinks and normalize traversals.
    This prevents:
    - ../ path traversal attacks
    - symlinks pointing outside allowed roots
    - any file access outside designated evidence directories
    """
    try:
        real = os.path.realpath(path)
    except (OSError, ValueError):
        return False

    return any(
        real.startswith(os.path.realpath(root) + os.sep) or real == os.path.realpath(root)
        for root in ALLOWED_EVIDENCE_ROOTS
    )


def _has_allowed_extension(path: str) -> bool:
    """Check if the file has an allowed image extension."""
    _, ext = os.path.splitext(path)
    return ext.lower() in ALLOWED_EXTENSIONS


def validate_evidence_file(path: str) -> tuple[bool, str]:
    """Validate that an evidence file exists, is in an allowed root,
    has an allowed extension, and meets minimum size requirements.

    Returns (valid, reason).
    """
    # 1. Extension check (before any disk access)
    if not _has_allowed_extension(path):
        _, ext = os.path.splitext(path)
        return False, f"Unerlaubte Dateiendung: '{ext}'. Erlaubt: {', '.join(sorted(ALLOWED_EXTENSIONS))}"

    # 2. Root check (prevents arbitrary file access)
    if not _is_under_allowed_root(path):
        return False, (
            f"Pfad ausserhalb erlaubter Evidence-Roots: {path}. "
            f"Erlaubt: {', '.join(ALLOWED_EVIDENCE_ROOTS)}"
        )

    # 3. Existence check
    real = os.path.realpath(path)
    if not os.path.exists(real):
        return False, f"Datei existiert nicht: {path}"

    # 4. Size check
    size = os.path.getsize(real)
    if size < MIN_SCREENSHOT_BYTES:
        return False, (
            f"Datei zu klein ({size} Bytes, Minimum: {MIN_SCREENSHOT_BYTES}). "
            "Vermutlich leer oder fehlerhaft."
        )

    return True, f"OK ({size} Bytes)"


# ── Full Validation Pipeline ──────────────────────────────────────────────────

def validate_visual_proof_evidence(
    comments: list,
    expected_content: str | None = None,
    target_url: str | None = None,
) -> tuple[bool, list[str]]:
    """Full validation for visual_proof task evidence.

    Returns (valid, list_of_issues).
    """
    issues = []

    # 1. Extract paths from comments
    paths = extract_evidence_paths(comments)
    if not paths:
        issues.append(
            "Kein Screenshot-/MEDIA-Pfad in Evidence-Kommentaren gefunden. "
            "Bitte MEDIA:-Pfad aus openclaw browser screenshot referenzieren."
        )
        return False, issues

    # 2. Validate at least one file passes all checks
    any_valid = False
    for path in paths:
        valid, reason = validate_evidence_file(path)
        if valid:
            any_valid = True
            logger.debug("Evidence file valid: %s — %s", path, reason)
        else:
            issues.append(reason)

    if not any_valid:
        issues.insert(0, "Keine gueltige Evidence-Datei gefunden.")
        return False, issues

    # 3. Check expected_content mention in comments (soft check)
    if expected_content:
        all_content = " ".join(
            getattr(c, "content", "") or "" for c in comments
        ).lower()
        keywords = [w.lower() for w in expected_content.split() if len(w) > 3]
        if keywords:
            found = sum(1 for kw in keywords if kw in all_content)
            if found < len(keywords) // 3:
                issues.append(
                    f"expected_content nicht ausreichend in Evidence erwaehnt: "
                    f"'{expected_content[:60]}'"
                )

    return len(issues) == 0 or any_valid, issues
