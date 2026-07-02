"""Shared constants for vault services.

Centralises filtering rules that must stay in sync across vault_watcher,
vault_lint, and any future vault service that walks the filesystem.
"""

# Prefixes (relative to vault root) that all vault services must skip.
# Keeping this in one place prevents silent false-positives when a new
# excluded directory is added to the watcher but forgotten in the linter.
EXCLUDED_PREFIXES: tuple[str, ...] = (
    "_inbox/",
    "_conflicts/",
    "_rejected/",
    "_lint/",
    # Soft-deleted notes live in _trash/ until purged. The DELETE handler
    # in routers/vault.py moves the file there + drops the FTS5 row by
    # the OLD path. Without excluding _trash/ here, the watcher's on_moved
    # event re-indexed the file under its NEW _trash/<ts>-foo.md path —
    # the deleted note then re-appeared in the list view (under a path
    # the GET endpoint refuses to open, producing 404s on click). See
    # 2026-05-16 incident.
    "_trash/",
    ".git/",
    ".obsidian/",
    ".mc_index.db",  # SQLite FTS index sentinel — skip the DB file itself
    # Phase A vault-as-brain: attachments/ holds hardlinked deliverable
    # assets (PDFs, images, audio, and the occasional .md whose content is
    # a deliverable — not a wrapper note). The watcher must NOT validate
    # these as notes, otherwise every markdown deliverable gets quarantined
    # as "missing required field". Wrappers themselves live under
    # agents/<slug>/deliverables/ and stay indexed.
    "attachments/",
)
