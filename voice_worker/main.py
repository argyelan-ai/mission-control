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
- Provider/Modell/Stimme kommen aus Jarvis' Runtime-Bindung in MC (ADR-082,
  ``GET /api/v1/agent/voice/config``, gepullt pro Anruf in ``entrypoint()``) —
  umschaltbar im MC-Runtime-Picker wie bei jedem anderen Agenten. Die
  Entscheidungslogik (MC schlaegt Env, Env schlaegt Hardcoded-Default, nie
  verstummen) sitzt in ``jarvis_core.voice_provider.resolve_voice_choice``.
  `VOICE_PROVIDER`/`VOICE_MODEL`/`VOICE_*_VOICE_ID` env vars bleiben der
  Rueckfall, wenn MC nicht antwortet oder nichts gebunden ist.
- Sprache: Auto-detect (das Realtime-Modell antwortet in der Sprache des
  Inputs — Deutsch ok)
"""

import logging
import random

from livekit.agents import Agent, AgentSession, JobContext, WorkerOptions, cli, function_tool
from livekit.plugins import openai, xai

from jarvis_core import frontier, mc_client, tools as jtools
from jarvis_core.channels import VOICE
from jarvis_core.persona import build_instructions
from jarvis_core.voice_provider import VoiceChoice, resolve_voice_choice

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


def _build_realtime_transport(choice: VoiceChoice):
    """The "realtime" api builder — livekit's Realtime plugins (openai/xai).

    Split out from ``_build_realtime_model`` so a second api gets its own
    function rather than a growing if/elif (ADR-082 follow-up: OpenAI's Live
    API — v1/live/sessions, WebSocket/WebRTC/SIP, a genuinely different wire
    protocol from Realtime — will need a ``_build_live_transport`` here and
    one new entry in ``_API_TRANSPORTS`` below; nothing else in this module
    changes).
    """
    if choice.provider == "openai":
        return openai.realtime.RealtimeModel(
            model=choice.model or "gpt-realtime-2.1",
            voice=choice.voice,
            turn_detection=_TURN_DETECTION,
        )

    if choice.provider == "xai":
        return xai.realtime.RealtimeModel(
            voice=choice.voice,
            turn_detection=_TURN_DETECTION,
        )

    raise RuntimeError(f"Unknown voice provider {choice.provider!r} from resolve_voice_choice.")


#: Which ``VoiceChoice.api`` values this worker image can actually build a
#: transport for. "live" (OpenAI's Live API) is a recognized value from
#: ``classify_voice_api`` but has no builder here yet — deliberately: this PR
#: only wires the runtime binding + a loud, logged refusal (see entrypoint()),
#: not the Live transport itself. Adding it later is one function + one entry.
_API_TRANSPORTS = {
    "realtime": _build_realtime_transport,
}


def _build_realtime_model(choice: VoiceChoice):
    """Baut das Realtime-LLM aus einer bereits entschiedenen ``VoiceChoice``.

    Die Entscheidung WELCHER Anbieter/Modell/Stimme selbst liegt in
    ``jarvis_core.voice_provider.resolve_voice_choice`` (ADR-082, MC-Runtime-
    Bindung schlaegt Env) — hier bleibt nur der livekit-Plugin-Aufbau, den
    dieses Modul bewusst als einzigen livekit-Import traegt (siehe
    voice_provider-Docstring: Trennung wegen der stumm uebersprungenen
    Worker-Tests, Memory 2026-08-21).

    Erwartet ein bereits api-geprueftes ``choice`` (entrypoint() faellt auf
    einen unterstuetzten Wert zurueck, BEVOR dieses hier gerufen wird) — der
    RuntimeError unten ist die letzte Verteidigungslinie, kein normaler Pfad.
    """
    logger.info(choice.as_log())

    builder = _API_TRANSPORTS.get(choice.api)
    if builder is None:
        raise RuntimeError(
            f"No transport for voice api {choice.api!r} — entrypoint() should "
            f"have fallen back before reaching this point."
        )
    return builder(choice)


class VoiceAssistant(Agent):
    """Jarvis — der persoenliche Voice-Assistant des Operators.

    Persona + Tool-Logik kommen aus ``jarvis_core`` (ADR-061); die
    ``@function_tool``-Methoden hier sind duenne Delegationen an die geteilten
    Handler mit ``channel=VOICE``. So verhaelt sich Voice identisch zum
    Telegram-Kanal (dieselbe Wahrheit), nur ueber ein anderes Transport-Modell.
    """

    def __init__(
        self,
        voice_choice: VoiceChoice,
        briefing: dict | None = None,
        operator_name: str | None = None,
    ) -> None:
        # Low-latency turn-detection: kurze Silence-Window damit der Operator schneller
        # Antworten bekommt (default ist ~700ms, wir gehen auf 400ms).
        # OpenAI + xAI Realtime akzeptieren beide dieselbe TurnDetection-Struktur
        # via dict (_TURN_DETECTION oben, provider-agnostisch).
        briefing_ctx = self._format_briefing_as_context(briefing) if briefing else None
        frontier_on = frontier.is_tool_enabled()
        super().__init__(
            instructions=build_instructions(
                VOICE,
                briefing_ctx=briefing_ctx,
                frontier_enabled=frontier_on,
                operator_name=operator_name,
            ),
            llm=_build_realtime_model(voice_choice),
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
    async def list_tasks(self, status: str | None = None, limit: int = 10) -> dict:
        """Listet Tasks, optional nach Status (auch 'done')."""
        return await jtools.dispatch(
            "list_tasks", mc_client, VOICE, {"status": status, "limit": limit}
        )

    @function_tool
    async def get_task_result(self, query: str) -> dict:
        """Das Ergebnis eines (auch abgeschlossenen) Tasks."""
        return await jtools.dispatch(
            "get_task_result", mc_client, VOICE, {"query": query}
        )

    @function_tool
    async def task_progress(self, query: str) -> dict:
        """Fortschritt eines laufenden Tasks."""
        return await jtools.dispatch(
            "task_progress", mc_client, VOICE, {"query": query}
        )

    @function_tool
    async def read_briefing(self) -> dict:
        """Liest das echte Morgenbriefing-Dokument vor."""
        return await jtools.dispatch("read_briefing", mc_client, VOICE, {})

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
            content: Markdown-Inhalt (was gemerkt werden soll)
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

        Wird automatisch beim Session-Start gerufen — laesst sich aber auch
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
        """Schickt eine Datei aus dem Brain (PDF / Screenshot / Doc) auf Telegram.

        Nutze diese Funktion bei einem expliziten 'schick mir das auf
        Telegram' oder 'ich brauch die Datei aufs Handy'. Vorher selber
        per search_notes() den passenden Treffer suchen.

        Args:
            query: Such-Stichwort (z.B. "wetterbericht staufen").
            force_path: Ist DIE Datei schon explizit gewaehlt,
                den vault_path direkt setzen — die Suche wird übersprungen.
            caption: Optionaler Begleittext auf Telegram.

        Verhalten:
        - 0 Treffer → 'nothing_found', schlag vor recherchieren zu lassen
        - 1 Treffer ODER klarer Top-Hit → sofort schicken + Bestätigung sprechen
        - 2+ aehnliche Treffer → 'ambiguous' + Kandidaten, nach der Wahl
          dann mit force_path nochmal aufrufen
        """
        return await jtools.dispatch("deliver_to_telegram", mc_client, VOICE, {
            "query": query, "force_path": force_path, "caption": caption,
        })

    @function_tool
    async def show_memory(self, query: str) -> dict:
        """Zeigt eine Vault-Notiz als Card im Voice-Drawer.

        Args:
            query: Stichworte (1-3 Begriffe, NICHT volle Saetze).
        """
        return await jtools.dispatch("show_memory", mc_client, VOICE, {"query": query})

    @function_tool
    async def show_url(self, url: str, title: str | None = None) -> dict:
        """Zeigt eine externe URL als Card im Voice-Drawer.

        Args:
            url: Vollstaendige URL (https://...)
            title: Optional Anzeige-Titel.
        """
        return await jtools.dispatch("show_url", mc_client, VOICE, {"url": url, "title": title})

    @function_tool
    async def show_file(self, query: str) -> dict:
        """Zeigt eine Vault-Datei (PDF/Image/Doc) als Card im Drawer.

        Args:
            query: Stichworte zur gesuchten Datei.
        """
        return await jtools.dispatch("show_file", mc_client, VOICE, {"query": query})

    @function_tool
    async def show_task(self, task_id: str | None = None, query: str | None = None) -> dict:
        """Zeigt einen Task als Card im Voice-Drawer.

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

    # Pull die MC-Runtime-Bindung VOR dem Modellaufbau (ADR-082) — LiveKit gibt
    # pro Anruf einen frischen Raum, ein Wechsel im Runtime-Picker wirkt also
    # ohne Container-Neustart ab dem naechsten Anruf. Fail-soft: mc_config
    # bleibt None bei Backend-Ausfall, resolve_voice_choice faellt dann auf die
    # Env-Defaults zurueck (raist nur, wenn wirklich kein API-Key existiert).
    mc_config = await mc_client.voice_config()
    voice_choice = resolve_voice_choice(mc_config)

    # Saubere Ablehnung statt stillem Fehlschlag (ADR-082 Follow-up): die
    # Bindung kann auf eine API zeigen, die dieses Image (noch) nicht bauen
    # kann — z.B. ein "gpt-live-*"-Modell (OpenAIs Live API, v1/live/sessions,
    # disjunkt von Realtime). Ohne diesen Guard wuerde _build_realtime_model
    # entweder mit dem FALSCHEN Endpoint verbinden (still falsches Verhalten)
    # oder crashen (kein Jarvis). Stattdessen: laut loggen, MC melden (damit
    # es im Activity-Feed sichtbar ist), auf die reinen Env-Defaults
    # zurueckfallen — die sind heute immer "realtime", ausser jemand setzt
    # VOICE_MODEL selbst auf einen gpt-live-*-Namen.
    if voice_choice.api not in _API_TRANSPORTS:
        logger.error(
            "voice api %r (provider=%s, model=%s) not supported by this "
            "worker image — falling back to env config",
            voice_choice.api, voice_choice.provider, voice_choice.model,
        )
        await mc_client.report_voice_unsupported(
            provider=voice_choice.provider, model=voice_choice.model, api=voice_choice.api,
        )
        voice_choice = resolve_voice_choice(None)

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

    # Anzeigename fuer die Anrede (Persona + Begruessung). Fail-soft in
    # mc_client.get_operator: ohne Namen bleibt die Persona neutral und die
    # Begruessung laesst die Anrede weg.
    operator = await mc_client.get_operator()
    operator_name = operator.get("name") if operator.get("ok") else None

    session = AgentSession()
    await session.start(
        agent=VoiceAssistant(voice_choice, briefing=briefing, operator_name=operator_name),
        room=ctx.room,
    )

    # Adaptive Begruessung mit Briefing-Snapshot (siehe Greeting-Pool unten).
    await session.generate_reply(instructions=_build_greeting(briefing, operator_name))


# ── Greeting Pool ─────────────────────────────────────────────────────
# Eintoenige Begruessungen waren ein Beschwerde-Punkt — jeder Anruf fing mit
# "Guten Tag/Abend Operator, X Tasks offen" an. Der Pool unten variiert Anrede,
# Zahlen-Einkleidung und die abschliessende Frage. Jeder Eintrag ist ein
# Template; {tasks} = Tasks-Count, {appr} = Approvals-Count, {vok} = Vokativ
# (", Mark"). Ohne konfigurierten Namen ist {vok} LEER — dann faellt die Anrede
# ganz weg, statt jemanden generisch "Operator" zu nennen. Deshalb sitzt {vok}
# in jedem Template an einer Stelle, die auch leer noch sauber klingt.
_GREETINGS_NO_APPROVALS = [
    "{tasks} Tasks im Board{vok}. Womit fangen wir an?",
    "Hey{vok} — {tasks} offen, welche zuerst?",
    "Servus{vok}, {tasks} Aufgaben warten. Was machst du als erstes?",
    "Da liegen {tasks} Tasks{vok}. Sollen wir die durchgehen?",
    "Bereit{vok}. {tasks} Tasks offen — wie willst du anfangen?",
    "Hi{vok} — {tasks} im Board. Was steht heute an?",
    "{tasks} Tasks offen{vok}. Brauchst du nen Ueberblick oder hast du was Konkretes?",
    "Hallo{vok}. {tasks} offen — soll ich was rauspicken?",
]
_GREETINGS_WITH_APPROVALS = [
    "{tasks} Tasks offen plus {appr} Approvals{vok} — die Approvals zuerst?",
    "Hey{vok} — {appr} Approvals und {tasks} Tasks. Womit machst du weiter?",
    "Servus{vok}, da haengen {appr} Approvals. Soll ich die durchgehen, oder erst die {tasks} Tasks?",
    "{appr} Approvals brauchen dich{vok}, {tasks} Tasks offen. Was zuerst?",
    "Bereit{vok}. {appr} Approvals, {tasks} Tasks — wie willst du starten?",
    "Hallo{vok} — {appr} Approvals haengen, {tasks} Tasks im Board. Approvals durchklicken?",
]
_GREETINGS_EMPTY = [
    "Hey{vok} — alles aufgeraeumt, Board ist leer. Was machst du?",
    "Kein offener Task{vok}. Soll ich was suchen oder neu anlegen?",
    "Bereit{vok}. Board ist sauber — was hast du im Kopf?",
    "Servus{vok}, nichts offen gerade. Was treibst du?",
]
_GREETINGS_FALLBACK = [
    "Hi{vok}, bin da. Was machst du?",
    "Ich hoere{vok} — was brauchst du?",
    "Bereit{vok}. Sag an.",
]


def _build_greeting(briefing: dict | None, operator_name: str | None = None) -> str:
    """Pick a randomized greeting template + render with briefing numbers.

    Falls kein Briefing da ist (Backend nicht erreichbar beim Session-Start),
    nutzen wir den Fallback-Pool — Jarvis erwaehnt dann keine Zahlen.

    ``operator_name`` ist der Anzeigename aus ``mc_client.get_operator``. Ohne
    Namen bleibt der Vokativ leer und die Begruessung kommt ganz ohne Anrede.
    """
    vok = f", {operator_name.strip()}" if (operator_name or "").strip() else ""

    if not briefing:
        line = random.choice(_GREETINGS_FALLBACK).format(vok=vok)
        return f"Sag GENAU diesen einen kurzen Satz auf Deutsch: '{line}'"

    n_tasks = len(briefing.get("open_tasks", []) or [])
    n_appr = briefing.get("open_approvals_count", 0)

    if n_tasks == 0 and n_appr == 0:
        line = random.choice(_GREETINGS_EMPTY).format(vok=vok)
    elif n_appr > 0:
        template = random.choice(_GREETINGS_WITH_APPROVALS)
        line = template.format(tasks=n_tasks, appr=n_appr, vok=vok)
    else:
        template = random.choice(_GREETINGS_NO_APPROVALS)
        line = template.format(tasks=n_tasks, vok=vok)

    return (
        f"Sag GENAU diesen einen kurzen Satz auf Deutsch (Schweizer-Hochdeutsche "
        f"Aussprache, kein englischer Akzent): '{line}'"
    )


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
