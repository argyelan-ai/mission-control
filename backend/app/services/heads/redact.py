"""Masking for anything read out of a head run folder before it leaves the
API — head transcripts, ``head.log``, run records (docs/decisions/085
Nachtrag 2026-10-04 §4: the chat view only ever reads files, but a file a
head wrote can contain anything the head's tool calls touched).

Two layers, always both:

1. ``app.log_redaction.redact_secrets`` + ``_BARE_TOKENS`` (moved here from
   ``routers/heads.py``, where ``mask_log`` used to own them — one mask, one
   owner) catch secrets by SHAPE (``gh[pousr]_…``, ``sk-…``, bearer tokens,
   ``key=``/``token=`` query params, an env-style ``…_API_KEY=``/``…_TOKEN=``/
   ``…_SECRET=`` assignment, an ``x-api-key:`` header, a bare ``hf_…`` token,
   a bare JWT).
2. ``head_env_values`` catches secrets by VALUE: whatever the operator (or
   a cloud recipe) put in this run's own ``head.env`` — a provider API key
   that carries no recognisable prefix is invisible to layer 1, but it is
   a short, known string the backend already has on disk for this exact
   run. ``anhang.md`` section D's stichprobe found both kinds in real
   transcripts (a 6-10 char OAuth-token fragment, and a bare key= value).
   Only SECRET-shaped keys (``…_API_KEY``/``…_TOKEN``/``…_SECRET``) feed
   this layer — ``head.env`` also carries ``ANTHROPIC_MODEL``/
   ``ANTHROPIC_BASE_URL`` etc., and those must stay readable (a head's
   model/engine is operationally interesting, not a secret; review finding
   on PR #751 — folding every ≥8-char value in made the model slug
   "<redacted>" for every claude-harness fixture/run).

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
    r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|sk-ant-[A-Za-z0-9_-]{20,}"
    r"|hf_[A-Za-z0-9]{30,}|eyJ[\w-]+\.[\w-]+\.[\w-]+)\b"
)

#: ``SOME_API_KEY=value`` / ``SOME_TOKEN: value`` / ``SOME_SECRET=value`` —
#: an env-style assignment or a debug print of one. ``redact_secrets``'s own
#: ``key=``/``token=`` pattern anchors on ``\b`` right before the keyword,
#: which never matches after a ``_`` (``\b`` needs a transition between a
#: word and a non-word character, and ``_`` is a word character) — so
#: ``OPENAI_API_KEY=…`` or ``export ANTHROPIC_API_KEY=…`` pass it untouched.
#: This pattern matches the WHOLE uppercase identifier by character class
#: instead of anchoring mid-word, so it does not have that gap; the
#: replacement keeps the key and separator, only the value is redacted.
_ENV_STYLE_SECRET = re.compile(r"\b([A-Z][A-Z0-9_]*(?:API_KEY|TOKEN|SECRET))(\s*[=:]\s*)\S+")

#: An ``x-api-key:`` header line (any case), as a debug print or a logged
#: request might carry it.
_HEADER_SECRET = re.compile(r"\b(x-api-key:\s*)\S+", re.IGNORECASE)

#: A value shorter than this is too common (model slugs, short flags) to
#: redact just for appearing in ``head.env`` — matches the bauplan's "≥ 8
#: Zeichen" rule and avoids masking e.g. a one-word model family name.
_MIN_EXTRA_LEN = 8

#: Which ``head.env`` KEYS carry a secret VALUE worth folding into
#: ``mask_text``'s ``extra`` tuple — ``…_API_KEY``/``…_TOKEN``/``…_SECRET``
#: (mirrors ``HEAD_ENV_KEYS``' naming convention in ``scripts/head/mc-head``
#: and ``services/heads/launcher.py``). Everything else ``head.env`` carries
#: today (``ANTHROPIC_BASE_URL``, ``ANTHROPIC_MODEL``,
#: ``ANTHROPIC_SMALL_FAST_MODEL``, ``OPENAI_BASE_URL``, ``OPENAI_MODEL``) is
#: operational, not secret, and must survive masking.
_SECRET_ENV_KEY = re.compile(r"(API_KEY|TOKEN|SECRET)$", re.IGNORECASE)


def mask_text(text: str, extra: tuple[str, ...] = ()) -> str:
    """``redact_secrets`` + bare-token shapes + the extra shape patterns
    above + every ``extra`` value this run's own ``head.env`` carries
    (longer than a few characters)."""
    out = _BARE_TOKENS.sub(REDACTED, redact_secrets(text))
    out = _ENV_STYLE_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", out)
    out = _HEADER_SECRET.sub(lambda m: f"{m.group(1)}{REDACTED}", out)
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


def _unquote(value: str) -> str:
    """Undo exactly what ``services/heads/launcher._format_env`` (the only
    writer of a real ``head.env``) does: wrap a value in single quotes.
    Also accepts double quotes and a leading ``export `` — a human editing
    ``head.env`` by hand on the host might use either; being liberal here
    costs nothing and this value is only ever used to redact itself."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def head_env_values(run) -> tuple[str, ...]:
    """The values of this run's own ``head.env`` whose KEY looks like a
    secret (``…_API_KEY``/``…_TOKEN``/``…_SECRET``) — never the keys
    themselves, never a model/URL value.

    Reads the file the same safe way every other head-writable file is read
    (``files.read_head_file``: ``O_NOFOLLOW``, regular files only) — even
    though ``head.env`` is backend-written, not head-written, the same
    symlink defence costs nothing and keeps one code path. Blank lines and
    comments are skipped, an optional ``export `` prefix is dropped, and a
    value wrapped in matching quotes is unwrapped — ``_format_env`` always
    writes ``KEY='value'``, and parsing it any other way (the previous
    version of this function did a bare ``strip()``) means the value this
    function returns is the QUOTED string, which never matches its own
    appearance in a transcript: live check against a real run (fcbf9b5d,
    2026-10-04) found 4/4 head.env values quoted and 0 redactions, every one
    ≥8 chars. A value is only ever RETURNED to be masked away, never
    printed, logged or placed in a response on its own.
    """
    text = read_head_file(run.folder / "head.env", 8_000) or ""
    values: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key = key.strip()
        value = _unquote(value.strip())
        if value and _SECRET_ENV_KEY.search(key):
            values.append(value)
    return tuple(values)
