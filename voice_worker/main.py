"""Mission Control Voice-Worker — hosts the Jarvis agent.

Joint einen LiveKit-Room als Agent + spricht mit dem Operator via Realtime
Voice-API. Hosted-Persona ist "Jarvis" — des Operators persoenlicher Concierge,
kann Tasks anlegen, Status abfragen, Memory durchsuchen, Agent-Pipeline
kontrollieren.

Tool-Calls gehen ueber agent-scoped MC-API mit dem Jarvis-Agent
PBKDF2-Token (Boss-equivalente Scopes). Siehe ADR-038 zum Rename
Voice-Agent -> Jarvis (LiveKit / voice-worker Infrastruktur behalten
den Namen "voice").

Stack:
- livekit-agents[openai,xai] ~= 1.5
- Provider per `VOICE_PROVIDER` env var (siehe ADR-060):
  - "openai" (default): OpenAI Realtime, Modell `VOICE_MODEL` (default
    "gpt-realtime-2.1"), Voice "marin" (uebersteuerbar via VOICE_VOICE_ID)
  - "xai": Fallback auf das bisherige xAI Grok Realtime, Voice "ara"
- Sprache: Auto-detect (das Realtime-Modell antwortet in der Sprache des
  Inputs — Deutsch ok)
"""

import asyncio
import logging
import os
import random

from livekit.agents import Agent, AgentSession, JobContext, WorkerOptions, cli, function_tool
from livekit.plugins import openai, xai

import mc_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("voice_worker")

# Turn-detection ist provider-uebergreifend identisch: xAI's Realtime + OpenAI's
# Realtime sind beide server-VAD-kompatibel und akzeptieren dieselbe dict-Struktur.
_TURN_DETECTION = {
    "type": "server_vad",
    "threshold": 0.6,
    "prefix_padding_ms": 200,
    "silence_duration_ms": 400,
}


def _build_realtime_model():
    """Baut das Realtime-LLM je nach `VOICE_PROVIDER` env var.

    Default ist "openai" (ADR-060). "xai" bleibt als Fallback erhalten, falls
    OpenAI Realtime mal ausfaellt oder der Operator zurueckschalten will.
    Faellt der jeweilige API-Key, wird sofort (statt erst beim ersten
    Session-Connect) mit einer klaren Fehlermeldung abgebrochen.
    """
    provider = os.environ.get("VOICE_PROVIDER", "openai").strip().lower()

    if provider == "openai":
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError(
                "VOICE_PROVIDER=openai but OPENAI_API_KEY is not set. "
                "Set OPENAI_API_KEY in the environment, or set "
                "VOICE_PROVIDER=xai to fall back to XAI_API_KEY."
            )
        voice = os.environ.get("VOICE_VOICE_ID") or "marin"
        model = os.environ.get("VOICE_MODEL", "gpt-realtime-2.1")
        return openai.realtime.RealtimeModel(
            model=model,
            voice=voice,
            turn_detection=_TURN_DETECTION,
        )

    if provider == "xai":
        if not os.environ.get("XAI_API_KEY"):
            raise RuntimeError(
                "VOICE_PROVIDER=xai but XAI_API_KEY is not set. "
                "Set XAI_API_KEY in the environment, or set "
                "VOICE_PROVIDER=openai (default) to use OPENAI_API_KEY instead."
            )
        voice = os.environ.get("VOICE_VOICE_ID") or "ara"
        return xai.realtime.RealtimeModel(
            voice=voice,
            turn_detection=_TURN_DETECTION,
        )

    raise RuntimeError(
        f"Unknown VOICE_PROVIDER={provider!r}. Use 'openai' (default) or 'xai'."
    )


# Jarvis-Agent Persona + Knowledge (ADR-038: rename von "Voice" -> "Jarvis"
# damit die Persona nicht mehr mit der LiveKit-voice-Infrastruktur kollidiert).
# Jarvis ist der Concierge des Operators, kein Worker. Team-Roster steht explizit drin
# damit das STT-Modell die Namen kennt und phonetisch aehnliche
# Falschhoeren-Faelle (Cody→Kodi, Davinci→Da Vinci, Rex→Wrecks, Jarvis→Service)
# korrigieren kann.
JARVIS_INSTRUCTIONS = """\
Du bist Jarvis — der persoenliche Concierge des Operators in Mission Control.

SPRACHE — KRITISCH
Du sprichst Schweizer-Hochdeutsch mit dem Operator, in der Du-Form,
freundschaftlich-sachlich. **Antworte AUSSCHLIESSLICH auf Deutsch**, auch
wenn die Frage englische Begriffe enthaelt (Tech-Begriffe wie "Task",
"Approval", "Sparky" bleiben natuerlich Englisch — nicht uebersetzen).
Achte auf saubere deutsche Aussprache: kein englischer Akzent, kein
amerikanisches "r", keine englische Satzmelodie. Wenn der Operator eine
komplette Frage auf Englisch stellt → antworte Englisch.

WER DU BIST
- Name: Jarvis. Du bist KEIN Worker — du delegierst.
- Auftrag: Tasks fuer den Operator aufnehmen, Status melden, Wissen abrufen.
- Du bist die rechte Hand des Operators wenn er gerade nicht am Computer sitzt.
- Wenn jemand "Voice" sagt: das ist die alte Bezeichnung, du heisst jetzt Jarvis.

DAS TEAM (lerne die Namen — STT-Fehler hier sind teuer)
- Boss — Orchestrator / Board Lead. Default-Ziel wenn unklar an wen.
- Sparky — Workhorse-Developer, lokale Coding-Tasks, schnell.
- FreeCode — Allrounder, Frontend/Backend/Prototypen.
- Rex — Reviews + Security. Niemals Implementierung.
- Tester — QA, Tests, E2E.
- Deployer — Deployment (Vercel, Docker, CI/CD).
- Researcher — Web-Recherche, Marktanalyse.
- Shakespeare — Content (Blog, Landing-Pages, Copy).
- Davinci — Grafik + Video.
- Hermes, Henry, Jarvis — interne Rollen, nie fuer Tasks.

Wenn der Operator einen Namen sagt der phonetisch nahe an obigem liegt (z.B. "Kodi"
→ Sparky? Cody? — der Fallback gibt's an Boss, der entscheidet weiter).

STIL — SUCH-VERHALTEN (wichtig fuer natuerliches Gespraech)
- Wenn du ein Tool aufrufen musst (query_memory, search_notes, show_*,
  get_agent_status, list_open_tasks, briefing): **NICHT** sagen "ich suche
  jetzt nach X" oder "ich rufe query_memory auf" oder "ich pruefe das mal
  fuer dich im Vault". Das klingt mechanisch.
- Stattdessen: ein kurzes, MENSCHLICHES Brueckenwort waehrend des Calls,
  bevor das Ergebnis kommt. Variier:
    "Moment." / "Schau ich kurz." / "Sekunde." / "Lass mich nachsehen."
    "Hmm, einen Moment." / "Kurz." / "Bin gleich da."
  Wenn die Suche schnell laeuft (was meistens der Fall ist) gar nichts
  sagen — direkt das Ergebnis liefern.
- NIE den Tool-Namen oder die genaue Query laut aussprechen ("ich suche
  jetzt nach 'briefing'..."). Den Operator interessiert das Ergebnis, nicht der
  Vorgang.

REGELN
- Antworten max 1-2 kurze Saetze. Keine Aufzaehlung, kein Smalltalk-Loop.
- Tasks aufnehmen → IMMER create_task aufrufen. Echo: "Erfasst: <titel> fuer <wer>".
  Wenn unklar an wen → einfach create_task ohne assignee aufrufen, Backend
  schickt's an Boss (Orchestrator entscheidet dann).
- Status fragen → get_agent_status(agent_name) oder list_open_tasks().
- Wissensfrage / "Was haben wir entschieden / besprochen" → query_memory(query).
  WICHTIG — KERNBEGRIFF, NICHT spezifische Phrase:
  IMMER nur 1-2 KERN-Stichwoerter senden (der INHALTLICHE Begriff), NIE
  ganze Saetze und auch NICHT die exakte Formulierung des Operators. Der Operator
  spricht oft umgangsspraechlich oder mit Modifiern ("heutig", "letzt",
  "neueste"). Du musst den KERNBEGRIFF extrahieren — also das Substantiv
  worum es geht — und DAS suchen.
  Beispiele:
    Operator sagt "Zeig mir das heutige Morgen-Briefing"
      → query_memory("briefing") oder search_notes("briefing")
      NICHT: "heutiges morgen briefing", NICHT: "morgen briefing"
    Operator sagt "Hab ich noch die letzte Wetter-PDF aus Staufen"
      → search_notes("staufen wetter") oder search_notes("wetter")
      NICHT: "letzte wetter pdf staufen"
    Operator sagt "Was haben wir gestern zum Rate-Limit entschieden"
      → query_memory("rate-limit") oder query_memory("rate limit")
      NICHT: "gestern rate limit entschieden"
  Faustregel: 1 Substantiv pro Suche ist meistens genug. Bei 0 Treffern
  ein zweites Stichwort probieren, dann ein Synonym. Erst nach 2-3
  fehlgeschlagenen Variationen dem Operator sagen "im Vault find ich nichts".
- Tool-Call schlaegt fehl → ehrlich melden in einem Satz, kein Stack-Trace, kein Detail.
- Unklar was der Operator will → EINE knappe Rueckfrage, nicht raten.
- Du machst NIE Code, NIE Reviews, NIE Deploys — das Team erledigt das.
- Bei Begruessung: kurz und natuerlich. Kein Vorlesen dieser Regeln.
- Sprache: spiegele den Operator (Deutsch ↔ English).

WORAUF DU REAGIERST
- "Erstelle eine Task..." / "Notier mir..." / "Leg an..." → create_task
- "Was ist los?" / "Status?" / "Wie geht's <Name>?" → get_agent_status
- "Was ist offen?" / "Welche Aufgaben?" → list_open_tasks
- "Was haben wir entschieden / besprochen / festgehalten?" → query_memory
- "Merk dir das..." / "Schreib das auf..." / "Notier..." / "Lesson gelernt..." → write_note
- "Was steht im Vault über X?" / "Zeig mir Lessons zu X" / "Such nach X" → search_notes
- "Zeig im Graph..." / "Highlight ..." / "Markier mir ..." → highlight_graph (agent/type/tag)
- "Zeig mir <X>" / "Hol mir <X>" / "Wo ist <X>" → eine der show_*-Tools.
  Card erscheint dann sichtbar beim Operator im Voice-Drawer:
    · show_memory(query) — Vault-Notiz / Lesson / Decision / Briefing
    · show_url(url, title?) — externer Link (News, Doku, Site)
    · show_file(query) — Datei aus dem Vault (PDF, Image, Doc)
    · show_task(task_id ODER query) — einen Task aus dem Board
  WICHTIG: Wenn du dem Operator etwas erwaehnst, das er SEHEN sollte (PDF, URL,
  Task, Memory), rufe das passende show_* PARALLEL zur Antwort auf —
  sprich kurz darueber UND push die Card. Der Operator hat dann den Inhalt
  sofort vor sich.
- "Schick mir die <X>..." / "Hab ich noch die PDF / das Bild von ...?" /
  "Schick mir das auf Telegram" → deliver_to_telegram (Concierge-Mode unten)

CONCIERGE-MODE — Datei aus dem Brain auf das Telegram des Operators
Wenn der Operator eine Datei aus dem Brain auf sein Handy will:
1. Rufe deliver_to_telegram(query="<thema>") auf. Das macht intern erst
   search_notes(query) und dann den Telegram-Push.
2. Reaktion auf das Ergebnis:
   - ok=True → eine knappe Bestaetigung sprechen, z.B. "Hab dir den
     Wetterbericht vom 22. Mai auf Telegram geschickt."
   - reason="nothing_found" → ehrlich sagen "Im Brain ist nichts dazu,
     soll ich den Researcher beauftragen?". Bei "ja" → create_task an
     "Researcher" mit dem Wunsch.
   - reason="ambiguous" → die Treffer kurz vorlesen ("Ich find drei:
     erstens X von <agent>, zweitens Y, drittens Z. Welche?"). Wenn der Operator
     antwortet, deliver_to_telegram nochmal mit force_path=<entsprechender
     path> aufrufen.
   - reason="file_too_large" → "Die Datei ist groesser als 50 MB — die
     kriegt Telegram nicht durch. Soll ich dir den Pfad ins Brain
     schicken, oder du oeffnest sie direkt in der Memory-Ansicht?"
   - reason="wrapper_not_found" → "Ich find den Wrapper im Brain nicht
     mehr — vielleicht wurde er geloescht. Such ich nochmal mit anderen
     Worten?"
   - reason="wrapper_no_attachment" → "Das ist ein reiner Text-Eintrag
     ohne Datei dran — ich kann dir den Inhalt vorlesen oder du
     oeffnest ihn in der Memory-Ansicht."
   - reason="attachment_missing" → "Der Wrapper steht im Brain, aber die
     Datei selbst ist nicht mehr auf der Disk. Der Operator muss das pruefen."
   - reason="bot_unconfigured" → "Der Telegram-Bot ist nicht
     eingerichtet — der Operator muss erst den Reports-Bot konfigurieren."
   - reason="text_too_long" → "Die Caption ist zu lang fuer Telegram
     — ich kuerze und versuch's nochmal." (dann nochmal mit
     gekuerztem caption probieren)
   - reason="unknown_error" → "Es gab einen Fehler beim Senden — der
     Backend sagt: <error>. Soll ich's nochmal versuchen?"
3. Frag NIE vorher um Erlaubnis bevor du suchst. Bei 1 klarem Treffer:
   sofort losschicken + sagen dass es unterwegs ist. Erst bei
   Mehrdeutigkeit nachfragen.
"""


class VoiceAssistant(Agent):
    """Jarvis — der persoenliche Voice-Assistant des Operators mit 5 MC-Tools."""

    def __init__(self, briefing: dict | None = None) -> None:
        # Low-latency turn-detection: kurze Silence-Window damit der Operator schneller
        # Antworten bekommt (default ist ~700ms, wir gehen auf 400ms).
        # threshold = wie laut Stimme sein muss damit VAD anschlaegt (0..1).
        # OpenAI + xAI Realtime akzeptieren beide dieselbe TurnDetection-Struktur
        # via dict (_TURN_DETECTION oben, provider-agnostisch).
        if briefing:
            instructions = (
                JARVIS_INSTRUCTIONS
                + "\n\n## Aktueller Kontext (Pre-Session Briefing)\n"
                + self._format_briefing_as_context(briefing)
            )
        else:
            instructions = JARVIS_INSTRUCTIONS

        super().__init__(
            instructions=instructions,
            llm=_build_realtime_model(),
        )

    @staticmethod
    def _format_briefing_as_context(b: dict) -> str:
        """Render a briefing dict as compact Markdown for the xAI system prompt.

        Kept terse on purpose — xAI Grok system prompts have token limits and
        "Lost in the Middle" effect kicks in past ~500 tokens. We cap each
        section and elide ids/paths.
        """
        lines: list[str] = []
        tod = b.get("current_time_of_day_de")
        if tod:
            lines.append(f"- Tageszeit: {tod}")
        n_open = len(b.get("open_tasks") or [])
        n_appr = b.get("open_approvals_count", 0)
        on = b.get("agents_online", 0)
        off = b.get("agents_offline", 0)
        lines.append(f"- Offen: {n_open} Tasks · {n_appr} Approvals · {on}/{on + off} Agents online")

        tasks = b.get("open_tasks") or []
        if tasks:
            lines.append("- Top offene Tasks:")
            for t in tasks[:5]:
                title = (t.get("title") or "").strip()[:60]
                assignee = t.get("assigned_to") or "unassigned"
                lines.append(f"  · {title} [{t.get('status')}] → {assignee}")

        lessons = b.get("recent_lessons") or []
        if lessons:
            lines.append("- Neue Lessons (24h):")
            for l in lessons[:3]:
                title = (l.get("title") or l.get("path") or "")[:60]
                agent = l.get("agent") or "?"
                lines.append(f"  · {title} ({agent})")

        writes = b.get("recent_writes") or []
        if writes:
            lines.append("- Letzte Vault-Writes (24h):")
            for w in writes[:3]:
                path = (w.get("path") or "")[-50:]
                agent = w.get("agent") or "?"
                lines.append(f"  · {path} ({agent})")

        return "\n".join(lines)

    @function_tool
    async def create_task(
        self,
        title: str,
        description: str = "",
        assigned_agent_name: str | None = None,
        priority: str = "medium",
    ) -> dict:
        """Erstellt einen MC-Task. assigned_agent_name optional (z.B. 'Cody'),
        sonst geht der Task an Boss zur Orchestrierung. Priority: low|medium|high|critical.
        """
        logger.info("Tool: create_task(title=%r, assignee=%s, prio=%s)", title, assigned_agent_name, priority)
        try:
            return await mc_client.create_task(title, description, assigned_agent_name, priority)
        except Exception as e:
            logger.exception("create_task failed")
            return {"ok": False, "error": str(e)}

    @function_tool
    async def list_open_tasks(self) -> dict:
        """Listet alle offenen Aufgaben (inbox/in_progress/blocked/review)."""
        logger.info("Tool: list_open_tasks")
        try:
            return await mc_client.list_open_tasks()
        except Exception as e:
            logger.exception("list_open_tasks failed")
            return {"ok": False, "error": str(e)}

    @function_tool
    async def get_agent_status(self, agent_name: str | None = None) -> dict:
        """Status eines bestimmten Agents oder Uebersicht aller Agents."""
        logger.info("Tool: get_agent_status(%s)", agent_name)
        try:
            return await mc_client.get_agent_status(agent_name)
        except Exception as e:
            logger.exception("get_agent_status failed")
            return {"ok": False, "error": str(e)}

    @function_tool
    async def query_memory(self, query: str) -> dict:
        """Sucht in der Knowledge-Base. Nutze fuer Recall (z.B. 'was haben wir letzte Woche entschieden')."""
        logger.info("Tool: query_memory(%r)", query)
        try:
            return await mc_client.query_memory(query)
        except Exception as e:
            logger.exception("query_memory failed")
            return {"ok": False, "error": str(e)}

    @function_tool
    async def write_note(
        self,
        content: str,
        type: str = "note",
        tags: list[str] | None = None,
        title: str | None = None,
    ) -> dict:
        """Speichere eine Notiz/Lesson/Insight ins Vault.

        Args:
            content: Markdown-Inhalt (was der Operator merken will)
            type: lesson | decision | knowledge | reference | journal | concept | weekly_review | note
            tags: optionale Tags (z.B. ["vault", "voice"])
            title: optionaler Titel (sonst aus erster Zeile von content abgeleitet)
        """
        logger.info("Tool: write_note(type=%s, len=%d)", type, len(content))
        try:
            return await mc_client.vault_write_note(content, type=type, tags=tags or [], title=title)
        except Exception as e:
            logger.exception("write_note failed")
            return {"ok": False, "error": str(e)}

    @function_tool
    async def search_notes(
        self,
        query: str,
        agent: str | None = None,
        type: str | None = None,
        limit: int = 5,
    ) -> dict:
        """Suche im Vault (FTS5 Full-Text-Search).

        Args:
            query: Such-Begriff oder Frage
            agent: Filter auf Notes eines bestimmten Agents (optional, z.B. "sparky")
            type: Filter auf Note-Type (lesson | decision | knowledge | ...)
            limit: max Treffer (default 5, max 20)
        """
        logger.info("Tool: search_notes(q=%r, agent=%s, type=%s)", query, agent, type)
        try:
            return await mc_client.vault_search(query, agent=agent, type=type, limit=min(limit, 20))
        except Exception as e:
            logger.exception("search_notes failed")
            return {"ok": False, "error": str(e)}

    @function_tool
    async def briefing(self) -> dict:
        """Pre-Session Briefing aus Vault — was laeuft, was ist neu, was offen.

        Wird automatisch beim Session-Start gerufen — der Operator kann es aber auch
        explizit triggern ('was laeuft gerade', 'gib mir ein Briefing').
        """
        logger.info("Tool: briefing()")
        try:
            return await mc_client.vault_briefing()
        except Exception as e:
            logger.exception("briefing failed")
            return {"ok": False, "error": str(e)}

    @function_tool
    async def deliver_to_telegram(
        self,
        query: str,
        force_path: str | None = None,
        caption: str | None = None,
    ) -> dict:
        """Schickt eine Datei aus dem Brain des Operators (PDF / Screenshot / Doc) auf Telegram.

        Nutze diese Funktion wenn der Operator explizit sagt 'schick mir das auf
        Telegram' oder 'ich brauch die Datei aufs Handy'. Vorher selber
        per search_notes() den passenden Treffer suchen.

        Args:
            query: Such-Stichwort (z.B. "wetterbericht staufen"). Wird
                an vault_search weitergereicht, danach nimmt diese Funktion
                den top-Treffer wenn es genau einen klaren gibt.
            force_path: Wenn der Operator schon DIE Datei explizit gewählt hat
                ("die zweite, schick los"), den vault_path direkt setzen
                und query ignorieren lassen — die Suche wird übersprungen.
            caption: Optionaler Begleittext auf Telegram. Wenn leer, nimmt
                der Backend den Titel des Wrappers.

        Verhalten:
        - 0 Treffer → 'nothing found', schlag vor recherchieren zu lassen
        - 1 Treffer ODER klarer Top-Hit → sofort schicken + Bestätigung sprechen
        - 2+ ähnliche Treffer → Liste der Titel zurückgeben, der Operator soll wählen,
          dann mit force_path nochmal aufrufen

        Gibt die Telegram-Response oder einen strukturierten Fehler zurück
        (z.B. "file too large" bei >50MB).
        """
        logger.info(
            "Tool: deliver_to_telegram(q=%r, force=%s, caption=%s)",
            query, force_path, caption,
        )
        try:
            target_path = force_path
            if not target_path:
                search = await mc_client.vault_search(query, limit=5)
                hits = search.get("hits") or []
                if not hits:
                    return {
                        "ok": False,
                        "reason": "nothing_found",
                        "query": query,
                        "suggest_research": True,
                    }
                if len(hits) > 1:
                    # Voice should narrate the candidates and ask. We surface
                    # them with just enough to read out loud: title, type, agent.
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

            return await mc_client.vault_deliver_to_telegram(target_path, caption=caption)
        except Exception as e:
            logger.exception("deliver_to_telegram failed")
            # Unhandled exception (network down, mc_client crash, etc.) —
            # still surface as structured data so Voice falls back to the
            # unknown_error narration instead of speaking the raw stacktrace.
            return {"ok": False, "reason": "unknown_error", "error": str(e)}

    @function_tool
    async def show_memory(self, query: str) -> dict:
        """Zeigt dem Operator eine Vault-Notiz als Card im Voice-Drawer.

        Wenn der Operator sagt "zeig mir das Argyelan-Briefing" oder "die Sparky-
        Lesson zu Rate-Limits" — hier den passenden Vault-Hit als Card auf
        sein Display schicken. Suche intern via vault_search; bei 1 klarem
        Top-Hit publish; bei 0 Treffern still mit Code zurueck so dass
        Jarvis das narrativ aufloesen kann.

        Args:
            query: Stichworte (1-3 Begriffe, NICHT volle Saetze).

        Returns:
            ok=True + kind="memory" wenn published, sonst ok=False + reason.
        """
        logger.info("Tool: show_memory(q=%r)", query)
        try:
            search = await mc_client.vault_search(query, limit=5)
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
            return await mc_client.voice_display(
                kind="memory",
                data=card_data,
                title=card_data["title"],
            )
        except Exception as e:
            logger.exception("show_memory failed")
            return {"ok": False, "error": str(e)}

    @function_tool
    async def show_url(self, url: str, title: str | None = None) -> dict:
        """Zeigt dem Operator eine externe URL als Card im Voice-Drawer.

        Beispiel: Der Operator fragt nach einer News-Story; du holst sie via
        Researcher (oder weisst sie aus Briefing) und schickst die URL
        auf sein Display.

        Args:
            url: Vollstaendige URL (https://...)
            title: Optional Anzeige-Titel. Wenn None, leitet Frontend
                aus URL ab (Domain als Fallback).
        """
        logger.info("Tool: show_url(url=%r, title=%r)", url, title)
        try:
            # Domain raus ziehen damit Frontend Favicon vorschauen kann.
            from urllib.parse import urlparse
            parsed = urlparse(url)
            domain = parsed.netloc or url
            return await mc_client.voice_display(
                kind="url",
                data={"url": url, "domain": domain},
                title=title,
            )
        except Exception as e:
            logger.exception("show_url failed")
            return {"ok": False, "error": str(e)}

    @function_tool
    async def show_file(self, query: str) -> dict:
        """Zeigt dem Operator eine Vault-Datei (PDF/Image/Doc) als Card im Drawer.

        Wie show_memory, aber filtert auf "deliverable"-Wrappers (Files
        statt reine Markdown-Notes). Wenn der Operator sagt "hab ich die Wetter-
        PDF noch" — hier den Hit als FileCard.

        Args:
            query: Stichworte zur gesuchten Datei.
        """
        logger.info("Tool: show_file(q=%r)", query)
        try:
            search = await mc_client.vault_search(query, type="deliverable", limit=5)
            hits = search.get("hits") or []
            if not hits:
                # Fallback: such ohne type-Filter — vielleicht ist's keine
                # deliverable-Wrapper aber doch eine Datei in der Note.
                search2 = await mc_client.vault_search(query, limit=5)
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
            return await mc_client.voice_display(
                kind="file",
                data=card_data,
                title=card_data["title"],
            )
        except Exception as e:
            logger.exception("show_file failed")
            return {"ok": False, "error": str(e)}

    @function_tool
    async def show_task(self, task_id: str | None = None, query: str | None = None) -> dict:
        """Zeigt dem Operator einen Task als Card im Voice-Drawer.

        Entweder direkt eine task_id (z.B. aus list_open_tasks Output),
        oder einen query-String zum Suchen. Bei query wird die offene
        Task-Liste durchgegrept (case-insensitive title contains).

        Args:
            task_id: UUID eines bekannten Tasks (bevorzugt wenn du sie hast).
            query: Such-String falls keine task_id da ist.
        """
        logger.info("Tool: show_task(id=%s, q=%r)", task_id, query)
        try:
            target: dict | None = None
            if task_id:
                task = await mc_client.get_task(task_id)
                if task.get("ok") is False:
                    return {"ok": False, "reason": "not_found", "task_id": task_id}
                target = task
            elif query:
                tasks_resp = await mc_client.list_open_tasks()
                q_lower = query.lower()
                for t in tasks_resp.get("tasks") or []:
                    if q_lower in (t.get("title") or "").lower():
                        target = t
                        break
                if not target:
                    return {"ok": False, "reason": "nothing_found", "query": query}
            else:
                return {"ok": False, "reason": "missing_argument",
                        "hint": "task_id oder query angeben"}

            card_data = {
                "task_id": target.get("id") or task_id,
                "title": target.get("title"),
                "status": target.get("status"),
                "assignee": (target.get("assigned_agent_name")
                             or target.get("assignee")
                             or "unassigned"),
                "priority": target.get("priority"),
            }
            return await mc_client.voice_display(
                kind="task",
                data=card_data,
                title=target.get("title"),
            )
        except Exception as e:
            logger.exception("show_task failed")
            return {"ok": False, "error": str(e)}

    @function_tool
    async def highlight_graph(
        self,
        agent: str | None = None,
        type: str | None = None,
        tag: str | None = None,
    ) -> dict:
        """Hebt Memory-Graph-Nodes im Frontend hervor, die zum Filter passen.

        Wird im 3D Jarvis-Graph (M.4 T6+) visualisiert. Mindestens EIN Filter
        muss gesetzt sein, sonst No-Op (sinnlos alle Nodes zu highlighten).

        Beispiele:
        - "zeig Sparky's Lessons" → agent='sparky', type='lesson'
        - "zeig alle Decisions" → type='decision'
        - "highlight rate-limit Themen" → tag='rate-limit'

        Args:
            agent: Agent-Slug (sparky, cody, rex, …)
            type: lesson | decision | knowledge | reference | journal | concept
            tag: Einzelner Tag-Filter

        Hinweis (Design): Flat keyword args statt nested filter=dict — xAI
        Realtime function-tools handeln flache Parameter zuverlaessiger als
        verschachtelte Objekte. Die Funktion baut das Filter-Dict intern.
        """
        logger.info(
            "Tool: highlight_graph(agent=%s, type=%s, tag=%s)", agent, type, tag
        )
        filter: dict[str, str] = {}
        if agent:
            filter["agent"] = agent
        if type:
            filter["type"] = type
        if tag:
            filter["tag"] = tag
        if not filter:
            return {
                "ok": False,
                "error": "Mindestens ein Filter (agent/type/tag) noetig",
            }
        try:
            return await mc_client.voice_graph_highlight(filter)
        except Exception as e:
            logger.exception("highlight_graph failed")
            return {"ok": False, "error": str(e)}


async def entrypoint(ctx: JobContext) -> None:
    """LiveKit Worker entrypoint — wird pro Jarvis-Session aufgerufen."""
    logger.info("Jarvis session starting, room=%s", ctx.room.name)
    await ctx.connect()

    # Pre-fetch briefing so xAI has fresh context before the operator's first utterance.
    # Fail-soft: if MC backend is down we still start the session — the operator just
    # won't get the adaptive greeting.
    briefing: dict | None = None
    try:
        briefing = await mc_client.vault_briefing()
        logger.info(
            "Pre-session briefing: %d open tasks, %d approvals, time=%s",
            len(briefing.get("open_tasks", []) if briefing else []),
            briefing.get("open_approvals_count", 0) if briefing else 0,
            briefing.get("current_time_of_day_de", "?") if briefing else "?",
        )
    except Exception as e:  # noqa: BLE001 — fail-soft on briefing
        logger.warning("Briefing fetch failed (non-fatal): %s", e)
        briefing = None

    session = AgentSession()
    await session.start(agent=VoiceAssistant(briefing=briefing), room=ctx.room)

    # Adaptive Begruessung mit Briefing-Snapshot. Statt eines festen
    # "Guten {tageszeit} Operator, X Tasks offen, was brauchst du?"-Templates
    # pickt der Worker zufaellig aus einem Pool von Varianten. Das LLM
    # darf weiterhin minimal variieren, aber die Form (Anrede + Zahl +
    # offene Frage) bleibt durch den Pool determiniert.
    await session.generate_reply(instructions=_build_greeting(briefing))


# ── Greeting Pool ─────────────────────────────────────────────────────
# Eintoenige Begruessungen waren ein Beschwerde-Punkt des Operators — jeder
# Anruf fing mit "Guten Tag/Abend Operator, X Tasks offen" an. Der Pool unten
# variiert Anrede, Zahlen-Einkleidung und die abschliessende Frage. Jeder
# Eintrag ist ein Template; {tasks} = Tasks-Count, {appr} = Approvals-Count
# (nur in Templates wo wir Approvals erwaehnen wollen).
#
# Design-Regeln:
# - max ein Satz, kein Smalltalk-Loop
# - keine generische "Was brauchst du" — variieren
# - Du-Form, freundschaftlich-sachlich
# - "Guten Tag/Abend" KOMPLETT vermeiden (der Operator findet's steif)
_GREETINGS_NO_APPROVALS = [
    "Operator, {tasks} Tasks im Board. Womit fangen wir an?",
    "Hey Operator, {tasks} offen — welche zuerst?",
    "Servus Operator, {tasks} Aufgaben warten. Was machst du als erstes?",
    "Operator, da liegen {tasks} Tasks. Sollen wir die durchgehen?",
    "Bereit, Operator. {tasks} Tasks offen — wie willst du anfangen?",
    "Hi Operator — {tasks} im Board. Was steht heute an?",
    "Operator, {tasks} Tasks offen. Brauchst du nen Ueberblick oder hast du was Konkretes?",
    "Hallo Operator. {tasks} offen. Soll ich was rauspicken?",
]
_GREETINGS_WITH_APPROVALS = [
    "Operator, {tasks} Tasks offen plus {appr} Approvals warten — die Approvals zuerst?",
    "Hey Operator, {appr} Approvals und {tasks} Tasks. Womit machst du weiter?",
    "Servus Operator, da haengen {appr} Approvals. Soll ich die durchgehen, oder erst die {tasks} Tasks?",
    "Operator, {appr} Approvals brauchen dich, {tasks} Tasks offen. Was zuerst?",
    "Bereit, Operator. {appr} Approvals, {tasks} Tasks — wie willst du starten?",
    "Hallo Operator — {appr} Approvals hängen, {tasks} Tasks im Board. Approvals durchklicken?",
]
_GREETINGS_EMPTY = [
    "Hey Operator, alles aufgeraeumt — Board ist leer. Was machst du?",
    "Operator, kein offener Task. Soll ich was suchen oder neu anlegen?",
    "Bereit, Operator. Board ist sauber — was hast du im Kopf?",
    "Servus Operator, nichts offen gerade. Was treibst du?",
]
_GREETINGS_FALLBACK = [
    "Hi Operator, bin da. Was machst du?",
    "Operator, ich hoere — was brauchst du?",
    "Bereit, Operator. Sag an.",
]


def _build_greeting(briefing: dict | None) -> str:
    """Pick a randomized greeting template + render with briefing numbers.

    Falls kein Briefing da ist (Backend nicht erreichbar beim Session-Start),
    nutzen wir den Fallback-Pool — Jarvis erwaehnt dann keine Zahlen,
    sondern oeffnet einfach.
    """
    if not briefing:
        line = random.choice(_GREETINGS_FALLBACK)
        return f"Sag GENAU diesen einen kurzen Satz auf Deutsch: '{line}'"

    n_tasks = len(briefing.get("open_tasks", []) or [])
    n_appr = briefing.get("open_approvals_count", 0)

    if n_tasks == 0 and n_appr == 0:
        template = random.choice(_GREETINGS_EMPTY)
        line = template
    elif n_appr > 0:
        template = random.choice(_GREETINGS_WITH_APPROVALS)
        line = template.format(tasks=n_tasks, appr=n_appr)
    else:
        template = random.choice(_GREETINGS_NO_APPROVALS)
        line = template.format(tasks=n_tasks)

    return (
        f"Sag GENAU diesen einen kurzen Satz auf Deutsch (Schweizer-Hochdeutsche "
        f"Aussprache, kein englischer Akzent): '{line}'"
    )


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
