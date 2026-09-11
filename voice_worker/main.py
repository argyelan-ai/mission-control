"""Mission Control Voice-Worker — hosts the Jarvis agent.

Joint einen LiveKit-Room als Agent + spricht mit dem Operator via Realtime
Voice-API. Hosted-Persona ist "Jarvis" — des Operators persoenlicher Concierge,
kann Tasks anlegen, Status abfragen, Memory durchsuchen, Agent-Pipeline
kontrollieren.

Seit ADR-061 ist dieser Worker ein duenner Wrapper: Persona, Tool-Handler und
der MC-Client leben im geteilten Package ``jarvis_core`` und werden mit dem
Telegram-Kanal geteilt. Hier bleibt nur das LiveKit-/Voice-spezifische:
Transport-Modell-Factory, die ``@function_tool``-Methoden (delegieren an die
geteilten Handler) und die gesprochene Begruessung.

Tool-Calls gehen ueber agent-scoped MC-API mit dem Jarvis-Agent
PBKDF2-Token (Boss-equivalente Scopes). Siehe ADR-038 zum Rename
Voice-Agent -> Jarvis (LiveKit / voice-worker Infrastruktur behalten
den Namen "voice").

Stack:
- livekit-agents[openai,xai] (Version: siehe voice_worker/requirements.txt +
  voice_worker/Dockerfile — der Live-Pfad unten force-installt zusaetzlich
  aus einem LiveKit-PR-SHA).
- Provider/Modell/Stimme kommen aus Jarvis' Runtime-Bindung in MC (ADR-082,
  ``GET /api/v1/agent/voice/config``, gepullt pro Anruf in ``entrypoint()``) —
  umschaltbar im MC-Runtime-Picker wie bei jedem anderen Agenten. Die
  Entscheidungslogik (MC schlaegt Env, Env schlaegt Hardcoded-Default, nie
  verstummen) sitzt in ``jarvis_core.voice_provider.resolve_voice_choice``.
  `VOICE_PROVIDER`/`VOICE_MODEL`/`VOICE_*_VOICE_ID` env vars bleiben der
  Rueckfall, wenn MC nicht antwortet oder nichts gebunden ist.
- Welches WIRE-PROTOKOLL ein Provider/Modell-Paar spricht ("realtime" vs.
  "live") klassifiziert ``jarvis_core.voice_provider.classify_voice_api`` und
  steht als ``VoiceChoice.api`` bereit; ``_API_TRANSPORTS`` unten mappt jeden
  unterstuetzten Wert auf seinen Builder (ADR-082 Follow-up + ADR-083).
- Sprache: Auto-detect (das Modell antwortet in der Sprache des Inputs —
  Deutsch ok)

## GPT-Live-Transport (ADR-083)

Seit 10.09.2026 Jarvis' PRODUKTIVER Voice-Transport (Marks Entscheid: Ersatz,
kein Nebenlaeufer) fuer ``api="live"`` ueber OpenAIs **Live API**
(``gpt-live-1``, Full-Duplex Voice-Modell, getrennt vom Denk-Backend):

- "realtime" (``_build_realtime_transport``): wie bisher, OpenAI/xAI Realtime
  WebSocket ueber die livekit-Plugins — bleibt der dokumentierte Rueckweg.
- "live" (``_build_live_transport``): ``GPTLiveModel`` aus dem noch offenen
  LiveKit-PR #7212 (``livekit.plugins.openai.realtime.GPTLiveModel``, Stand
  10.09.2026, SHA ``de3c5ce66058c6ab437ad41f963cbaeb39046c6d``; noch nicht auf
  PyPI). Der REGULAERE ``voice_worker/Dockerfile``-Build installiert das
  Vorab-Plugin per PR-SHA (siehe Dockerfile-Kommentar) — kein separates
  Test-Image mehr.

Delegation: ``delegation="responses"`` — ein eigenes Backend-Responses-Modell
(``_resolve_live_backend_model()``, Default ``gpt-5.6-luna`` — Latenz-Tuning
nach Marks erstem Anruf, siehe ``_build_live_transport()`` Docstring) ruft
unsere ``@function_tool``-Methoden exakt wie bisher; Jarvis' Denken/Tools
bleiben unveraendert in ``jarvis_core``. "Client delegation" (PR-Beispiel
``client_delegation.py``) verlangt eine Agent-Instanz OHNE jegliche Tools (die
Tools laufen dort auf einer separaten ``llm.LLM``, von der Anwendung selbst
ueber ``delegation_created``-Events getrieben) — das haette einen kompletten
Umbau unserer ~20 Tool-Handler erfordert. Siehe
``docs/decisions/083-jarvis-gpt-live-transport.md``.

Ist ``GPTLiveModel`` nicht importierbar (z.B. das dokumentierte Rueckweg-Image
``realtime-backup-20260910`` ohne den Vorab-Plugin-Block) und die Bindung
(MC ODER die ``VOICE_MODEL``-Env) zeigt trotzdem auf ``api="live"``, faellt
``entrypoint()`` mit einer lauten Warnung + MC-Meldung
(``report_voice_unsupported``) auf einen ERZWUNGENEN Realtime-Choice zurueck
(fest ``openai``/``gpt-realtime-2.1``, nicht nur ein erneuter
``resolve_voice_choice(None)``-Aufruf — der wuerde bei gesetztem
``VOICE_MODEL=gpt-live-1`` denselben Fehler reproduzieren) — statt den Worker
crashen zu lassen oder mit dem falschen Endpoint zu verbinden.
"""

import logging
import os

from livekit.agents import Agent, AgentSession, JobContext, WorkerOptions, cli, function_tool
from livekit.plugins import openai, xai

from jarvis_core import frontier, mc_client, tools as jtools
from jarvis_core import voice_greeting as _voice_greeting
from jarvis_core.channels import VOICE
from jarvis_core.persona import (
    build_instructions,
    build_live_delegation_instructions,
    build_live_voice_instructions,
)
from jarvis_core.voice_greeting import build_greeting, track_latency
from jarvis_core.voice_provider import (
    VoiceChoice,
    resolve_live_backend_model,
    resolve_voice_choice,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("voice_worker")

# GPT-Live-Plugin (ADR-083) wird im regulaeren voice_worker/Dockerfile-Build
# per PR-SHA installiert (PR #7212, noch nicht released auf PyPI). Import-
# Fehler wird trotzdem abgefangen (z.B. ein aelteres Image ohne diesen Build-
# Schritt), damit eine Bindung auf ``api="live"`` dann laut (statt mit einem
# nackten ImportError-Traceback) auf ``realtime`` zurueckfaellt.
try:
    from livekit.plugins.openai.realtime import GPTLiveModel  # type: ignore[attr-defined]
    _GPT_LIVE_AVAILABLE = True
except ImportError:
    GPTLiveModel = None  # type: ignore[assignment]
    _GPT_LIVE_AVAILABLE = False

# ADR-083 Nachschliff: livekit-plugins-openai>=~1.7 (inkl. das 1.8.0, das der
# Live-Build-Schritt force-installiert) verschaerfte RealtimeModel/
# xai.RealtimeModel's turn_detection-Parameter von "beliebiges dict" auf ein
# typisiertes Objekt (openai.types.beta.realtime.session.TurnDetection — beide
# Plugins importieren dieselbe Klasse). Ein reines dict wirft seither beim
# Konstruieren "AttributeError: 'dict' object has no attribute
# 'create_response'" (live reproduziert 10.09.2026, unabhaengig vom
# GPT-Live-Bezug — betrifft auch den Realtime-FALLBACK-Pfad, der auf diesem
# Image funktionieren muss). Import ist genauso abgesichert wie GPTLiveModel:
# aeltere Plugin-Releases akzeptieren weiterhin ein blosses dict.
try:
    from openai.types.beta.realtime.session import TurnDetection as _TurnDetectionType
except ImportError:
    _TurnDetectionType = None  # type: ignore[assignment]

# Turn-detection ist provider-uebergreifend identisch: xAI's Realtime + OpenAI's
# Realtime sind beide server-VAD-kompatibel und akzeptieren dieselbe Struktur.
_TURN_DETECTION_KWARGS = {
    "type": "server_vad",
    "threshold": 0.6,
    "prefix_padding_ms": 200,
    "silence_duration_ms": 400,
}
_TURN_DETECTION = (
    _TurnDetectionType(**_TURN_DETECTION_KWARGS)
    if _TurnDetectionType is not None
    else dict(_TURN_DETECTION_KWARGS)
)


def _build_realtime_transport(
    choice: VoiceChoice,
    *,
    briefing_ctx: str | None = None,
    frontier_enabled: bool | None = None,
    operator_name: str | None = None,
):
    """Die "realtime"-api: livekit's Realtime-Plugins (openai/xai).

    Returnt ``(llm, agent_instructions)`` wie jeder Transport-Builder in
    ``_API_TRANSPORTS`` (ADR-083 Instructions-Split) — realtime bekommt
    weiterhin die volle ``build_instructions()``-Persona als Top-Level-Agent-
    Instructions (kein Split noetig, hier ruft das EINE Modell die Tools
    selbst auf).
    """
    instructions = build_instructions(
        VOICE, briefing_ctx=briefing_ctx, frontier_enabled=frontier_enabled,
        operator_name=operator_name,
    )

    if choice.provider == "openai":
        llm = openai.realtime.RealtimeModel(
            model=choice.model or "gpt-realtime-2.1",
            voice=choice.voice,
            turn_detection=_TURN_DETECTION,
        )
        return llm, instructions

    if choice.provider == "xai":
        # model ist absichtlich WEGGELASSEN (nicht als None uebergeben), wenn
        # choice.model leer ist — der Plugin-eigene Default ist NOT_GIVEN, und
        # der Type-Hint akzeptiert None fuer `model` nicht so wie fuer `voice`.
        # Bindet MC ein Modell (z.B. grok-voice-think-fast-1.0 auf der
        # voice-xai Seed-Zeile), MUSS es ankommen — Review-Fund (2026-09-10):
        # dieser Zweig liess choice.model vorher stillschweigend fallen.
        if (choice.model or "").strip().lower().startswith("gpt-live"):
            # classify_voice_api() gibt fuer Provider "xai" IMMER "realtime"
            # zurueck (nur OpenAI spricht Live). Bindet jemand trotzdem ein
            # gpt-live-*-Modell an die voice-xai-Zeile, landet der Name hier
            # unveraendert im xAI-Realtime-Plugin und wuerde erst beim Connect
            # abgelehnt (stumme Leitung bis dahin) — Review-Fund (2026-09-10).
            # Kein harter Stop (das Modell KOENNTE in Zukunft bei xAI
            # existieren), aber laut warnen statt schweigend zu verbinden.
            logger.warning(
                "voice-xai bound to %r, which looks like an OpenAI Live model "
                "name — xAI's Realtime plugin will very likely reject this at "
                "connect time. classify_voice_api() only recognizes gpt-live-* "
                "as 'live' for provider=openai.",
                choice.model,
            )
        kwargs: dict = {"voice": choice.voice, "turn_detection": _TURN_DETECTION}
        if choice.model:
            kwargs["model"] = choice.model
        llm = xai.realtime.RealtimeModel(**kwargs)
        return llm, instructions

    raise RuntimeError(f"Unknown voice provider {choice.provider!r} from resolve_voice_choice.")


# GPT-Live-Stimmen-Validierung: lebt in jarvis_core.voice_greeting (kein
# livekit-Import dort, ADR-083 Review-Fund 10.09.2026 — dieses Modul
# importiert livekit auf Modulebene, also skippte JEDER Test dafuer still in
# CI). GPT_LIVE_KNOWN_VOICES/GPT_LIVE_DEFAULT_VOICE bleiben hier als Re-Export
# fuer bestehenden Code/Doku-Verweise.
GPT_LIVE_KNOWN_VOICES = _voice_greeting.GPT_LIVE_KNOWN_VOICES
GPT_LIVE_DEFAULT_VOICE = _voice_greeting.GPT_LIVE_DEFAULT_VOICE
_validate_live_voice = _voice_greeting.validate_live_voice


# Backend-Modell fuer die GPT-Live-Responses-Delegation. ADR-083 waehlte
# urspruenglich jarvis_core.frontier.resolve_model() (gpt-5.5, ein
# Reasoning-Modell ohne Effort-Limit) — Marks erster echter Anruf zeigte
# dann 16s Latenz zwischen letztem User-Item und erster Assistant-Antwort.
# GPTLiveModel's EIGENER Default ist "gpt-5.6-luna" (siehe DEFAULT_BACKEND_MODEL
# in gpt_live_model.py) — OpenAIs "Fast mode" fuer genau diesen
# Full-Duplex-Anwendungsfall, kein reines Codename-Raetsel wie im Frontier-
# Kontext, sondern der vom PR selbst gewaehlte Live-Default. JARVIS_LIVE_BACKEND_MODEL
# (Default "gpt-5.6-luna") ist eine EIGENE Env-Var, getrennt von
# JARVIS_FRONTIER_MODEL — die beiden Anwendungsfaelle (schnelle Voice-
# Delegation vs. schwere ask_frontier-Analyse) brauchen unterschiedliche
# Modelle, keine gemeinsame Config. Konstante + Resolver leben in
# jarvis_core.voice_provider (nicht hier) — geteilt mit
# scripts/gpt_live_protocol_smoke.py, damit dort keine zweite, driftende
# "gpt-5.5"/"gpt-5.6-luna"-Kopie entsteht (genau das brach CI: 10.09.2026,
# backend/tests/test_no_hardcoded_models.py).
_resolve_live_backend_model = resolve_live_backend_model


def _build_live_transport(
    choice: VoiceChoice,
    *,
    briefing_ctx: str | None = None,
    frontier_enabled: bool | None = None,
    operator_name: str | None = None,
):
    """Die "live"-api: ``GPTLiveModel`` (ADR-083, vorab ueber LiveKit-PR #7212).

    ``delegation="responses"``: ein Backend-Responses-Modell fuehrt Reasoning +
    Tool-Calls, exakt wie bei ``_build_realtime_transport()`` — unsere
    ``@function_tool``-Methoden funktionieren unveraendert.

    Latenz-Tuning (ADR-083 Nachschliff nach Marks erstem Anruf, 16s
    Antwortzeit): Backend-Modell auf ``_resolve_live_backend_model()``
    (Fast-Mode-Default ``gpt-5.6-luna`` statt des Reasoning-Modells
    ``gpt-5.5``), ``reasoning={"effort":"low"}``, ``text={"verbosity":"low"}``,
    ``service_tier="priority"``, ``max_output_tokens=400`` — Full-Duplex
    verlangt zuegige Antworten, nicht erschoepfende.

    Instructions-Split (ADR-083, Review-Fund): returnt ``(llm,
    agent_instructions)`` wie jeder Transport-Builder — die
    ``agent_instructions`` sind hier die KURZE Voice-Layer-Persona
    (``build_live_voice_instructions()``, Stil/Tempo/Sprach-Switch, KEINE
    Tool-Regeln). Das Backend-Responses-Modell bekommt separat seine eigenen,
    vollen Verfahrens-/Tool-/Honesty-Instructions
    (``build_live_delegation_instructions()``), weil DORT die Tools
    tatsaechlich aufgerufen werden.
    """
    if not _GPT_LIVE_AVAILABLE:
        raise RuntimeError(
            "voice api 'live' but GPTLiveModel is not importable — this "
            "image does not have the vorab-installed LiveKit PR #7212 "
            "plugin block in voice_worker/Dockerfile. Rebuild the image, or "
            "rebind the runtime to a 'realtime' api."
        )
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError(
            "voice api 'live' but OPENAI_API_KEY is not set. GPT-Live needs "
            "an OpenAI key (same as the 'openai' realtime provider)."
        )
    voice = _validate_live_voice(choice.voice)
    model = choice.model or "gpt-live-1"
    backend_model = _resolve_live_backend_model()
    llm = GPTLiveModel(
        model=model,
        voice=voice,
        delegation="responses",
        responses_options={
            "model": backend_model,
            "instructions": build_live_delegation_instructions(
                briefing_ctx=briefing_ctx,
                frontier_enabled=frontier_enabled,
                operator_name=operator_name,
            ),
            "reasoning": {"effort": "low"},
            "text": {"verbosity": "low"},
            "service_tier": "priority",
            "max_output_tokens": 400,
        },
    )
    instructions = build_live_voice_instructions(operator_name)
    return llm, instructions


#: Which ``VoiceChoice.api`` values this worker image can actually build a
#: transport for. Each builder takes ``(choice, *, briefing_ctx,
#: frontier_enabled, operator_name)`` and returns ``(llm,
#: agent_instructions)`` — both transport-dependent (ADR-083 Instructions-
#: Split). A value ``classify_voice_api`` can produce but that has no entry
#: here (future apis) is entrypoint()'s job to catch BEFORE reaching
#: ``_build_transport`` — see the guard there.
_API_TRANSPORTS = {
    "realtime": _build_realtime_transport,
    "live": _build_live_transport,
}


def _build_transport(
    choice: VoiceChoice,
    *,
    briefing_ctx: str | None = None,
    frontier_enabled: bool | None = None,
    operator_name: str | None = None,
):
    """Baut Transport-LLM + Agent-Instructions aus einer bereits entschiedenen
    ``VoiceChoice``.

    Die Entscheidung WELCHER Anbieter/Modell/Stimme/Api selbst liegt in
    ``jarvis_core.voice_provider.resolve_voice_choice`` (ADR-082, MC-Runtime-
    Bindung schlaegt Env) — hier bleibt nur der livekit-/GPTLive-Plugin-
    Aufbau, den dieses Modul bewusst als einzigen livekit-Import traegt
    (siehe voice_provider-Docstring: Trennung wegen der stumm uebersprungenen
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
    return builder(
        choice, briefing_ctx=briefing_ctx, frontier_enabled=frontier_enabled,
        operator_name=operator_name,
    )


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
        # (_TURN_DETECTION oben, provider-agnostisch).
        briefing_ctx = self._format_briefing_as_context(briefing) if briefing else None
        frontier_on = frontier.is_tool_enabled()
        # _build_transport() returns (llm, agent_instructions): die Top-Level-
        # Agent-Instructions sind transport-abhaengig (ADR-083 Instructions-
        # Split — realtime bekommt die volle Persona, live bekommt die kurze
        # Voice-Layer-Persona, waehrend die vollen Verfahrensregeln bei live
        # stattdessen ins Backend-Responses-Modell gehen).
        llm, agent_instructions = _build_transport(
            voice_choice, briefing_ctx=briefing_ctx, frontier_enabled=frontier_on,
            operator_name=operator_name,
        )
        super().__init__(instructions=agent_instructions, llm=llm)
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

    # Saubere Ablehnung statt stillem Fehlschlag (ADR-082 Follow-up, ADR-083
    # Review-Fund 10.09.2026). Zwei Faelle, beide muessen abgefangen werden
    # BEVOR _build_transport laeuft:
    #
    # (a) Die Bindung zeigt auf eine api, fuer die _API_TRANSPORTS gar keinen
    #     Builder hat (zukuenftiger api-Wert, den classify_voice_api schon
    #     kennt, dieses Image aber noch nicht baut). Aktuell unerreichbar
    #     (beide heutigen Werte "realtime"/"live" sind registriert), bleibt
    #     als Verteidigungslinie fuer einen kuenftigen dritten Wert.
    #
    # (b) api == "live", aber GPTLiveModel ist auf DIESEM Image nicht
    #     importierbar (kein Vorab-Plugin-Build-Schritt, z.B. das
    #     dokumentierte Rueckweg-Image realtime-backup-20260910). Vorher
    #     ENDETE das hier in _build_live_transport()s RuntimeError, weil
    #     "live" laengst in _API_TRANSPORTS registriert ist — der alte Guard
    #     (nur "api not in _API_TRANSPORTS") kann diesen Fall NIE mehr fangen.
    #     resolve_voice_choice(None) allein reicht als Fix NICHT: die Prod-.env
    #     hat VOICE_MODEL=gpt-live-1 gesetzt, also klassifiziert der reine
    #     Env-Fallback erneut api="live" — derselbe Fehler waere sofort
    #     zurueck. Deshalb hier ein ERZWUNGENER Realtime-Choice, der jedes
    #     Modellnamen-Signal (MC UND Env) ignoriert.
    if voice_choice.api not in _API_TRANSPORTS or (
        voice_choice.api == "live" and not _GPT_LIVE_AVAILABLE
    ):
        logger.error(
            "voice api %r (provider=%s, model=%s) not supported by this "
            "worker image — forcing a realtime fallback so Jarvis never "
            "goes silent (a plain env re-resolve is not enough here, see "
            "code comment)",
            voice_choice.api, voice_choice.provider, voice_choice.model,
        )
        await mc_client.report_voice_unsupported(
            provider=voice_choice.provider, model=voice_choice.model, api=voice_choice.api,
        )
        voice_choice = VoiceChoice(
            provider="openai",
            model="gpt-realtime-2.1",
            voice=(voice_choice.voice or "marin"),
            source="live-unavailable-fallback",
            api="realtime",
        )

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
    _attach_latency_logging(session)
    await session.start(
        agent=VoiceAssistant(voice_choice, briefing=briefing, operator_name=operator_name),
        room=ctx.room,
    )

    # Adaptive Begruessung mit Briefing-Snapshot (siehe Greeting-Pool unten).
    await session.generate_reply(instructions=_build_greeting(briefing, operator_name))


def _attach_latency_logging(session: AgentSession) -> None:
    """Misst + loggt die Verzoegerung zwischen dem letzten User-Item und der
    ersten Assistant-Antwort pro Gespraechsrunde (ADR-083 Nachschliff).

    Konkreter Anlass: Marks erster GPT-Live-Anruf zeigte 16s zwischen "Erzähl
    ein bitz" und Jarvis' Antwort — mit dieser Zahl allein im Transkript war
    unklar, ob das die Backend-Delegation war oder etwas anderes. Ab jetzt
    steht ``delegation_latency_s=…`` pro Runde im Log, egal ob Realtime oder
    GPT-Live (``session.on("conversation_item_added")`` ist transport-
    unabhaengig — kein GPT-Live-spezifischer Hook noetig).

    Die eigentliche Latenz-Arithmetik ist ``jarvis_core.voice_greeting.
    track_latency()`` (pure, livekit-frei, ADR-083 Review-Fund — laeuft jetzt
    im gewoehnlichen Backend-Testjob statt still zu skippen). Hier bleibt nur
    das livekit-spezifische Event-Handling + Logging.

    Fail-soft: ein Fehler hier darf die Session nie stoppen.
    """
    state: dict[str, float | None] = {"last_user_at": None}

    def _on_item(event) -> None:  # ConversationItemAddedEvent, lazy-typed to
        try:                       # avoid importing voice-internal event types
            role = getattr(event.item, "role", None)
            ts = float(getattr(event, "created_at", 0.0) or 0.0)
            state["last_user_at"], latency = track_latency(state["last_user_at"], role, ts)
            if latency is not None:
                logger.info("delegation_latency_s=%.2f", latency)
        except Exception as e:  # noqa: BLE001 — logging must never break the call
            logger.debug("latency logging hook failed (non-fatal): %s", e)

    session.on("conversation_item_added", _on_item)


# Begruessung: lebt in jarvis_core.voice_greeting (kein livekit-Import dort,
# ADR-083 Review-Fund 10.09.2026) — build_greeting/urgent_note laufen jetzt
# im gewoehnlichen Backend-Testjob statt still zu skippen. _urgent_note bleibt
# hier als Re-Export fuer bestehende Doku-Verweise.
_urgent_note = _voice_greeting.urgent_note
_build_greeting = build_greeting


if __name__ == "__main__":
    # AGENT_NAME (optional, default unset): leer = automatischer Dispatch, wie
    # der Produktions-Worker heute (LiveKit dispatcht jede neue Room-Job an
    # jeden Worker ohne agent_name). Gesetzt = NUR explizites Dispatch
    # (RoomConfiguration.agents=[{agent_name: ...}] oder CreateDispatch API)
    # erreicht diesen Worker — so kann ein Test-Worker NEBEN dem
    # Produktions-Worker laufen, ohne ihm Anrufe wegzuschnappen (siehe
    # docs/decisions/083-jarvis-gpt-live-transport.md).
    agent_name = os.environ.get("AGENT_NAME", "").strip()
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, agent_name=agent_name))
