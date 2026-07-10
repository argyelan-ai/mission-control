"""Mission Control Voice-Worker — hosts the Jarvis agent.

Joint einen LiveKit-Room als Agent + spricht mit dem Operator via Realtime
Voice-API. Hosted-Persona ist "Jarvis" — des Operators persoenlicher Concierge,
kann Tasks anlegen, Status abfragen, Memory durchsuchen, Agent-Pipeline
kontrollieren.

Seit ADR-061 ist dieser Worker ein duenner Wrapper: Persona, Tool-Handler und
der MC-Client leben im geteilten Package ``jarvis_core`` und werden mit dem
Telegram-Kanal geteilt. Hier bleibt nur das LiveKit-/Voice-spezifische:
Realtime-Modell-Factory, die ``@function_tool``-Methoden (delegieren an die
geteilten Handler) und die gesprochene Begruessung.

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

import logging
import os
import random

from livekit.agents import Agent, AgentSession, JobContext, WorkerOptions, cli, function_tool
from livekit.plugins import openai, xai

from jarvis_core import frontier, mc_client, tools as jtools
from jarvis_core.channels import VOICE
from jarvis_core.persona import build_instructions

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


class VoiceAssistant(Agent):
    """Jarvis — der persoenliche Voice-Assistant des Operators.

    Persona + Tool-Logik kommen aus ``jarvis_core`` (ADR-061); die
    ``@function_tool``-Methoden hier sind duenne Delegationen an die geteilten
    Handler mit ``channel=VOICE``. So verhaelt sich Voice identisch zum
    Telegram-Kanal (dieselbe Wahrheit), nur ueber ein anderes Transport-Modell.
    """

    def __init__(self, briefing: dict | None = None) -> None:
        # Low-latency turn-detection: kurze Silence-Window damit der Operator schneller
        # Antworten bekommt (default ist ~700ms, wir gehen auf 400ms).
        # OpenAI + xAI Realtime akzeptieren beide dieselbe TurnDetection-Struktur
        # via dict (_TURN_DETECTION oben, provider-agnostisch).
        briefing_ctx = self._format_briefing_as_context(briefing) if briefing else None
        frontier_on = frontier.is_tool_enabled()
        super().__init__(
            instructions=build_instructions(
                VOICE, briefing_ctx=briefing_ctx, frontier_enabled=frontier_on
            ),
            llm=_build_realtime_model(),
        )
        # ask_frontier ist per JARVIS_FRONTIER_ENABLED gated (Default off, ADR-062):
        # ist es aus, das Tool aus dem LiveKit-Schema entfernen, sodass das
        # Realtime-Modell es gar nicht erst anbieten/aufrufen kann (Persona-Passage
        # oben ist bereits konditional). Fail-soft: aendert sich die livekit-API,
        # greift zusaetzlich der Gate-Check in jarvis_core.tools.dispatch.
        if not frontier_on:
            try:
                remaining = [t for t in self.tools if getattr(t, "name", None) != "ask_frontier"]
                if len(remaining) != len(self.tools):
                    self.update_tools(remaining)
            except Exception as e:  # noqa: BLE001 — never block session start on this
                logger.warning("Could not strip ask_frontier tool from voice schema: %s", e)

    @staticmethod
    def _format_briefing_as_context(b: dict) -> str:
        """Render a briefing dict as compact Markdown for the realtime system prompt.

        Delegates to the shared jarvis_core formatter (ADR-061) so Voice and
        Telegram render the same age-annotated, honesty-preserving briefing text.
        """
        return jtools.format_briefing_as_context(b)

    # ── Tool-Delegationen ────────────────────────────────────────────────
    # Jede Methode reicht ihre Argumente an den geteilten jarvis_core-Handler
    # weiter (channel=VOICE). Signatur + Docstring bleiben reich, weil das
    # Realtime-Modell daraus das Tool-Schema ableitet.

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
        return await jtools.dispatch("create_task", mc_client, VOICE, {
            "title": title, "description": description,
            "assigned_agent_name": assigned_agent_name, "priority": priority,
        })

    @function_tool
    async def dispatch_to_agent(
        self,
        agent_name: str,
        instruction: str,
        priority: str = "medium",
    ) -> dict:
        """Weist einem Agenten SOFORT einen Auftrag zu, sodass er direkt loslegt.

        Anders als create_task (Backlog-Eintrag an Boss) startet dies den genannten
        Agenten unmittelbar ueber den normalen MC-Dispatch. agent_name ist Pflicht.

        Args:
            agent_name: Realer Agent (Cody, Sparky, Rex, …).
            instruction: Was der Agent tun soll — klar und vollstaendig.
            priority: low | medium | high | critical.
        """
        return await jtools.dispatch("dispatch_to_agent", mc_client, VOICE, {
            "agent_name": agent_name, "instruction": instruction, "priority": priority,
        })

    @function_tool
    async def ask_frontier(self, question: str, context_hint: str | None = None) -> dict:
        """Delegiert eine schwere Frage (Analyse/Planung/Wissen) an ein starkes Denk-Modell.

        Kuendige es kurz an ('einen Moment, ich denk kurz nach') und gib die Antwort
        danach in eigenen Worten kompakt wieder — nicht wie ein Dokument vorlesen.

        Args:
            question: Die Frage, die echtes Nachdenken braucht.
            context_hint: Optionaler Zusatzkontext.
        """
        return await jtools.dispatch("ask_frontier", mc_client, VOICE, {
            "question": question, "context_hint": context_hint,
        })

    @function_tool
    async def list_open_tasks(self) -> dict:
        """Listet alle offenen Aufgaben (inbox/in_progress/blocked/review)."""
        return await jtools.dispatch("list_open_tasks", mc_client, VOICE, {})

    @function_tool
    async def get_agent_status(self, agent_name: str | None = None) -> dict:
        """Status eines bestimmten Agents oder Uebersicht aller Agents."""
        return await jtools.dispatch("get_agent_status", mc_client, VOICE,
                                     {"agent_name": agent_name})

    @function_tool
    async def query_memory(self, query: str) -> dict:
        """Sucht in der Knowledge-Base. Nutze fuer Recall (z.B. 'was haben wir letzte Woche entschieden')."""
        return await jtools.dispatch("query_memory", mc_client, VOICE, {"query": query})

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
        return await jtools.dispatch("write_note", mc_client, VOICE, {
            "content": content, "type": type, "tags": tags, "title": title,
        })

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
        return await jtools.dispatch("search_notes", mc_client, VOICE, {
            "query": query, "agent": agent, "type": type, "limit": limit,
        })

    @function_tool
    async def briefing(self) -> dict:
        """Pre-Session Briefing aus Vault — was laeuft, was ist neu, was offen.

        Wird automatisch beim Session-Start gerufen — der Operator kann es aber auch
        explizit triggern ('was laeuft gerade', 'gib mir ein Briefing').
        """
        return await jtools.dispatch("briefing", mc_client, VOICE, {})

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
            query: Such-Stichwort (z.B. "wetterbericht staufen").
            force_path: Wenn der Operator schon DIE Datei explizit gewählt hat,
                den vault_path direkt setzen — die Suche wird übersprungen.
            caption: Optionaler Begleittext auf Telegram.

        Verhalten:
        - 0 Treffer → 'nothing_found', schlag vor recherchieren zu lassen
        - 1 Treffer ODER klarer Top-Hit → sofort schicken + Bestätigung sprechen
        - 2+ ähnliche Treffer → 'ambiguous' + Kandidaten, der Operator waehlt,
          dann mit force_path nochmal aufrufen
        """
        return await jtools.dispatch("deliver_to_telegram", mc_client, VOICE, {
            "query": query, "force_path": force_path, "caption": caption,
        })

    @function_tool
    async def show_memory(self, query: str) -> dict:
        """Zeigt dem Operator eine Vault-Notiz als Card im Voice-Drawer.

        Args:
            query: Stichworte (1-3 Begriffe, NICHT volle Saetze).
        """
        return await jtools.dispatch("show_memory", mc_client, VOICE, {"query": query})

    @function_tool
    async def show_url(self, url: str, title: str | None = None) -> dict:
        """Zeigt dem Operator eine externe URL als Card im Voice-Drawer.

        Args:
            url: Vollstaendige URL (https://...)
            title: Optional Anzeige-Titel.
        """
        return await jtools.dispatch("show_url", mc_client, VOICE, {"url": url, "title": title})

    @function_tool
    async def show_file(self, query: str) -> dict:
        """Zeigt dem Operator eine Vault-Datei (PDF/Image/Doc) als Card im Drawer.

        Args:
            query: Stichworte zur gesuchten Datei.
        """
        return await jtools.dispatch("show_file", mc_client, VOICE, {"query": query})

    @function_tool
    async def show_task(self, task_id: str | None = None, query: str | None = None) -> dict:
        """Zeigt dem Operator einen Task als Card im Voice-Drawer.

        Args:
            task_id: UUID eines bekannten Tasks (bevorzugt wenn du sie hast).
            query: Such-String falls keine task_id da ist.
        """
        return await jtools.dispatch("show_task", mc_client, VOICE,
                                     {"task_id": task_id, "query": query})

    @function_tool
    async def highlight_graph(
        self,
        agent: str | None = None,
        type: str | None = None,
        tag: str | None = None,
    ) -> dict:
        """Hebt Memory-Graph-Nodes im Frontend hervor, die zum Filter passen.

        Mindestens EIN Filter muss gesetzt sein.

        Args:
            agent: Agent-Slug (sparky, cody, rex, …)
            type: lesson | decision | knowledge | reference | journal | concept
            tag: Einzelner Tag-Filter
        """
        return await jtools.dispatch("highlight_graph", mc_client, VOICE,
                                     {"agent": agent, "type": type, "tag": tag})


async def entrypoint(ctx: JobContext) -> None:
    """LiveKit Worker entrypoint — wird pro Jarvis-Session aufgerufen."""
    logger.info("Jarvis session starting, room=%s", ctx.room.name)
    await ctx.connect()

    # Pre-fetch briefing so the realtime model has fresh context before the
    # operator's first utterance. Fail-soft: if MC backend is down we still start
    # the session — the operator just won't get the adaptive greeting.
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

    # Adaptive Begruessung mit Briefing-Snapshot (siehe Greeting-Pool unten).
    await session.generate_reply(instructions=_build_greeting(briefing))


# ── Greeting Pool ─────────────────────────────────────────────────────
# Eintoenige Begruessungen waren ein Beschwerde-Punkt des Operators — jeder
# Anruf fing mit "Guten Tag/Abend Operator, X Tasks offen" an. Der Pool unten
# variiert Anrede, Zahlen-Einkleidung und die abschliessende Frage. Jeder
# Eintrag ist ein Template; {tasks} = Tasks-Count, {appr} = Approvals-Count.
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
    nutzen wir den Fallback-Pool — Jarvis erwaehnt dann keine Zahlen.
    """
    if not briefing:
        line = random.choice(_GREETINGS_FALLBACK)
        return f"Sag GENAU diesen einen kurzen Satz auf Deutsch: '{line}'"

    n_tasks = len(briefing.get("open_tasks", []) or [])
    n_appr = briefing.get("open_approvals_count", 0)

    if n_tasks == 0 and n_appr == 0:
        line = random.choice(_GREETINGS_EMPTY)
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
