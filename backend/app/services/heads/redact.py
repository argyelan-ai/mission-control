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
#: This pattern matches the WHOLE identifier by character class instead of
#: anchoring mid-word, so it does not have that gap; the replacement keeps
#: the key and separator, only the value is redacted.
#:
#: Round 3 review finding: the prefix used to be MANDATORY
#: (``[A-Z][A-Z0-9_]*`` followed directly by the suffix), so a BARE
#: ``API_KEY=…``/``TOKEN=…``/``PASSWORD=…`` (no prefix at all) never
#: matched, and ``AWS_SECRET_ACCESS_KEY=…`` never matched either (the
#: identifier ends in ``ACCESS_KEY``, not in ``API_KEY``/``TOKEN``/
#: ``SECRET``). The prefix is now optional and ``SECRET_ACCESS_KEY``/
#: ``PASSWORD`` were added as their own suffixes. ``re.IGNORECASE`` closes
#: the matching lowercase gaps found live: a bare YAML ``token: …`` line and
#: a lowercase ``client_secret=…`` assignment — this module already masks
#: by VALUE-independent SHAPE, so erring towards redacting ordinary prose
#: that happens to look like ``word: value`` costs nothing a transcript
#: reader needs, and a missed real secret costs everything.
_ENV_STYLE_SECRET = re.compile(
    r"\b([A-Z][A-Z0-9_]*)?(API_KEY|SECRET_ACCESS_KEY|TOKEN|SECRET|PASSWORD)(\s*[=:]\s*)\S+",
    re.IGNORECASE,
)

#: An ``x-api-key:`` header line (any case), as a debug print or a logged
#: request might carry it.
_HEADER_SECRET = re.compile(r"\b(x-api-key:\s*)\S+", re.IGNORECASE)

#: ``Authorization: Basic …`` / ``authorization: token …`` — a Basic- or
#: Token-scheme Authorization header, logged verbatim (a bare Bearer value
#: with no further scheme word is already a JWT/opaque token caught by
#: ``_BARE_TOKENS`` or ``redact_secrets``'s own ``Bearer `` pattern; this
#: one is for the OTHER two schemes a debug print of a request might show).
#: Round 3 review finding on PR #751. Only the credential half is
#: replaced — the header name and scheme word stay readable.
_AUTH_SCHEME_SECRET = re.compile(r"(\bAuthorization:\s*(?:Basic|Token)\s+)\S+", re.IGNORECASE)

#: A secret passed as a CLI flag, as a shell command logged into a
#: transcript might show it: ``--api-key xxx``, ``--token=xxx``. Round 3
#: review finding on PR #751 — neither ``_ENV_STYLE_SECRET`` (needs an
#: identifier made of ``[A-Z0-9_]`` only; a flag name has hyphens) nor
#: ``redact_secrets``'s ``key=`` pattern (anchors ``\b`` right before the
#: keyword, which a leading ``--`` also defeats the same way a leading
#: ``_`` does) catches this shape.
_CLI_FLAG_SECRET = re.compile(r"(--(?:api[_-]?key|token|secret|password)[=\s]+)\S+", re.IGNORECASE)

#: A JSON- or Python-dict-quoted secret-shaped key: ``"OPENAI_API_KEY":
#: "value"``, ``{"apiKey":"value"}`` or a Python ``repr()``'d dict's
#: ``{'api_key': 'value'}`` — a tool call's own request/response body (or a
#: debug print of a dict) logged verbatim into the transcript. Neither
#: ``_ENV_STYLE_SECRET`` (anchors directly on ``KEY<sep>value``, with no
#: room for the key's own closing quote in between) nor ``redact_secrets``'s
#: plain ``key=``/``token=`` catches this shape; review finding on PR #751.
#: The key's quote char is captured once (``q``) and backreferenced for
#: BOTH the key's closing quote and the value's quotes, so a single-quoted
#: Python dict and a double-quoted JSON body are each masked with their own
#: matching quote style, never a mismatched one. The key itself (and its
#: surrounding quotes/colon, plus the value's own quotes) is kept — only
#: the quoted value's content is replaced.
_JSON_KEY_SECRET = re.compile(
    r"(?P<prefix>(?P<q>[\"'])(?:[A-Za-z_]*(?:api[_-]?key|token|secret|password)[A-Za-z_]*)(?P=q)\s*:\s*(?P=q))"
    r"[^\"']*"
    r"(?P<suffix>(?P=q))",
    re.IGNORECASE,
)

#: A bare ``password=value`` assignment (query string, config dump, debug
#: print) — distinct from ``_ENV_STYLE_SECRET``, which only matches an
#: ALL-CAPS ``…_API_KEY``/``…_TOKEN``/``…_SECRET`` identifier and never
#: lowercase ``password``. Review finding on PR #751.
_PASSWORD_EQ = re.compile(r"\b(password\s*=\s*)\S+", re.IGNORECASE)

#: HTTP Basic-auth-style userinfo embedded in a URL — ``scheme://user:pass@
#: host``. Only the password half is replaced; the username stays (it is
#: rarely secret on its own, and keeping it makes the masked line still
#: readable). Review finding on PR #751. The password half excludes ``/``
#: (round 3 review finding): without that, ``http://localhost:3000/@vite/
#: client`` over-matched — the ``[^@\s]+`` password class happily crossed
#: the path separator to reach the FIRST ``@`` anywhere later in the path,
#: masking ``3000/`` as if it were a credential. A real userinfo ``@`` is
#: always part of the URL's AUTHORITY, which ends at the first ``/`` — so
#: the password can never legitimately contain one either.
_URL_USERINFO_SECRET = re.compile(r"(://[^/\s:@]+:)[^/@\s]+(@)")

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
    out = _ENV_STYLE_SECRET.sub(lambda m: f"{m.group(1) or ''}{m.group(2)}{m.group(3)}{REDACTED}", out)
    out = _HEADER_SECRET.sub(lambda m: f"{m.group(1)}{REDACTED}", out)
    out = _AUTH_SCHEME_SECRET.sub(lambda m: f"{m.group(1)}{REDACTED}", out)
    out = _CLI_FLAG_SECRET.sub(lambda m: f"{m.group(1)}{REDACTED}", out)
    out = _JSON_KEY_SECRET.sub(lambda m: f"{m.group('prefix')}{REDACTED}{m.group('suffix')}", out)
    out = _PASSWORD_EQ.sub(lambda m: f"{m.group(1)}{REDACTED}", out)
    out = _URL_USERINFO_SECRET.sub(lambda m: f"{m.group(1)}{REDACTED}{m.group(2)}", out)
    for value in extra:
        if value and len(value) >= _MIN_EXTRA_LEN:
            out = out.replace(value, REDACTED)
    return out


#: A dict KEY whose name alone marks its value as secret, regardless of
#: shape — a structured tool event carries its payload as an actual dict
#: (``detail: {"api_key": "…", "headers": {"Authorization": "Basic …"}}``,
#: an MCP or web tool's input/output can be any key/value pairs), and by
#: the time ``mask_tree`` sees it, the value is already a plain Python
#: string with no ``=``/``:``/quote shape left for ``mask_text``'s TEXT
#: patterns to anchor on — round 3 review finding on PR #751:
#: ``mask_tree({'detail': {'api_key': 'Zq9x…', 'password': 'Zq9x…',
#: 'headers': {'Authorization': 'Basic Zq9x…'}}})`` returned all three
#: values unredacted before this fix. No ``\b`` anchors on purpose: a key
#: like ``input_tokens`` matching the ``token`` fragment is harmless (its
#: value is an ``int``, never checked below), and a false-positive STRING
#: match only means over-redacting, never under-redacting.
_SECRET_KEY_NAME = re.compile(r"(?i)(api[_-]?key|token|secret|password|passwd|authorization|cookie|credential)")


def mask_tree(obj: Any, extra: tuple[str, ...] = ()) -> Any:
    """``mask_text`` recursively over a dict/list/str structure — the shape
    returned by ``transcript_chat.read_history`` (events, nested ``detail``
    dicts, tool results). Non-string leaves (ints, bools, ``None``) pass
    through unchanged; only string content can carry a secret.

    A dict value is checked AGAINST ITS OWN KEY first (``_SECRET_KEY_NAME``)
    before anything else: a secret-shaped key's STRING value is replaced
    wholesale, never handed to ``mask_text``'s shape patterns at all — the
    value of ``"api_key"`` might be an opaque provider token with no
    recognisable shape of its own, exactly the case layer 1 (shape) and the
    old key-blind layer 2 (nested recursion only) both missed."""
    if isinstance(obj, str):
        return mask_text(obj, extra)
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        for k, v in obj.items():
            if isinstance(k, str) and isinstance(v, str) and _SECRET_KEY_NAME.search(k):
                out[k] = REDACTED
            else:
                out[k] = mask_tree(v, extra)
        return out
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
