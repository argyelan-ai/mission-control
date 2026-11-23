"""Search across chats (concept P3, behind ``CHAT_RECORD_ENABLED``).

What it searches: only message text of the user and the assistant —
thinking, tool titles, command output and teammate notes are noise for
"where did we talk about X?".

How it matches: the query is split on spaces only, so "deploy.sh", "v0.9",
an IP or an e-mail stay one term. Every term must match the start of a word
("deplo" finds "deploy", "deploy.s" finds "deploy.sh"). On Postgres the
candidates come from the GIN word index (``to_tsvector('simple', …)``,
migration 0227). The ``tsquery`` is built as a literal, not with
``to_tsquery``: ``to_tsquery`` would run the term through the parser again
and split "chat_record" or "bob@ex" into pieces that never match the stored
word "chat_record.py" / "bob@example.com". Each term is
``'term':* | ('part1':* & 'part2':*)``, so a term the text splits ("it's")
is found too. On SQLite (tests) a case-insensitive substring match stands in.

Only the operator's own conversation is searched: subagent prompts and
answers (``payload.sidechain``) are left out — their jump target would sit
inside a collapsed subagent group.

What it returns: one row per chat — its best event (the one to jump to), a
short snippet around the match with the match marked (``highlight`` =
``[start, end]`` in UTF-16 code units, i.e. JavaScript string indices), and
how many candidate events matched. Ranking is stable: match quality first
(exact word > word start > inside a word), then the newest event. Work per
query is bounded by ``CANDIDATE_CAP`` newest candidates; when that cap is
hit the answer says ``truncated`` and Postgres runs one extra pass for exact
words, so an older exact hit is not lost behind newer word-start hits.
"""
from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, cast, false, func, literal, or_
from sqlalchemy.dialects.postgresql import TSQUERY
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
#: Anything that is not a word character at the start or end of a term.
_EDGE = re.compile(r"^\W+|\W+$")
_SPACE = re.compile(r"\s+")
_EPOCH = datetime.min.replace(tzinfo=UTC)


def terms_of(query: str) -> list[str]:
    """Lower-cased query terms, split on spaces only, punctuation at the edges
    dropped ("deploy.sh," → "deploy.sh", "(v0.9)" → "v0.9", "!(" → nothing),
    duplicates dropped. Inner characters stay, so file names, versions, IPs
    and e-mails remain one term."""
    out: list[str] = []
    for raw in (query or "").lower().split():
        t = _EDGE.sub("", raw)
        if t and t not in out:
            out.append(t)
    return out[:MAX_TERMS]


def _parts(term: str) -> list[str]:
    """The word pieces of a term ("it's" → it, s); empty for a plain word."""
    parts = _WORD.findall(term)
    return parts if parts != [term] else []


def _lexeme(word: str, prefix: bool) -> str:
    """One quoted tsquery operand; quotes and backslashes are escaped, so
    nothing the user types is ever tsquery syntax."""
    q = word.replace("\\", "\\\\").replace("'", "''")
    return f"'{q}':*" if prefix else f"'{q}'"


def tsquery_of(terms: list[str], *, prefix: bool = True) -> str:
    """A tsquery literal: every term as one word, or as all of its pieces."""
    out = []
    for t in terms:
        whole = _lexeme(t, prefix)
        parts = _parts(t)
        if parts:
            whole = f"({whole} | ({' & '.join(_lexeme(p, prefix) for p in parts)}))"
        out.append(whole)
    return " & ".join(out)


def _one_hit(text: str, term: str) -> tuple[int, int, int] | None:
    """Best (quality, start, length) of one word in the text, or None.
    Case-insensitive on the original text, so positions stay right even when
    a letter changes length on ``lower()`` ("İ")."""
    e = re.escape(term)
    # exact = not followed by more of the same word; "deploy" in "deploy.sh"
    # is a word start, as the Postgres parser keeps "deploy.sh" as one word
    for quality, pattern in ((EXACT, rf"(?<!\w){e}(?!\w|[.@/:-]\w)"), (PREFIX, rf"(?<!\w){e}"), (SUBSTRING, e)):
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            end = m.end()
            if quality == PREFIX:  # a word-start match marks the whole word
                w = _WORD.match(text, m.end())
                end = w.end() if w else end
            return quality, m.start(), end - m.start()
    return None


def _term_hit(text: str, term: str) -> tuple[int, int, int] | None:
    """The term as one word, or — like the tsquery — all of its pieces
    (the weakest piece sets the quality, the first one is marked)."""
    h = _one_hit(text, term)
    if h is not None:
        return h
    parts = _parts(term)
    if not parts:
        return None
    hits = [_one_hit(text, p) for p in parts]
    if any(x is None for x in hits):
        return None
    first = min(hits, key=lambda x: x[1])
    return min(x[0] for x in hits), first[1], first[2]


def score(text: str, terms: list[str]) -> tuple[int, int, int] | None:
    """(quality, match start, match length) for an event's text, or None when
    a term is missing. Quality = the weakest term (all words must match);
    the marked match is the strongest term, longest first on a tie."""
    hits = []
    for t in terms:
        h = _term_hit(text, t)
        if h is None:
            return None
        hits.append(h)
    quality = min(h[0] for h in hits)
    best = max(hits, key=lambda h: (h[0], h[2], -h[1]))
    return quality, best[1], best[2]


def _utf16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def snippet_of(text: str, start: int, length: int) -> tuple[str, list[int]]:
    """A window of ``SNIPPET_CHARS`` around the match; ``…`` marks a cut.
    Returns the snippet and ``[start, end]`` of the match inside it, in
    UTF-16 code units (JavaScript string indices)."""
    lo = max(0, start - SNIPPET_LEAD)
    hi = min(len(text), max(lo + SNIPPET_CHARS, start + length))
    lo = max(0, min(lo, hi - SNIPPET_CHARS))
    head = "…" if lo > 0 else ""
    tail = "…" if hi < len(text) else ""
    snippet = f"{head}{text[lo:hi]}{tail}"
    s = start - lo + len(head)
    # UTF-16 units: the client is JavaScript, where an emoji is two units
    a = _utf16(snippet[:s])
    return snippet, [a, a + _utf16(snippet[s:s + length])]


async def _candidates(session: AsyncSession, user: Any, terms: list[str], dialect: str,
                      *, exact: bool, agent_id: uuid.UUID | None) -> list[Any]:
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
            # subagent prompts/answers are not the operator's conversation
            func.coalesce(ChatEvent.payload["sidechain"].as_boolean(), false()) == false(),
        )
    )
    if dialect == "postgresql":
        doc = func.to_tsvector("simple", func.coalesce(ChatEvent.search_text, ""))
        tsq = cast(literal(tsquery_of(terms, prefix=not exact)), TSQUERY)
        q = q.where(doc.op("@@")(tsq))
    else:
        for t in terms:
            cond = ChatEvent.search_text.ilike(f"%{t}%")
            parts = _parts(t)
            if parts:
                cond = or_(cond, and_(*(ChatEvent.search_text.ilike(f"%{p}%") for p in parts)))
            q = q.where(cond)
    if agent_id is not None:
        q = q.where(Chat.legacy_agent_id == agent_id)
    visible = _visible(user)
    if visible is not True:
        q = q.where(visible)
    q = q.order_by(ChatEvent.ts.desc().nulls_last(), ChatEvent.seq.desc()).limit(CANDIDATE_CAP)
    return list((await session.exec(q)).all())


async def search(
    session: AsyncSession,
    user: Any,
    query: str,
    *,
    limit: int = 50,
    agent_id: uuid.UUID | None = None,
    with_meta: bool = False,
) -> Any:
    """One best hit per visible, not archived chat, best chats first.
    ``with_meta`` returns ``{"hits": [...], "truncated": bool}`` — truncated
    means the newest ``CANDIDATE_CAP`` candidates were all read, so older
    word-start hits may be missing (refine the search)."""
    terms = terms_of(query)
    if not terms:
        return {"hits": [], "truncated": False} if with_meta else []
    dialect = session.bind.dialect.name if session.bind is not None else "postgresql"
    rows = await _candidates(session, user, terms, dialect, exact=False, agent_id=agent_id)
    truncated = len(rows) >= CANDIDATE_CAP
    if truncated and dialect == "postgresql":
        # newer word-start hits filled the window: fetch exact words on their own
        seen = {r[0] for r in rows}
        rows += [r for r in await _candidates(session, user, terms, dialect, exact=True, agent_id=agent_id)
                 if r[0] not in seen]

    best: dict[uuid.UUID, dict[str, Any]] = {}
    for ev_id, chat_id, kind, role, ts, raw, title, legacy_agent in rows:
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
    ranked = sorted(best.values(), key=lambda r: (r["_key"], r["chatId"]), reverse=True)
    for r in ranked:
        del r["_key"]
    hits = ranked[:limit]
    return {"hits": hits, "truncated": truncated} if with_meta else hits
