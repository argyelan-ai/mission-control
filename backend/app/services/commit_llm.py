"""commit_llm — suggest a commit message with the local model, like ZCode.

Operator decision (12.10.2026): the commit sheet in the chat "Changes" panel
(``chat_actions.suggest_commit_message``, Changes panel part 3) should
suggest a message generated from the changes plus the chat's own intent —
not only a mechanical "chore: update prices.py and 2 more files". Local
model first (ADR-086): the SAME box MC already talks to for everything else
(``spark_client.SparkClient``, the active Spark runtime's OpenAI-compatible
endpoint, resolved fresh per call — no host hardcoded here), never a paid
API, never leaves the LAN.

The prompt is short on purpose: the changed file list with +/-, a capped
unified diff (``MAX_DIFF_CHARS`` — a prompt, not a patch), and the
operator's last few chat messages for intent. ``redact`` strips anything
that looks like a secret or a path outside the repository before either
ever reaches the model.

**Never raises.** A timeout, an unreachable box, or junk the model hands
back all become ``None`` — the router falls back to the deterministic
mechanical message (``chat_actions.suggest_commit_message``), which stays
the single source of truth for what gets committed when there is no
suggestion at all. The operator can always edit either one; nothing here
ever runs by itself.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from app.services.spark_client import SparkClient

logger = logging.getLogger(__name__)

#: The whole round trip — connect, generate, read — never holds the sheet
#: open longer than this; a Spark box that is busy or off just loses its turn.
LLM_TIMEOUT_SECONDS = 8.0
#: A prompt, not a patch — a few files' worth of context, not a full diff.
MAX_DIFF_CHARS = 6_000
#: Conventional Commit subject cap (ADR none; matches the mechanical message).
MAX_SUBJECT_CHARS = 72
#: Intent context: the chat's last few operator turns, newest last.
MAX_CHAT_MESSAGES = 3
#: One chat message capped before it rides the prompt.
MAX_MESSAGE_CHARS = 500
#: Body bullets kept from the model's answer.
MAX_BODY_BULLETS = 3

SYSTEM_PROMPT = (
    "You write git commit messages for a coding chat's own changes. Given "
    "the changed files, a short diff, and the operator's recent chat "
    "messages, answer with ONE Conventional Commit message: a subject line "
    "'<type>: <description>' of at most 72 characters (lowercase type, e.g. "
    "feat/fix/chore/docs/test/refactor), optionally followed by a blank "
    "line and 1-3 short bullet points, each starting with '- '. English "
    "only. Answer with the message ONLY — no markdown code fences, no "
    "quotes, no explanation, no preamble."
)

_SUBJECT_RE = re.compile(r"^[a-z]+(\([a-zA-Z0-9_\-./ ]+\))?!?:\s+\S.*$")
_FENCE_RE = re.compile(r"^```[a-zA-Z0-9]*\n?|\n?```$")

# ── Redaction — nothing secret, nothing outside the repo, reaches the prompt ─

_SECRET_PATTERNS = [
    # key = value / key: value style secrets (api key, token, password, …)
    re.compile(
        r"(?im)\b((?:api[_-]?key|secret|token|password|passwd|access[_-]?key"
        r"|private[_-]?key|client[_-]?secret)\b\s*[:=]\s*)(['\"]?)(\S+?)\2(?=[\s,;]|$)"
    ),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),  # GitHub tokens
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),  # OpenAI-style keys
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),  # JWT
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----"),
]
_HOME_PATH_RE = re.compile(r"(/(?:Users|home)/)([^/\s]+)")


def redact(text: str) -> str:
    """Strips anything that looks like a secret, and anonymises a home
    directory path (``/Users/testuser/...`` -> ``/Users/«redacted»/...``) —
    best effort, never a guarantee, applied to everything built into the
    prompt before it leaves this process."""
    out = text
    for pattern in _SECRET_PATTERNS[:2]:
        out = pattern.sub(lambda m: m.group(1) + "«redacted»" if m.lastindex else "«redacted»", out)
    for pattern in _SECRET_PATTERNS[2:]:
        out = pattern.sub("«redacted»", out)
    out = _HOME_PATH_RE.sub(lambda m: f"{m.group(1)}«redacted»", out)
    return out


def _sanitize(raw: str) -> str | None:
    """The model's raw answer, turned into a commit message — or ``None``
    when it is not usable (empty, not Conventional Commit shaped, too
    long): the caller falls back rather than commit junk."""
    if not raw:
        return None
    text = _FENCE_RE.sub("", raw.strip()).strip()
    lines = [ln.rstrip() for ln in text.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return None
    subject = lines[0].strip().strip('"').strip("'")
    if not subject or len(subject) > MAX_SUBJECT_CHARS or not _SUBJECT_RE.match(subject):
        return None
    bullets: list[str] = []
    for ln in lines[1:]:
        stripped = ln.strip()
        if not stripped:
            continue
        if not stripped.startswith(("-", "*")):
            stripped = f"- {stripped}"
        else:
            stripped = "- " + stripped.lstrip("-* ").strip()
        bullets.append(stripped)
        if len(bullets) >= MAX_BODY_BULLETS:
            break
    if bullets:
        return subject + "\n\n" + "\n".join(bullets) + "\n"
    return subject + "\n"


def _file_lines(files: list[dict[str, Any]]) -> str:
    out = []
    for f in files[:30]:
        counts = []
        if f.get("additions"):
            counts.append(f"+{f['additions']}")
        if f.get("deletions"):
            counts.append(f"-{f['deletions']}")
        suffix = f" ({' '.join(counts)})" if counts else ""
        out.append(f"- {f['path']} [{f.get('status', 'modified')}]{suffix}")
    if len(files) > 30:
        out.append(f"- … {len(files) - 30} more files")
    return "\n".join(out)


def extract_user_messages(events: Any, limit: int = MAX_CHAT_MESSAGES) -> list[str]:
    """The chat's last ``limit`` operator turns, oldest first — the Changes
    panel's own transcript events (``kind: "message", role: "user"``, see
    ``transcript_chat.py``). Anything not shaped like that is ignored; never
    raises on odd input."""
    out: list[str] = []
    if not isinstance(events, (list, tuple)):
        return out
    for ev in events:
        if isinstance(ev, dict) and ev.get("kind") == "message" and ev.get("role") == "user":
            text = ev.get("text")
            if isinstance(text, str) and text.strip():
                out.append(text.strip())
    return out[-limit:] if limit else out


def build_prompt(
    *, branch: str, files: list[dict[str, Any]], diff_text: str, user_messages: list[str]
) -> str:
    """The compact, redacted prompt body (system prompt is ``SYSTEM_PROMPT``,
    sent separately)."""
    messages = [redact(m)[:MAX_MESSAGE_CHARS] for m in user_messages if m and m.strip()]
    messages_block = (
        "\n".join(f"{i + 1}. {m}" for i, m in enumerate(messages))
        if messages
        else "(no chat messages)"
    )
    return (
        f"Branch: {branch}\n\n"
        f"Changed files:\n{redact(_file_lines(files))}\n\n"
        f"Diff (truncated):\n{redact(diff_text)[:MAX_DIFF_CHARS]}\n\n"
        f"Recent operator chat messages (most recent last):\n{messages_block}"
    )


async def suggest_commit_message(
    *,
    branch: str,
    files: list[dict[str, Any]],
    diff_text: str,
    user_messages: list[str],
    spark: SparkClient | None = None,
) -> str | None:
    """The local model's commit-message suggestion, or ``None`` on any
    failure/timeout/unusable answer — the router's fallback is the
    deterministic mechanical message, never a 500. ``spark`` is the seam
    tests inject a fake client through; production always resolves the
    active Spark runtime fresh (``spark.llm_url`` reads the DB on every
    call, no restart needed after a recipe switch)."""
    if not files:
        return None
    client = spark or SparkClient(timeout=LLM_TIMEOUT_SECONDS)
    prompt = build_prompt(branch=branch, files=files, diff_text=diff_text, user_messages=user_messages)
    try:
        raw = await asyncio.wait_for(
            client.complete(prompt=prompt, system=SYSTEM_PROMPT, max_tokens=220, temperature=0.2),
            timeout=LLM_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 — unreachable/timeout/anything: fall back, never 500
        logger.info("commit_llm: suggestion unavailable (%s: %s)", type(exc).__name__, exc)
        return None
    return _sanitize(raw)
