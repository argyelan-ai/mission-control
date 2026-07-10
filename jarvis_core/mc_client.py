"""Tool-Bridge: macht authentifizierte API-Calls gegen das MC-Backend im Namen
des Jarvis-Agents.

Geteilt (ADR-061) zwischen dem ``voice_worker`` (LiveKit-Host) und dem Backend
(Telegram-Inbound). Beide Kanaele fuehren Jarvis-Tool-Calls ueber denselben
agent-scoped Pfad /api/v1/agent/* aus — kein Auth-Bypass, keine Direkt-DB.

Base-URL + Token kommen aus dem Environment und unterscheiden sich pro Prozess:
- voice_worker: ``MC_BACKEND_URL=http://backend:8000`` (Docker-Netz).
- backend (self-call): ``MC_BACKEND_URL=http://localhost:8000``.

Nutzt den Jarvis-Agent PBKDF2-Token (env ``JARVIS_AGENT_TOKEN`` mit
``VOICE_AGENT_TOKEN`` als Legacy-Fallback fuer Bootstrap-Phasen wo das
.env noch nicht durchgezogen wurde — siehe ADR-038). Die Boss-equivalenten
Scopes (siehe agent.scopes) gelten fuer alle Calls.
"""

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger("voice_worker.mc_client")

MC_BACKEND_URL = os.environ.get("MC_BACKEND_URL", "http://backend:8000")
# ADR-038: Token-Var-Name ist jetzt JARVIS_AGENT_TOKEN. VOICE_AGENT_TOKEN
# bleibt einen Release-Zyklus lang als Fallback erhalten damit ein nicht
# nachgezogenes .env keinen 401-Loop verursacht. Bei nur einem gesetzten Wert
# nehmen wir den; bei beiden gewinnt der neue Name.
JARVIS_AGENT_TOKEN = (
    os.environ.get("JARVIS_AGENT_TOKEN")
    or os.environ.get("VOICE_AGENT_TOKEN")
    or ""
)
JARVIS_BOARD_ID = os.environ.get(
    "JARVIS_BOARD_ID",
    os.environ.get("VOICE_BOARD_ID", "7bd0be90-c45a-4a15-9037-ebb72f15ba09"),
)  # mc-dev

# Jarvis-Agent hat board_id = mc-dev gesetzt (siehe Jarvis DB-Eintrag).
# Tool-Routen schicken explizit darauf, damit Scopes greifen.

_client = httpx.AsyncClient(
    base_url=MC_BACKEND_URL,
    headers={"Authorization": f"Bearer {JARVIS_AGENT_TOKEN}"},
    timeout=15.0,
)


# Deutsche + englische Stoppwörter, die der Operator beim Sprechen reinwirft aber
# die in der Vault FTS5-Suche nichts beitragen. Bewusst klein gehalten —
# wir wollen NICHT semantisch wichtige Wörter rauswerfen (z.B. "morgen"
# ist hier drin, aber "Morgen-Briefing" hat den Bindestrich-Token "morgen"
# der INHALT ist; die Heuristik unten behandelt den Spezialfall: wenn die
# Query nach dem Filtern leer wird, geben wir die Original-Query zurück).
_STOPWORDS = frozenset({
    # Artikel + Demonstrativ
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "einen", "einer",
    "eines", "ne", "nem",
    # Pronomen / Possessiv
    "ich", "du", "er", "sie", "es", "wir", "ihr", "mich", "dich", "mir", "dir",
    "uns", "euch", "sich", "mein", "meine", "dein", "deine",
    # Hilfsverben
    "ist", "sind", "war", "waren", "bin", "bist", "hab", "habe", "hat", "hatte",
    "wird", "werde", "wurden",
    # Konjunktionen / Partikeln
    "und", "oder", "aber", "denn", "sondern", "weil", "dass", "ob", "wenn",
    "noch", "auch", "nur", "ganz", "gar", "schon", "halt", "eben", "ja", "doch",
    # Präpositionen
    "auf", "an", "in", "zu", "von", "mit", "bei", "fuer", "für", "aus", "nach",
    "über", "ueber", "unter", "vor", "ohne", "bis", "durch",
    # Fragewörter (die LEERE Frage interessiert uns selten — der INHALT zählt)
    "was", "wer", "wie", "wo", "wann", "wieso", "warum", "welche", "welcher",
    "welches",
    # Höflichkeit + Voice-Floskeln
    "bitte", "danke", "mal", "etwas", "ein", "paar", "kannst", "könntest",
    "koenntest", "zeig", "zeige", "such", "suche", "finde", "gib", "sag",
    "erzähl", "erzaehl", "such", "look",
    # Temporal-Modifier
    "heutig", "heutige", "heutiges", "letzt", "letzte", "letzter", "letzten",
    "gestern",
    # Generisch / leere Modifier
    "nicht", "nichts", "alles", "alle", "etwas", "irgend", "irgendwas",
    "irgendwie", "mehr", "weniger",
    # English equivalents (der Operator switcht sprachlich)
    "the", "a", "an", "is", "are", "was", "were", "and", "or", "but",
    "for", "of", "to", "in", "on", "at", "with", "from", "this", "that",
    "what", "who", "how", "when", "where", "why", "please", "show", "find",
    "search", "give", "tell",
})


def _smart_query(q: str) -> tuple[str, bool]:
    """Reduziert eine Voice-Phrase auf such-taugliche Keywords.

    Der Operator spricht in Sätzen — Jarvis sendet die direkt an FTS5, das mit
    AND-Semantik dann 0 Treffer liefert wenn auch nur ein irrelevantes
    Wort (z.B. "heutiges") nirgends im Vault steht. Heuristik:

    1. Lowercase + auf Wort-Tokens splitten (alpha + Bindestrich/Underscore).
    2. Stoppwörter wegfiltern.
    3. Falls ≥ 2 Tokens übrigbleiben → Wörter mit Komma trennen (FTS5
       bekommt sie als getrennte Phrasen, der index.search-Sanitizer
       macht daraus AND — das ist die strenge Variante).
    4. Returnt zusätzlich ein Flag ``or_fallback_useful`` damit der Caller
       weiß: bei 0 Treffern lohnt sich ein zweiter Versuch mit OR.
    5. Wenn nach Filter NICHTS uebrig → Original zurück (der Operator hat nur ein
       einzelnes Stoppwort gesagt, da wollen wir nicht löschen).
    """
    import re
    raw = (q or "").strip()
    if not raw:
        return raw, False
    # Tokens: alphanumeric + Bindestrich/Underscore (so dass "morgen-briefing"
    # ein Token bleibt). Punkte/Kommas etc. fliegen raus.
    tokens = re.findall(r"[\wäöüÄÖÜß][\wäöüÄÖÜß\-_]*", raw, flags=re.UNICODE)
    if not tokens:
        return raw, False
    kept = [t for t in tokens if t.lower() not in _STOPWORDS and len(t) >= 2]
    if not kept:
        # Reine Stoppwort-Query? Original behalten als Fallback.
        return raw, False
    cleaned = " ".join(kept)
    or_fallback_useful = len(kept) >= 2
    return cleaned, or_fallback_useful


async def _resolve_agent_id(name: str | None) -> tuple[str | None, str | None]:
    """Resolve einen Agent-Namen → (agent_id, canonical_name).

    Case-insensitive + Levenshtein-ish fuzzy match (Single-Char-Distance) damit
    STT-Fehler wie "Kodi"→"Cody" oder "Spaki"→"Sparky" trotzdem treffen.
    Returnt (None, None) wenn nichts gefunden.
    """
    if not name:
        return None, None
    resp = await _client.get(f"/api/v1/agent/boards/{JARVIS_BOARD_ID}/agents")
    if resp.status_code != 200:
        return None, None
    agents = resp.json()
    target = name.strip().lower()

    # 1) exact case-insensitive match
    for a in agents:
        if a.get("name", "").lower() == target:
            return a["id"], a["name"]

    # 2) single-char edit distance (handles STT confusion: cody/kodi, neo/neu)
    def _close(a: str, b: str) -> bool:
        if abs(len(a) - len(b)) > 1:
            return False
        if a == b:
            return True
        # one substitution
        if len(a) == len(b):
            diffs = sum(1 for x, y in zip(a, b) if x != y)
            return diffs == 1
        # one insertion/deletion
        short, long = (a, b) if len(a) < len(b) else (b, a)
        i = j = diffs = 0
        while i < len(short) and j < len(long):
            if short[i] != long[j]:
                diffs += 1
                if diffs > 1:
                    return False
                j += 1
            else:
                i += 1
                j += 1
        return True

    for a in agents:
        if _close(a.get("name", "").lower(), target):
            return a["id"], a["name"]

    return None, None


async def _find_board_lead() -> tuple[str | None, str | None]:
    """Holt den Board-Lead-Agent als Fallback fuer unklare Task-Zuweisungen."""
    resp = await _client.get(f"/api/v1/agent/boards/{JARVIS_BOARD_ID}/agents")
    if resp.status_code != 200:
        return None, None
    agents = resp.json()
    for a in agents:
        if a.get("is_board_lead"):
            return a["id"], a["name"]
    return None, None


async def create_task(
    title: str,
    description: str = "",
    assigned_agent_name: str | None = None,
    priority: str = "medium",
) -> dict[str, Any]:
    """Erstellt einen MC-Task.

    - Wenn assigned_agent_name gesetzt + erkannt → direkt zugewiesen.
    - Wenn assigned_agent_name None ODER nicht erkannt → Board Lead (Boss) bekommt
      ihn zur Orchestrierung. NIEMALS dem Jarvis-Agent selbst zuweisen.

    Backend defaultet sonst auf agent.id (Jarvis) — siehe agent_task_status.py:1016.
    Daher MUSS hier ein expliziter assigned_agent_id rein.
    """
    assigned_id: str | None = None
    canonical_name: str | None = None
    if assigned_agent_name:
        assigned_id, canonical_name = await _resolve_agent_id(assigned_agent_name)
        if assigned_id is None:
            logger.warning(
                "create_task: agent_name=%r nicht erkannt — Fallback auf Board Lead",
                assigned_agent_name,
            )

    fallback_used = False
    if not assigned_id:
        assigned_id, canonical_name = await _find_board_lead()
        fallback_used = True
        if not assigned_id:
            return {
                "ok": False,
                "error": "Kein Board Lead auf diesem Board gefunden — Task kann nicht zugewiesen werden.",
            }

    payload = {
        "title": title,
        "description": description,
        "priority": priority,
        "assigned_agent_id": assigned_id,
    }

    resp = await _client.post(
        f"/api/v1/agent/boards/{JARVIS_BOARD_ID}/tasks",
        json=payload,
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "ok": True,
        "task_id": data.get("id"),
        "title": data.get("title"),
        "assigned_to": canonical_name,
        "note": (
            f"Name '{assigned_agent_name}' nicht erkannt — an Board Lead ({canonical_name}) delegiert."
            if fallback_used and assigned_agent_name
            else None
        ),
    }


async def dispatch_to_agent(
    agent_name: str,
    instruction: str,
    priority: str = "medium",
) -> dict[str, Any]:
    """Weist einem konkreten Agenten SOFORT einen Auftrag zu (ADR-062).

    Unterschied zu ``create_task``:
    - ``create_task`` faellt bei unbekanntem/fehlendem Assignee auf den Board Lead
      (Boss) zurueck → Backlog/Orchestrierung.
    - ``dispatch_to_agent`` braucht einen ECHTEN Agenten. Wird der Name nicht
      erkannt, gibt es KEINEN stillen Fallback — stattdessen ein klarer Fehler,
      damit Jarvis nachfragt statt den falschen Weg zu gehen.

    Der eigentliche Dispatch (Session-Start des Agenten) passiert im Backend:
    ``POST /boards/{board}/tasks`` mit ``assigned_agent_id != creator`` triggert im
    agent-scoped Router direkt den CLI-Bridge-Dispatch (kein Auth-Bypass, keine
    Direkt-DB, kein neuer Endpoint). Die Antwort des Backends enthaelt ein
    ``dispatch``-Feld mit dem Status ({status: dispatched|blocked|...}), das wir
    unveraendert durchreichen, damit Jarvis dem Operator ehrlich sagen kann, ob der
    Agent wirklich losgelegt hat oder (z.B. offline) in der Queue haengt.
    """
    assigned_id, canonical_name = await _resolve_agent_id(agent_name)
    if assigned_id is None:
        return {
            "ok": False,
            "reason": "agent_not_found",
            "requested": agent_name,
            "error": f"Agent '{agent_name}' nicht erkannt — an wen soll der Auftrag gehen?",
        }

    payload = {
        "title": instruction,
        "description": instruction,
        "priority": priority,
        "assigned_agent_id": assigned_id,
    }
    resp = await _client.post(
        f"/api/v1/agent/boards/{JARVIS_BOARD_ID}/tasks",
        json=payload,
    )
    resp.raise_for_status()
    data = resp.json()
    dispatch = data.get("dispatch") or {}
    return {
        "ok": True,
        "task_id": data.get("id"),
        "agent": canonical_name,
        "dispatch_status": dispatch.get("status"),
        "dispatch": dispatch,
    }


async def list_open_tasks() -> dict[str, Any]:
    """Holt alle offenen Tasks (inbox + in_progress + blocked + review)."""
    resp = await _client.get(f"/api/v1/agent/boards/{JARVIS_BOARD_ID}/tasks")
    resp.raise_for_status()
    tasks = resp.json()
    open_tasks = [t for t in tasks if t.get("status") in ("inbox", "in_progress", "blocked", "review")]
    return {
        "ok": True,
        "count": len(open_tasks),
        "tasks": [
            {
                "title": t.get("title"),
                "status": t.get("status"),
                "assignee": t.get("assigned_agent_name") or "unassigned",
            }
            for t in open_tasks[:10]  # cap fuer Jarvis-Antwort
        ],
    }


async def get_agent_status(agent_name: str | None = None) -> dict[str, Any]:
    """Status eines Agents oder Übersicht aller."""
    resp = await _client.get(f"/api/v1/agent/boards/{JARVIS_BOARD_ID}/agents")
    resp.raise_for_status()
    agents = resp.json()

    if agent_name:
        for a in agents:
            if a.get("name", "").lower() == agent_name.lower():
                return {
                    "ok": True,
                    "name": a.get("name"),
                    "status": a.get("status"),
                    "current_task": a.get("current_task_id"),
                }
        return {"ok": False, "error": f"Agent '{agent_name}' nicht gefunden"}

    # Overview
    return {
        "ok": True,
        "summary": [
            {"name": a.get("name"), "status": a.get("status")}
            for a in agents
        ],
    }


async def vault_briefing() -> dict[str, Any]:
    """Fetch pre-session briefing JSON from MC backend.

    Calls GET /api/v1/agent/vault/briefing (requires vault:read scope).
    Returns the raw JSON shape:
      {current_iso, current_time_of_day_de, open_tasks, open_approvals_count,
       recent_lessons, recent_writes, agents_online, agents_offline,
       [error]}

    On HTTP failure the response is wrapped in {ok: False, error}.
    """
    resp = await _client.get("/api/v1/agent/vault/briefing")
    if resp.status_code != 200:
        return {"ok": False, "error": f"Briefing fetch failed ({resp.status_code})"}
    data = resp.json()
    data["ok"] = True
    return data


async def vault_write_note(
    content: str,
    type: str = "note",
    tags: list[str] | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """POST /api/v1/agent/vault/note (writes via inbox envelope).

    Requires vault:write scope on the voice agent token.
    idempotency_key omitted → backend generates a fresh one per call
    (each voice utterance = new note, no dedup needed).
    """
    if not title:
        first_line = (content.strip().split("\n", 1)[0] or "note")[:80]
        title = first_line

    payload: dict[str, Any] = {
        "title": title,
        "content": content,
        "type": type,
        "tags": tags or [],
    }
    resp = await _client.post("/api/v1/agent/vault/note", json=payload)
    resp.raise_for_status()
    return resp.json()


async def vault_search(
    query: str,
    agent: str | None = None,
    type: str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """GET /api/v1/agent/vault/search (FTS5 full-text search).

    Requires vault:read scope on the voice agent token.
    limit is capped at 20 by the backend (Query(le=50), voice caps at 20).

    Smart-query behaviour (added 2026-05-18):
    1. Der Operator spricht Sätze ("zeig mir das heutige Morgen-Briefing"). FTS5
       per Default = AND aller Tokens → "heutig" oder "zeig" sind in
       keiner Note → 0 Treffer obwohl "Morgen-Briefing" existiert.
    2. Wir filtern Stoppwörter via _smart_query und schicken die
       reduzierten Keywords ans Backend.
    3. Falls trotzdem 0 Treffer + Original-Query hatte ≥2 nutzbare Tokens:
       zweiter Versuch mit der Original-Query (manchmal hat das STT
       Eigennamen als "Stoppwort"-ähnliche Wörter zerlegt).
    """
    cleaned, or_fallback_useful = _smart_query(query)
    params: dict[str, Any] = {"q": cleaned, "limit": limit}
    if agent:
        params["agent"] = agent
    if type:
        params["type"] = type
    resp = await _client.get("/api/v1/agent/vault/search", params=params)
    resp.raise_for_status()
    data = resp.json()
    hits = data.get("hits") or []
    # Falls Smart-Query 0 Treffer ergab UND wir gefiltert haben → 2nd-try
    # mit dem unmodifizierten Original (z.B. wenn der Operator ein Buzzword sagt
    # das im Stoppwort-Filter mit drin steht).
    if not hits and or_fallback_useful and cleaned != query.strip():
        logger.info(
            "vault_search retry: smart-query %r→0 hits, retrying with original %r",
            cleaned, query,
        )
        params["q"] = query
        resp2 = await _client.get("/api/v1/agent/vault/search", params=params)
        if resp2.status_code == 200:
            data = resp2.json()
    return data


# Substrings the backend uses inside HTTPException details for the
# telegram-send pipeline. Ordered: first match wins. Keep the failure modes
# voice can actually explain to the operator in human-friendly German.
_TELEGRAM_REASON_MAP: tuple[tuple[str, str], ...] = (
    ("file too large", "file_too_large"),                  # backend telegram_reports too-large
    ("Reports-Bot nicht konfiguriert", "bot_unconfigured"),
    ("Wrapper hat keinen attachment_path", "wrapper_no_attachment"),  # document/url kind
    ("Vault-Wrapper nicht gefunden", "wrapper_not_found"),
    ("attachment fehlt", "attachment_missing"),            # hardlink broken
    ("attachment_path verlaesst", "attachment_unsafe"),    # path traversal blocked
    ("Wrapper-Frontmatter ungueltig", "wrapper_invalid"),
    ("schliessen sich aus", "input_mutex"),                # vault_path + deliverable_id
    ("Telegram-Limit", "text_too_long"),
    ("Telegram-Send fehlgeschlagen", "telegram_send_failed"),
)


def _classify_telegram_error(detail: str | None) -> str:
    """Map a backend HTTPException detail string to a stable reason code.

    Jarvis narrates the reason; the code lets the function_tool in main.py
    pick a German sentence rather than reading the raw exception text aloud.
    Returns 'unknown_error' when no needle matches — Jarvis falls back to
    the literal detail in that case.
    """
    if not detail:
        return "unknown_error"
    haystack = detail.lower()
    for needle, code in _TELEGRAM_REASON_MAP:
        if needle.lower() in haystack:
            return code
    return "unknown_error"


async def vault_deliver_to_telegram(
    vault_path: str,
    caption: str | None = None,
) -> dict[str, Any]:
    """POST /api/v1/agent/me/telegram — ship a vault file to the operator.

    Used by Jarvis as concierge: when the operator says "schick mir den letzten
    Wetterbericht", Jarvis calls vault_search first → picks the top hit's
    path → invokes this helper with that path. Backend resolves the wrapper's
    attachment_path, validates it stays under the vault root, and forwards
    the binary via telegram_reports.send_document().

    Uses the consolidated /me/telegram endpoint (chat:write scope) with
    `vault_path` as the attachment source — one Telegram endpoint, three input
    modes (deliverable_id / document_deliverable_id / vault_path).

    Errors are translated to ``{ok: False, status, reason, error}`` instead
    of raising — Jarvis expects all tool outputs as data so it can narrate
    them. ``reason`` is a stable code (file_too_large, wrapper_not_found,
    etc.) the function_tool maps to a German sentence; ``error`` retains
    the raw backend detail for the unknown_error fallback path.

    Note: /me/telegram requires non-empty ``text`` — we pass caption (or a
    placeholder) as text since text becomes the document caption.
    """
    body: dict[str, Any] = {
        "text": caption or "Hier ist die Datei.",
        "vault_path": vault_path,
    }
    resp = await _client.post("/api/v1/agent/me/telegram", json=body)
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail")
        except Exception:
            detail = resp.text
        return {
            "ok": False,
            "status": resp.status_code,
            "reason": _classify_telegram_error(detail),
            "error": str(detail) if detail else "telegram delivery failed",
        }
    data = resp.json()
    # /me/telegram returns {ok, message_id}; preserve the legacy
    # `telegram_message_id` alias the voice main.py + tests expect.
    if data.get("ok") and "message_id" in data and "telegram_message_id" not in data:
        data["telegram_message_id"] = data["message_id"]
    return data


async def voice_display(
    kind: str,
    data: dict[str, Any],
    title: str | None = None,
) -> dict[str, Any]:
    """POST /api/v1/voice/display — push a card to the operator's VoiceDrawer.

    `kind` is one of memory|url|file|task. `data` carries kind-specific
    fields the frontend Card renders (e.g. for memory: vault_path, snippet,
    type, agent; for url: url, domain, favicon). `title` is optional;
    frontend falls back to a kind-specific default.

    Used by Jarvis function-tools (show_memory/url/file/task). Fail-soft:
    backend wraps Redis failures as 200 + ok=False, the function-tool
    surfaces that to xAI which then narrates a fallback.
    """
    body: dict[str, Any] = {"kind": kind, "data": data}
    if title:
        body["title"] = title
    resp = await _client.post("/api/v1/voice/display", json=body)
    resp.raise_for_status()
    return resp.json()


async def get_task(task_id: str) -> dict[str, Any]:
    """GET /api/v1/agent/boards/{board}/tasks/{id} — fetch one task.

    Used by show_task to populate the TaskCard with current status +
    assignee instead of guessing from voice-stale memory.
    """
    resp = await _client.get(
        f"/api/v1/agent/boards/{JARVIS_BOARD_ID}/tasks/{task_id}"
    )
    if resp.status_code != 200:
        return {"ok": False, "status": resp.status_code}
    return resp.json()


async def voice_graph_highlight(filter: dict[str, Any]) -> dict[str, Any]:
    """POST /api/v1/voice/graph-highlight — broadcasts filter command to frontend.

    Requires vault:read scope on the voice agent token. Backend validates the
    filter shape (whitelisted keys, str|list[str] values) and publishes to the
    `voice:graph-highlight` Redis channel. The frontend WS subscriber forwards
    the payload to the 3D graph (M.4 T9, frontend wiring).

    Returns the backend response verbatim:
      {"ok": True, "published_at": "<iso8601>"}
    or on Redis failure (fail-soft, HTTP 200):
      {"ok": False, "error": "<msg>", "published_at": None}
    """
    resp = await _client.post(
        "/api/v1/voice/graph-highlight",
        json={"filter": filter},
    )
    resp.raise_for_status()
    return resp.json()


async def query_memory(query: str, limit: int = 5) -> dict[str, Any]:
    """Sucht in der Knowledge-Base nach relevanten Eintraegen.

    Bug A fix (2026-05-14): Endpoint ist /api/v1/agent/knowledge (NICHT
    /api/v1/agent/boards/{id}/knowledge — letzteres returnt 404). Der Endpoint
    filtert selbst nach agent.board_id + agent.id + globalen Eintraegen
    (siehe agent_scoped.py:agent_list_knowledge).

    Smart-query (2026-05-18): Stoppwoerter raus bevor die Query ans
    Backend geht — Knowledge.search ist `LIKE %q%`, also wenig
    tolerant gegenueber "heutiges morgen briefing"-artigen Phrasen.
    Bei 0 Treffern fallback auf Original-Query.
    """
    cleaned, or_fallback_useful = _smart_query(query)
    resp = await _client.get(
        "/api/v1/agent/knowledge",
        params={"search": cleaned, "limit": limit},
    )
    if resp.status_code != 200:
        return {"ok": False, "error": f"Knowledge query failed ({resp.status_code})"}
    entries = resp.json()
    # Fallback: Wenn Smart-Filter 0 Treffer ergab und es etwas zu filtern gab,
    # versuche es nochmal mit dem Original-String. Ist billig (in-memory LIKE).
    if not entries and or_fallback_useful and cleaned != query.strip():
        logger.info(
            "query_memory retry: smart-query %r→0 hits, retrying with original %r",
            cleaned, query,
        )
        resp2 = await _client.get(
            "/api/v1/agent/knowledge",
            params={"search": query, "limit": limit},
        )
        if resp2.status_code == 200:
            entries = resp2.json()
    return {
        "ok": True,
        "count": len(entries),
        "entries": [
            {
                "title": e.get("title") or (e.get("content") or "")[:80],
                "type": e.get("memory_type"),
                "snippet": (e.get("content") or "")[:200],
            }
            for e in entries[:limit]
        ],
    }
