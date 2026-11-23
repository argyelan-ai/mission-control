"""Search across chats (concept P3, behind ``CHAT_RECORD_ENABLED``).

What it searches: only message text of the user and the assistant —
thinking, tool titles, command output and teammate notes are noise for
"where did we talk about X?".

How it matches: every word of the query must match the start of a word
("deplo" finds "deploy"). On Postgres the candidates come from the GIN word
index (``to_tsvector('simple', …)``, migration 0227) with a prefix
``tsquery``; on SQLite (tests) from a case-insensitive substring match.

What it returns: one row per chat — its best event (the one to jump to), a
short snippet around the match with the match marked, and how many
candidate events matched. Ranking is stable: match quality first
(exact word > word start > inside a word), then the newest event.
"""
from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.chat_record import Chat, ChatEvent
from app.services.chat_record import _visible

#: Roles whose message text is searched.
SEARCH_ROLES = ("user", "assistant")
#: Newest matching events read per query before grouping; a chat whose only
#: matches are older than this window is not found (bounded work per query).
CANDIDATE_CAP = 2000
#: More query words than this are ignored.
MAX_TERMS = 8
#: Snippet length (without the two ``…`` marks) and context before the match.
SNIPPET_CHARS = 160
SNIPPET_LEAD = 50

EXACT, PREFIX, SUBSTRING = 3, 2, 1
_MATCH_NAME = {EXACT: "exact", PREFIX: "prefix", SUBSTRING: "substring"}
_WORD = re.compile(r"\w+")
_SPACE = re.compile(r"\s+")
_EPOCH = datetime.min.replace(tzinfo=UTC)


def terms_of(query: str) -> list[str]:
    """Lower-cased word tokens of the query, duplicates dropped. Only word
    characters survive, so nothing the user types is ever tsquery syntax."""
    out: list[str] = []
    for t in _WORD.findall((query or "").lower()):
        if t not in out:
            out.append(t)
    return out[:MAX_TERMS]


def _term_hit(text_lower: str, term: str) -> tuple[int, int] | None:
    """Best (quality, position) of one term in the text, or None."""
    m = re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text_lower)
    if m:
        return EXACT, m.start()
    m = re.search(rf"(?<!\w){re.escape(term)}", text_lower)
    if m:
        return PREFIX, m.start()
    i = text_lower.find(term)
    return (SUBSTRING, i) if i >= 0 else None


def score(text: str, terms: list[str]) -> tuple[int, int, int] | None:
    """(quality, match start, match length) for an event's text, or None when
    a term is missing. Quality = the weakest term (all words must match);
    the marked match is the strongest term, longest first on a tie."""
    lower = text.lower()
    hits = []
    for t in terms:
        h = _term_hit(lower, t)
        if h is None:
            return None
        hits.append((h[0], len(t), h[1]))
    quality = min(h[0] for h in hits)
    best = max(hits, key=lambda h: (h[0], h[1], -h[2]))
    end = best[2] + best[1]
    # a word-start match marks the whole word ("deplo" marks "deploy")
    if best[0] == PREFIX:
        m = _WORD.match(lower, best[2])
        if m:
            end = m.end()
    return quality, best[2], end - best[2]


def snippet_of(text: str, start: int, length: int) -> tuple[str, list[int]]:
    """A window of ``SNIPPET_CHARS`` around the match; ``…`` marks a cut.
    Returns the snippet and ``[start, end]`` of the match inside it."""
    lo = max(0, start - SNIPPET_LEAD)
    hi = min(len(text), max(lo + SNIPPET_CHARS, start + length))
    lo = max(0, min(lo, hi - SNIPPET_CHARS))
    head = "…" if lo > 0 else ""
    tail = "…" if hi < len(text) else ""
    s = start - lo + len(head)
    return f"{head}{text[lo:hi]}{tail}", [s, s + length]


async def search(
    session: AsyncSession,
    user: Any,
    query: str,
    *,
    limit: int = 50,
    agent_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """One best hit per visible, not archived chat, best chats first."""
    terms = terms_of(query)
    if not terms:
        return []
    dialect = session.bind.dialect.name if session.bind is not None else "postgresql"
    q = (
        select(
            ChatEvent.id, ChatEvent.chat_id, ChatEvent.kind, ChatEvent.role,
            ChatEvent.ts, ChatEvent.search_text, Chat.title, Chat.legacy_agent_id,
        )
        .join(Chat, Chat.id == ChatEvent.chat_id)
        .where(
            Chat.archived_at.is_(None),
            ChatEvent.kind == "message",
            ChatEvent.role.in_(SEARCH_ROLES),
        )
    )
    if dialect == "postgresql":
        doc = func.to_tsvector("simple", func.coalesce(ChatEvent.search_text, ""))
        tsq = func.to_tsquery("simple", " & ".join(f"{t}:*" for t in terms))
        q = q.where(doc.op("@@")(tsq))
    else:
        for t in terms:
            q = q.where(ChatEvent.search_text.ilike(f"%{t}%"))
    if agent_id is not None:
        q = q.where(Chat.legacy_agent_id == agent_id)
    visible = _visible(user)
    if visible is not True:
        q = q.where(visible)
    q = q.order_by(ChatEvent.ts.desc().nulls_last(), ChatEvent.seq.desc()).limit(CANDIDATE_CAP)

    best: dict[uuid.UUID, dict[str, Any]] = {}
    for ev_id, chat_id, kind, role, ts, raw, title, legacy_agent in (await session.exec(q)).all():
        text = _SPACE.sub(" ", raw or "").strip()
        scored = score(text, terms)
        if scored is None:
            continue
        quality, start, length = scored
        when = ts if ts is None or ts.tzinfo else ts.replace(tzinfo=UTC)  # SQLite drops the zone
        key = (quality, when or _EPOCH)
        cur = best.get(chat_id)
        if cur is not None:
            cur["matches"] += 1
            if key <= cur["_key"]:
                continue
        snippet, highlight = snippet_of(text, start, length)
        best[chat_id] = {
            "_key": key,
            "chatId": str(chat_id),
            "chatTitle": title,
            "agentId": str(legacy_agent) if legacy_agent else None,
            "eventId": str(ev_id),
            "kind": kind,
            "role": role,
            "ts": ts.isoformat() if ts else None,
            "snippet": snippet,
            "highlight": highlight,
            "match": _MATCH_NAME[quality],
            "matches": cur["matches"] if cur is not None else 1,
        }
    # stable: quality, newest, then chat id so equal rows never swap
    rows = sorted(best.values(), key=lambda r: (r["_key"], r["chatId"]), reverse=True)
    for r in rows:
        del r["_key"]
    return rows[:limit]
