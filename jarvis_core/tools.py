"""Provider-neutrale Jarvis-Tools (ADR-061).

Jedes Tool ist ein ``ToolSpec``: Name, Beschreibung, JSON-Schema der Parameter,
ein async Handler und die Menge der Kanaele, auf denen es verfuegbar ist.

Der Handler bekommt immer ``(client, channel, **kwargs)``:
- ``client`` — ein Objekt mit den ``mc_client``-Koroutinen (create_task,
  vault_search, voice_display, ...). In Produktion ist das das Modul
  ``jarvis_core.mc_client``; in Tests ein Mock.
- ``channel`` — der aktive ``Channel``; Handler degradieren ihr Verhalten
  anhand von ``channel.supports_cards`` / ``supports_graph_highlight``.

Die Voice-Wrapper (LiveKit ``@function_tool`` in ``voice_worker/main.py``) und der
Text-``JarvisBrain`` (Telegram) rufen dieselben Handler auf — eine einzige Quelle
der Tool-Wahrheit fuer alle Kanaele.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from jarvis_core.channels import Channel

logger = logging.getLogger("jarvis_core.tools")

Handler = Callable[..., Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ToolSpec:
    """Eine kanal-agnostische Tool-Definition."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema (OpenAI function-tool "parameters")
    handler: Handler
    #: Kanal-Namen, auf denen dieses Tool angeboten wird. Leer = alle.
    channels: frozenset[str] = field(default_factory=frozenset)

    def available_on(self, channel: Channel) -> bool:
        return not self.channels or channel.name in self.channels


# ────────────────────────────────────────────────────────────────────────
# Handler
# ────────────────────────────────────────────────────────────────────────


async def _create_task(
    client,
    channel: Channel,
    title: str,
    description: str = "",
    assigned_agent_name: str | None = None,
    priority: str = "medium",
) -> dict:
    logger.info("Tool: create_task(title=%r, assignee=%s, prio=%s)", title, assigned_agent_name, priority)
    try:
        return await client.create_task(title, description, assigned_agent_name, priority)
    except Exception as e:
        logger.exception("create_task failed")
        return {"ok": False, "error": str(e)}


async def _list_open_tasks(client, channel: Channel) -> dict:
    logger.info("Tool: list_open_tasks")
    try:
        return await client.list_open_tasks()
    except Exception as e:
        logger.exception("list_open_tasks failed")
        return {"ok": False, "error": str(e)}


async def _get_agent_status(client, channel: Channel, agent_name: str | None = None) -> dict:
    logger.info("Tool: get_agent_status(%s)", agent_name)
    try:
        return await client.get_agent_status(agent_name)
    except Exception as e:
        logger.exception("get_agent_status failed")
        return {"ok": False, "error": str(e)}


async def _query_memory(client, channel: Channel, query: str) -> dict:
    logger.info("Tool: query_memory(%r)", query)
    try:
        return await client.query_memory(query)
    except Exception as e:
        logger.exception("query_memory failed")
        return {"ok": False, "error": str(e)}


async def _write_note(
    client,
    channel: Channel,
    content: str,
    type: str = "note",
    tags: list[str] | None = None,
    title: str | None = None,
) -> dict:
    logger.info("Tool: write_note(type=%s, len=%d)", type, len(content or ""))
    try:
        return await client.vault_write_note(content, type=type, tags=tags or [], title=title)
    except Exception as e:
        logger.exception("write_note failed")
        return {"ok": False, "error": str(e)}


async def _search_notes(
    client,
    channel: Channel,
    query: str,
    agent: str | None = None,
    type: str | None = None,
    limit: int = 5,
) -> dict:
    logger.info("Tool: search_notes(q=%r, agent=%s, type=%s)", query, agent, type)
    try:
        return await client.vault_search(query, agent=agent, type=type, limit=min(limit, 20))
    except Exception as e:
        logger.exception("search_notes failed")
        return {"ok": False, "error": str(e)}


async def _briefing(client, channel: Channel) -> dict:
    logger.info("Tool: briefing()")
    try:
        return await client.vault_briefing()
    except Exception as e:
        logger.exception("briefing failed")
        return {"ok": False, "error": str(e)}


async def _deliver_to_telegram(
    client,
    channel: Channel,
    query: str,
    force_path: str | None = None,
    caption: str | None = None,
) -> dict:
    logger.info(
        "Tool: deliver_to_telegram(q=%r, force=%s, caption=%s)", query, force_path, caption
    )
    try:
        target_path = force_path
        if not target_path:
            search = await client.vault_search(query, limit=5)
            hits = search.get("hits") or []
            if not hits:
                return {
                    "ok": False,
                    "reason": "nothing_found",
                    "query": query,
                    "suggest_research": True,
                }
            if len(hits) > 1:
                return {
                    "ok": False,
                    "reason": "ambiguous",
                    "candidates": [
                        {
                            "title": h.get("title") or h.get("path", "").split("/")[-1],
                            "type": h.get("type"),
                            "agent": h.get("agent"),
                            "path": h.get("path"),
                        }
                        for h in hits[:5]
                    ],
                }
            target_path = hits[0]["path"]

        return await client.vault_deliver_to_telegram(target_path, caption=caption)
    except Exception as e:
        logger.exception("deliver_to_telegram failed")
        return {"ok": False, "reason": "unknown_error", "error": str(e)}


async def _show_memory(client, channel: Channel, query: str) -> dict:
    logger.info("Tool: show_memory(q=%r)", query)
    try:
        search = await client.vault_search(query, limit=5)
        hits = search.get("hits") or []
        if not hits:
            return {"ok": False, "reason": "nothing_found", "query": query}
        top = hits[0]
        card_data = {
            "vault_path": top.get("path"),
            "title": top.get("title") or (top.get("path") or "").split("/")[-1],
            "type": top.get("type"),
            "agent": top.get("agent"),
            "date": top.get("date"),
            "snippet": (top.get("content") or "")[:280],
        }
        if not channel.supports_cards:
            # Kein Display — den Fund als Text zurueckgeben, damit der Brain ihn
            # im Chat vorlesen/zusammenfassen kann.
            return {"ok": True, "degraded": True, "kind": "memory", **card_data}
        return await client.voice_display(kind="memory", data=card_data, title=card_data["title"])
    except Exception as e:
        logger.exception("show_memory failed")
        return {"ok": False, "error": str(e)}


async def _show_url(client, channel: Channel, url: str, title: str | None = None) -> dict:
    logger.info("Tool: show_url(url=%r, title=%r)", url, title)
    try:
        parsed = urlparse(url)
        domain = parsed.netloc or url
        if not channel.supports_cards:
            # Kein Display — der Brain schickt den Link einfach im Text mit.
            return {"ok": True, "degraded": True, "kind": "url", "url": url,
                    "domain": domain, "title": title}
        return await client.voice_display(
            kind="url", data={"url": url, "domain": domain}, title=title
        )
    except Exception as e:
        logger.exception("show_url failed")
        return {"ok": False, "error": str(e)}


async def _show_file(client, channel: Channel, query: str) -> dict:
    logger.info("Tool: show_file(q=%r)", query)
    try:
        search = await client.vault_search(query, type="deliverable", limit=5)
        hits = search.get("hits") or []
        if not hits:
            search2 = await client.vault_search(query, limit=5)
            hits = search2.get("hits") or []
        if not hits:
            return {"ok": False, "reason": "nothing_found", "query": query}
        top = hits[0]
        card_data = {
            "vault_path": top.get("path"),
            "title": top.get("title") or (top.get("path") or "").split("/")[-1],
            "type": top.get("type"),
            "agent": top.get("agent"),
            "date": top.get("date"),
        }
        if not channel.supports_cards:
            # Kein Display — Metadaten zurueck + Hinweis, dass die Datei per
            # deliver_to_telegram tatsaechlich verschickt werden kann.
            return {
                "ok": True, "degraded": True, "kind": "file",
                "hint": "use deliver_to_telegram to send the file", **card_data,
            }
        return await client.voice_display(kind="file", data=card_data, title=card_data["title"])
    except Exception as e:
        logger.exception("show_file failed")
        return {"ok": False, "error": str(e)}


async def _show_task(
    client, channel: Channel, task_id: str | None = None, query: str | None = None
) -> dict:
    logger.info("Tool: show_task(id=%s, q=%r)", task_id, query)
    try:
        target: dict | None = None
        if task_id:
            task = await client.get_task(task_id)
            if task.get("ok") is False:
                return {"ok": False, "reason": "not_found", "task_id": task_id}
            target = task
        elif query:
            tasks_resp = await client.list_open_tasks()
            q_lower = query.lower()
            for t in tasks_resp.get("tasks") or []:
                if q_lower in (t.get("title") or "").lower():
                    target = t
                    break
            if not target:
                return {"ok": False, "reason": "nothing_found", "query": query}
        else:
            return {"ok": False, "reason": "missing_argument", "hint": "task_id oder query angeben"}

        card_data = {
            "task_id": target.get("id") or task_id,
            "title": target.get("title"),
            "status": target.get("status"),
            "assignee": (
                target.get("assigned_agent_name") or target.get("assignee") or "unassigned"
            ),
            "priority": target.get("priority"),
        }
        if not channel.supports_cards:
            return {"ok": True, "degraded": True, "kind": "task", **card_data}
        return await client.voice_display(kind="task", data=card_data, title=target.get("title"))
    except Exception as e:
        logger.exception("show_task failed")
        return {"ok": False, "error": str(e)}


async def _highlight_graph(
    client,
    channel: Channel,
    agent: str | None = None,
    type: str | None = None,
    tag: str | None = None,
) -> dict:
    logger.info("Tool: highlight_graph(agent=%s, type=%s, tag=%s)", agent, type, tag)
    if not channel.supports_graph_highlight:
        # Der 3D-Memory-Graph existiert nur im Voice-/Desk-Frontend.
        return {
            "ok": False,
            "reason": "desk_only",
            "message": "Graph-Highlight ist nur am Schreibtisch (Voice-Frontend) verfuegbar.",
        }
    filter: dict[str, str] = {}
    if agent:
        filter["agent"] = agent
    if type:
        filter["type"] = type
    if tag:
        filter["tag"] = tag
    if not filter:
        return {"ok": False, "error": "Mindestens ein Filter (agent/type/tag) noetig"}
    try:
        return await client.voice_graph_highlight(filter)
    except Exception as e:
        logger.exception("highlight_graph failed")
        return {"ok": False, "error": str(e)}


# ────────────────────────────────────────────────────────────────────────
# Tool-Registry
# ────────────────────────────────────────────────────────────────────────

_STR = {"type": "string"}


ALL_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="create_task",
        description=(
            "Erstellt einen MC-Task. assigned_agent_name optional (z.B. 'Cody'), "
            "sonst geht der Task an Boss zur Orchestrierung. "
            "Priority: low|medium|high|critical."
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": _STR,
                "description": {"type": "string", "default": ""},
                "assigned_agent_name": {"type": "string"},
                "priority": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
            },
            "required": ["title"],
        },
        handler=_create_task,
    ),
    ToolSpec(
        name="list_open_tasks",
        description="Listet alle offenen Aufgaben (inbox/in_progress/blocked/review).",
        parameters={"type": "object", "properties": {}},
        handler=_list_open_tasks,
    ),
    ToolSpec(
        name="get_agent_status",
        description="Status eines bestimmten Agents oder Uebersicht aller Agents.",
        parameters={
            "type": "object",
            "properties": {"agent_name": {"type": "string"}},
        },
        handler=_get_agent_status,
    ),
    ToolSpec(
        name="query_memory",
        description=(
            "Sucht in der Knowledge-Base. Nutze fuer Recall (z.B. 'was haben wir "
            "letzte Woche entschieden'). Sende 1-2 Kern-Stichwoerter, keine Saetze."
        ),
        parameters={
            "type": "object",
            "properties": {"query": _STR},
            "required": ["query"],
        },
        handler=_query_memory,
    ),
    ToolSpec(
        name="write_note",
        description=(
            "Speichere eine Notiz/Lesson/Insight ins Vault. type: "
            "lesson|decision|knowledge|reference|journal|concept|weekly_review|note."
        ),
        parameters={
            "type": "object",
            "properties": {
                "content": _STR,
                "type": {"type": "string", "default": "note"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "title": {"type": "string"},
            },
            "required": ["content"],
        },
        handler=_write_note,
    ),
    ToolSpec(
        name="search_notes",
        description=(
            "Suche im Vault (FTS5 Full-Text-Search). Optionaler Filter auf agent "
            "oder type. limit default 5, max 20."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": _STR,
                "agent": {"type": "string"},
                "type": {"type": "string"},
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
        handler=_search_notes,
    ),
    ToolSpec(
        name="briefing",
        description="Pre-Session Briefing aus Vault — was laeuft, was ist neu, was offen.",
        parameters={"type": "object", "properties": {}},
        handler=_briefing,
    ),
    ToolSpec(
        name="deliver_to_telegram",
        description=(
            "Schickt eine Datei aus dem Brain des Operators (PDF/Screenshot/Doc) "
            "auf Telegram. Vorher selber per Stichwort suchen lassen. force_path "
            "setzen, wenn der Operator schon DIE Datei gewaehlt hat."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": _STR,
                "force_path": {"type": "string"},
                "caption": {"type": "string"},
            },
            "required": ["query"],
        },
        handler=_deliver_to_telegram,
    ),
    ToolSpec(
        name="show_memory",
        description=(
            "Zeigt dem Operator eine Vault-Notiz. Am Desk als Card im Voice-Drawer, "
            "auf Telegram als Text-Zusammenfassung. Stichworte (1-3 Begriffe)."
        ),
        parameters={
            "type": "object",
            "properties": {"query": _STR},
            "required": ["query"],
        },
        handler=_show_memory,
    ),
    ToolSpec(
        name="show_url",
        description=(
            "Zeigt dem Operator eine externe URL. Am Desk als Card, auf Telegram "
            "als Link im Text."
        ),
        parameters={
            "type": "object",
            "properties": {"url": _STR, "title": {"type": "string"}},
            "required": ["url"],
        },
        handler=_show_url,
    ),
    ToolSpec(
        name="show_file",
        description=(
            "Zeigt/findet eine Vault-Datei (PDF/Image/Doc). Am Desk als Card; auf "
            "Telegram Metadaten + Hinweis, dass deliver_to_telegram sie verschickt."
        ),
        parameters={
            "type": "object",
            "properties": {"query": _STR},
            "required": ["query"],
        },
        handler=_show_file,
    ),
    ToolSpec(
        name="show_task",
        description=(
            "Zeigt einen Task. Entweder task_id oder query (Titel-Suche in offenen "
            "Tasks). Am Desk als Card, auf Telegram als Text."
        ),
        parameters={
            "type": "object",
            "properties": {"task_id": {"type": "string"}, "query": {"type": "string"}},
        },
        handler=_show_task,
    ),
    ToolSpec(
        name="highlight_graph",
        description=(
            "Hebt Memory-Graph-Nodes im 3D-Frontend hervor (agent/type/tag). NUR "
            "am Schreibtisch verfuegbar — auf Telegram nicht nutzbar."
        ),
        parameters={
            "type": "object",
            "properties": {
                "agent": {"type": "string"},
                "type": {"type": "string"},
                "tag": {"type": "string"},
            },
        },
        handler=_highlight_graph,
        channels=frozenset({"voice"}),
    ),
)


BY_NAME: dict[str, ToolSpec] = {t.name: t for t in ALL_TOOLS}


def tools_for(channel: Channel) -> list[ToolSpec]:
    """Die auf einem Kanal verfuegbaren Tools."""
    return [t for t in ALL_TOOLS if t.available_on(channel)]


def openai_tool_schemas(channel: Channel) -> list[dict[str, Any]]:
    """Die Tool-Definitionen im OpenAI function-calling Format fuer einen Kanal."""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in tools_for(channel)
    ]


async def dispatch(
    name: str, client, channel: Channel, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Fuehrt den Handler eines Tools aus.

    Unbekannte Tools oder auf diesem Kanal nicht verfuegbare Tools liefern einen
    strukturierten Fehler statt zu werfen (der Brain narrativiert ihn).
    """
    spec = BY_NAME.get(name)
    if spec is None:
        return {"ok": False, "error": f"Unbekanntes Tool: {name}"}
    if not spec.available_on(channel):
        return {
            "ok": False,
            "reason": "unavailable_on_channel",
            "message": f"Tool '{name}' ist auf {channel.label} nicht verfuegbar.",
        }
    return await spec.handler(client, channel, **(arguments or {}))
