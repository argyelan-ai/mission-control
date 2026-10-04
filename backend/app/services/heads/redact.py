"""Masking for anything read out of a head run folder before it leaves the
API — head transcripts, ``head.log``, run records (docs/decisions/085
Nachtrag 2026-10-04 §4: the chat view only ever reads files, but a file a
head wrote can contain anything the head's tool calls touched).

Two layers, always both:

1. ``app.log_redaction.redact_secrets`` + ``_BARE_TOKENS`` (moved here from
   ``routers/heads.py``, where ``mask_log`` used to own them — one mask, one
   owner) catch secrets by SHAPE (``gh[pousr]_…``, ``sk-…``, bearer tokens,
   ``key=``/``token=`` query params).
2. ``head_env_values`` catches secrets by VALUE: whatever the operator (or
   a cloud recipe) put in this run's own ``head.env`` — a provider API key
   that carries no recognisable prefix is invisible to layer 1, but it is
   a short, known string the backend already has on disk for this exact
   run. ``anhang.md`` section D's stichprobe found both kinds in real
   transcripts (a 6-10 char OAuth-token fragment, and a bare key= value).

Never log or return a value found by ``head_env_values`` anywhere except as
input to ``mask_text``/``mask_tree`` — the whole point is that it never
reaches a response body unredacted.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.log_redaction import redact_secrets
from app.services.heads.files import read_head_file

REDACTED = "<redacted>"

#: Tokens that appear BARE in a harness transcript (no ``key=``/``Bearer``
#: prefix for ``redact_secrets`` to anchor on) — moved from
#: ``routers/heads.py`` (``mask_log`` used only this one call site).
_BARE_TOKENS = re.compile(
    r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|sk-ant-[A-Za-z0-9_-]{20,})\b"
)

#: A value shorter than this is too common (model slugs, short flags) to
#: redact just for appearing in ``head.env`` — matches the bauplan's "≥ 8
#: Zeichen" rule and avoids masking e.g. a one-word model family name.
_MIN_EXTRA_LEN = 8


def mask_text(text: str, extra: tuple[str, ...] = ()) -> str:
    """``redact_secrets`` + bare-token shapes + every ``extra`` value this
    run's own ``head.env`` carries (longer than a few characters)."""
    out = _BARE_TOKENS.sub(REDACTED, redact_secrets(text))
    for value in extra:
        if value and len(value) >= _MIN_EXTRA_LEN:
            out = out.replace(value, REDACTED)
    return out


def mask_tree(obj: Any, extra: tuple[str, ...] = ()) -> Any:
    """``mask_text`` recursively over a dict/list/str structure — the shape
    returned by ``transcript_chat.read_history`` (events, nested ``detail``
    dicts, tool results). Non-string leaves (ints, bools, ``None``) pass
    through unchanged; only string content can carry a secret."""
    if isinstance(obj, str):
        return mask_text(obj, extra)
    if isinstance(obj, dict):
        return {k: mask_tree(v, extra) for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask_tree(v, extra) for v in obj]
    return obj


def head_env_values(run) -> tuple[str, ...]:
    """The values (never the keys) of this run's own ``head.env`` —
    whatever provider key or weak ``GH_TOKEN`` the head itself was given.

    Reads the file the same safe way every other head-writable file is read
    (``files.read_head_file``: ``O_NOFOLLOW``, regular files only) — even
    though ``head.env`` is backend-written, not head-written, the same
    symlink defence costs nothing and keeps one code path. Blank lines and
    comments are skipped; a value is only ever RETURNED to be masked away,
    never printed, logged or placed in a response on its own.
    """
    text = read_head_file(run.folder / "head.env", 8_000) or ""
    values: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        _, _, value = line.partition("=")
        value = value.strip()
        if value:
            values.append(value)
    return tuple(values)
