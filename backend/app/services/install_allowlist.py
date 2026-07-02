"""Source allowlist for install-requests.

Only sources matching these regex patterns are accepted. New sources require
a code change — Boss cannot bypass this via API. This is the trust boundary.
"""
import re
from typing import Final


class AllowlistError(ValueError):
    """Raised when an install source does not match any allowlist pattern."""


_PATTERNS: Final[dict[str, list[re.Pattern[str]]]] = {
    "skill": [
        # Local skills already in ~/.mc/skills/
        re.compile(r"^~/\.mc/skills/[a-z0-9_-]+$"),
        # Trusted GitHub orgs
        re.compile(r"^github:(anthropic|getcursor|google-labs-code|obra)/[a-z0-9_-]+$"),
    ],
    "plugin": [
        re.compile(r"^claude-plugins-official$"),
        re.compile(r"^github:(claude-plugins|anthropic)/[a-z0-9_-]+$"),
    ],
    "mcp": [
        re.compile(r"^npm:@modelcontextprotocol/server-[a-z0-9-]+$"),
        re.compile(r"^npm:@(supabase|vercel|cloudflare)/mcp-[a-z0-9-]+$"),
        # GitHub repos with "mcp" anywhere in the repo name (prefix or suffix
        # convention — e.g. `mcp-higgsfield` OR `higgsfield_ai_mcp`). The
        # operator's manual approval remains the real trust boundary.
        re.compile(r"^github:[a-z0-9_.-]+/[a-z0-9_.-]*mcp[a-z0-9_.-]*$"),
    ],
}


def validate_source(install_type: str, source: str) -> bool:
    """Return True if source matches an allowlist pattern, else raise AllowlistError."""
    if install_type not in _PATTERNS:
        raise AllowlistError(f"Unknown install type: {install_type!r}")
    if not source:
        raise AllowlistError("Source must not be empty")
    for pattern in _PATTERNS[install_type]:
        if pattern.match(source):
            return True
    raise AllowlistError(
        f"Source {source!r} does not match any allowlist for {install_type}"
    )


def list_allowed_sources(install_type: str) -> list[str]:
    """Return human-readable list of allowlist patterns (for UI / TOOLS.md)."""
    if install_type not in _PATTERNS:
        return []
    return [p.pattern for p in _PATTERNS[install_type]]
