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

## GPT-Live-Transport (ADR-083)

Seit 10.09.2026 Jarvis' PRODUKTIVER Voice-Transport (Marks Entscheid: Ersatz,
kein Nebenlaeufer) ueber OpenAIs **Live API** (``gpt-live-1``, Full-Duplex
Voice-Modell, getrennt vom Denk-Backend). Ausgewaehlt per ``VOICE_API`` env var
(mit Auto-Erkennung aus ``VOICE_MODEL`` falls die Var fehlt, siehe
``_resolve_voice_api()``):

- "realtime" (default ohne Erkennung): wie bisher, ``_build_realtime_model()``
  (OpenAI/xAI Realtime WebSocket) — bleibt der dokumentierte Rueckweg.
- "live": ``_build_live_model()`` — ``GPTLiveModel`` aus dem noch offenen
  LiveKit-PR #7212 (``livekit.plugins.openai.realtime.GPTLiveModel``, Stand
  10.09.2026, SHA ``de3c5ce66058c6ab437ad41f963cbaeb39046c6d``; noch nicht auf
  PyPI). Der REGULAERE ``voice_worker/Dockerfile``-Build installiert das
  Vorab-Plugin per PR-SHA (siehe Dockerfile-Kommentar) — kein separates
  Test-Image mehr.

Delegation: ``delegation="responses"`` — ein eigenes Backend-Responses-Modell
(``_resolve_live_backend_model()``, Default ``gpt-5.6-luna`` — Latenz-Tuning
nach Marks erstem Anruf, siehe ``_build_live_model()`` Docstring) ruft unsere
``@function_tool``-Methoden exakt wie bisher; Jarvis' Denken/Tools bleiben
unveraendert in ``jarvis_core``. "Client delegation" (PR-Beispiel
``client_delegation.py``) verlangt eine Agent-Instanz OHNE jegliche Tools (die
Tools laufen dort auf einer separaten ``llm.LLM``, von der Anwendung selbst
ueber ``delegation_created``-Events getrieben) — das haette einen kompletten
Umbau unserer ~20 Tool-Handler erfordert. Siehe
``docs/decisions/083-jarvis-gpt-live-transport.md``.

Ist ``GPTLiveModel`` nicht importierbar (z.B. ein aelteres Image ohne den
Vorab-Plugin-Block) und ``VOICE_API=live`` gesetzt, faellt
``_build_llm_model()`` mit einer lauten Warnung auf ``realtime`` zurueck statt
den Worker crashen zu lassen.
"""

import logging
import os
import random

from livekit.agents import Agent, AgentSession, JobContext, WorkerOptions, cli, function_tool
from livekit.plugins import openai, xai

from jarvis_core import frontier, mc_client, tools as jtools
from jarvis_core.channels import VOICE
from jarvis_core.persona import (
    build_instructions,
    build_live_delegation_instructions,
    build_live_voice_instructions,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("voice_worker")

# GPT-Live-Plugin (ADR-083) wird im regulaeren voice_worker/Dockerfile-Build
# per PR-SHA installiert (PR #7212, noch nicht released auf PyPI). Import-
# Fehler wird trotzdem abgefangen (z.B. ein aelteres Image ohne diesen Build-
# Schritt), damit `VOICE_API=live` dann laut (statt mit einem nackten
# ImportError-Traceback) auf `realtime` zurueckfaellt.
try:
    from livekit.plugins.openai.realtime import GPTLiveModel  # type: ignore[attr-defined]
    _GPT_LIVE_AVAILABLE = True
except ImportError:
    GPTLiveModel = None  # type: ignore[assignment]
    _GPT_LIVE_AVAILABLE = False

# ADR-083 Nachschliff: livekit-plugins-openai>=~1.7 (incl. the 1.8.0 this
# vorab image force-installs) tightened RealtimeModel/xai.RealtimeModel's
# turn_detection param from "any dict" to a typed object
# (openai.types.beta.realtime.session.TurnDetection — both plugins import the
# SAME class). A plain dict now raises
# "AttributeError: 'dict' object has no attribute 'create_response'" at
# construction time (live reproduced 10.09.2026, unrelated to GPT-Live work —
# hits the realtime FALLBACK path too, which this image needs working for
# VOICE_API=realtime as the documented rollback). Import is wrapped the same
# way as GPTLiveModel: older plugin releases still accept a bare dict, so
# fall back to that shape if the typed class isn't importable.
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


# GPT-Live-Stimmen: aus dem PR-Code selbst (nicht aus Doku-Vermutungen) —
# `GPTLiveVoices = Literal["aster", "beacon", "cinder", "marin", "stone",
# "vesper"]` und `DEFAULT_VOICE = "marin"` in gpt_live_model.py (PR #7212,
# SHA de3c5ce, Stand 10.09.2026). "marin" (unser bisheriger Realtime-Default)
# ist also tatsaechlich GUELTIG fuer gpt-live-1 — keine Fehlkonfiguration.
# README des PR: "Other supported names and custom voice objects still pass
# through to the API" — die Liste ist daher als bekannt-gute Namen gefuehrt,
# nicht als hartes Schema; ein unbekannter String wird trotzdem abgelehnt
# (laut + Fallback), weil ein Tippfehler sonst erst beim ersten Anruf auffaellt.
GPT_LIVE_KNOWN_VOICES = frozenset({"aster", "beacon", "cinder", "marin", "stone", "vesper"})
GPT_LIVE_DEFAULT_VOICE = "marin"


def _resolve_live_voice() -> str:
    """Bestimmt + validiert die GPT-Live-Stimme aus ``VOICE_VOICE_ID``.

    Unbekannter String (kein dict/Custom-Voice-Objekt, nicht in
    ``GPT_LIVE_KNOWN_VOICES``) → laute Warnung + Fallback auf
    ``GPT_LIVE_DEFAULT_VOICE`` ("marin"), statt eine vermutlich falsche
    Stimme stillschweigend an die API durchzureichen.
    """
    raw = os.environ.get("VOICE_VOICE_ID", "").strip()
    if not raw:
        return GPT_LIVE_DEFAULT_VOICE
    if raw.lower() not in GPT_LIVE_KNOWN_VOICES:
        logger.warning(
            "VOICE_VOICE_ID=%r is not a known gpt-live-1 voice (known: %s) — "
            "falling back to default %r. If OpenAI added a new voice name, "
            "add it to GPT_LIVE_KNOWN_VOICES in voice_worker/main.py.",
            raw, sorted(GPT_LIVE_KNOWN_VOICES), GPT_LIVE_DEFAULT_VOICE,
        )
        return GPT_LIVE_DEFAULT_VOICE
    return raw.lower()


# Backend-Modell fuer die GPT-Live-Responses-Delegation. ADR-083 waehlte
# urspruenglich jarvis_core.frontier.resolve_model() (gpt-5.5, ein
# Reasoning-Modell ohne Effort-Limit) — Marks erster echter Anruf zeigte
# dann 16s Latenz zwischen letztem User-Item und erster Assistant-Antwort.
# GPTLiveModel's EIGENER Default ist "gpt-5.6-luna" (siehe DEFAULT_BACKEND_MODEL
# in gpt_live_model.py) — OpenAIs "Fast mode" fuer genau diesen
# Full-Duplex-Anwendungsfall, kein reines Codename-Rätsel wie im Frontier-
# Kontext, sondern der vom PR selbst gewaehlte Live-Default. Umgestellt:
# JARVIS_LIVE_BACKEND_MODEL (Default "gpt-5.6-luna") ist jetzt eine EIGENE
# Env-Var, getrennt von JARVIS_FRONTIER_MODEL — die beiden Anwendungsfaelle
# (schnelle Voice-Delegation vs. schwere ask_frontier-Analyse) brauchen
# unterschiedliche Modelle, keine gemeinsame Config mehr.
LIVE_BACKEND_DEFAULT_MODEL = "gpt-5.6-luna"


def _resolve_live_backend_model() -> str:
    return os.environ.get("JARVIS_LIVE_BACKEND_MODEL", "").strip() or LIVE_BACKEND_DEFAULT_MODEL


def _build_live_model(
    *,
    briefing_ctx: str | None = None,
    frontier_enabled: bool | None = None,
    operator_name: str | None = None,
):
    """Baut das GPT-Live-Duplex-Modell (ADR-083, vorab ueber LiveKit-PR #7212).

    ``delegation="responses"``: ein Backend-Responses-Modell fuehrt Reasoning +
    Tool-Calls, exakt wie bei ``_build_realtime_model()`` — unsere
    ``@function_tool``-Methoden funktionieren unveraendert.

    Latenz-Tuning (ADR-083 Nachschliff nach Marks erstem Anruf, 16s
    Antwortzeit): Backend-Modell auf ``_resolve_live_backend_model()``
    (Fast-Mode-Default ``gpt-5.6-luna`` statt des Reasoning-Modells
    ``gpt-5.5``), ``reasoning={"effort":"low"}``, ``text={"verbosity":"low"}``,
    ``service_tier="priority"``, ``max_output_tokens=400`` — Full-Duplex
    verlangt zuegige Antworten, nicht erschoepfende.

    Instructions-Split (ADR-083, Review-Fund): die Voice-Layer-Instructions
    (Stil/Tempo/Sprach-Switch, KEINE Tool-Regeln) gehen als Agent-Top-Level-
    ``instructions`` mit (siehe ``_build_llm_model()``/``VoiceAssistant``) —
    NICHT hier. Hier bekommt nur das Backend-Responses-Modell seine eigenen,
    vollen Verfahrens-/Tool-/Honesty-Instructions
    (``build_live_delegation_instructions()``), weil DORT die Tools
    tatsaechlich aufgerufen werden.
    """
    if not _GPT_LIVE_AVAILABLE:
        raise RuntimeError(
            "VOICE_API=live but GPTLiveModel is not importable — this image "
            "does not have the vorab-installed LiveKit PR #7212 plugin. Use "
            "the vorab-installed LiveKit PR #7212 plugin block in voice_worker/Dockerfile. Rebuild the image, or set VOICE_API=realtime."
        )
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError(
            "VOICE_API=live but OPENAI_API_KEY is not set. GPT-Live needs an "
            "OpenAI key (same as VOICE_PROVIDER=openai)."
        )
    voice = _resolve_live_voice()
    model = os.environ.get("VOICE_MODEL", "gpt-live-1")
    backend_model = _resolve_live_backend_model()
    return GPTLiveModel(
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


def _resolve_voice_api() -> str:
    """Bestimmt den Voice-Transport: explizites ``VOICE_API`` gewinnt immer.

    Fehlt ``VOICE_API`` UND ``VOICE_MODEL`` sieht wie ein GPT-Live-Modell aus
    (Praefix ``gpt-live``) → automatisch ``live`` (mit Log), statt still im
    Realtime-Pfad zu landen und ``gpt-live-1`` als ungueltiges Realtime-Modell
    an die falsche API zu schicken. Das ist live so passiert (10.09.2026:
    Compose-Service reichte nur ``VOICE_MODEL`` durch, ``VOICE_API`` fehlte im
    Container) — dieser Fallback verhindert die Wiederholung.
    """
    explicit = os.environ.get("VOICE_API", "").strip().lower()
    if explicit:
        return explicit
    model = os.environ.get("VOICE_MODEL", "").strip().lower()
    if model.startswith("gpt-live"):
        logger.info(
            "VOICE_API is not set but VOICE_MODEL=%r looks like a GPT-Live "
            "model — auto-selecting VOICE_API=live. Set VOICE_API explicitly "
            "to silence this.",
            model,
        )
        return "live"
    return "realtime"


def _build_llm_model(
    *,
    briefing_ctx: str | None = None,
    frontier_enabled: bool | None = None,
    operator_name: str | None = None,
):
    """Waehlt den Voice-Transport (``_resolve_voice_api()``) + baut das passende LLM.

    Returnt ``(llm, agent_instructions)``: das Modell-Objekt UND die
    top-level Agent-``instructions``, weil beide transport-abhaengig sind
    (ADR-083 Instructions-Split) — realtime bekommt die volle
    ``build_instructions()``-Persona wie bisher, live bekommt die kurze
    Voice-Layer-Persona (``build_live_voice_instructions()``); die vollen
    Verfahrensregeln gehen bei live stattdessen ins Backend-Responses-Modell
    (siehe ``_build_live_model()``).

    'live' faellt bei fehlendem Plugin (Produktions-Image ohne PR #7212) mit
    einer lauten Warnung + Fallback auf 'realtime' zurueck, statt den Worker
    mit einem nackten ImportError sterben zu lassen — siehe Modul-Docstring
    "GPT-Live-Transport".
    """
    api = _resolve_voice_api()
    if api == "realtime":
        llm = _build_realtime_model()
        instructions = build_instructions(
            VOICE, briefing_ctx=briefing_ctx, frontier_enabled=frontier_enabled,
            operator_name=operator_name,
        )
        return llm, instructions
    if api == "live":
        if not _GPT_LIVE_AVAILABLE:
            logger.warning(
                "VOICE_API=live requested but GPTLiveModel is not available "
                "on this image (missing LiveKit PR #7212 plugin) — falling "
                "back to VOICE_API=realtime. Use "
                "the vorab-installed LiveKit PR #7212 plugin block in voice_worker/Dockerfile for the gpt-live-1 transport."
            )
            llm = _build_realtime_model()
            instructions = build_instructions(
                VOICE, briefing_ctx=briefing_ctx, frontier_enabled=frontier_enabled,
                operator_name=operator_name,
            )
            return llm, instructions
        llm = _build_live_model(
            briefing_ctx=briefing_ctx, frontier_enabled=frontier_enabled,
            operator_name=operator_name,
        )
        instructions = build_live_voice_instructions(operator_name)
        return llm, instructions
    raise RuntimeError(f"Unknown VOICE_API={api!r}. Use 'realtime' (default) or 'live'.")


class VoiceAssistant(Agent):
    """Jarvis — der persoenliche Voice-Assistant des Operators.

    Persona + Tool-Logik kommen aus ``jarvis_core`` (ADR-061); die
    ``@function_tool``-Methoden hier sind duenne Delegationen an die geteilten
    Handler mit ``channel=VOICE``. So verhaelt sich Voice identisch zum
    Telegram-Kanal (dieselbe Wahrheit), nur ueber ein anderes Transport-Modell.
    """

    def __init__(
        self, briefing: dict | None = None, operator_name: str | None = None
    ) -> None:
        # Low-latency turn-detection: kurze Silence-Window damit der Operator schneller
        # Antworten bekommt (default ist ~700ms, wir gehen auf 400ms).
        # OpenAI + xAI Realtime akzeptieren beide dieselbe TurnDetection-Struktur
        # via dict (_TURN_DETECTION oben, provider-agnostisch).
        briefing_ctx = self._format_briefing_as_context(briefing) if briefing else None
        frontier_on = frontier.is_tool_enabled()
        # _build_llm_model() returns (llm, agent_instructions): the top-level
        # Agent instructions are transport-dependent (ADR-083 Instructions-
        # Split — realtime gets the full persona, live gets the short
        # voice-layer persona while the full procedure/tool rules go to the
        # GPT-Live backend model instead).
        llm, agent_instructions = _build_llm_model(
            briefing_ctx=briefing_ctx,
            frontier_enabled=frontier_on,
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
        agent=VoiceAssistant(briefing=briefing, operator_name=operator_name),
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

    Fail-soft: ein Fehler hier darf die Session nie stoppen.
    """
    state: dict[str, float | None] = {"last_user_at": None}

    def _on_item(event) -> None:  # ConversationItemAddedEvent, lazy-typed to
        try:                       # avoid importing voice-internal event types
            role = getattr(event.item, "role", None)
            ts = float(getattr(event, "created_at", 0.0) or 0.0)
            if role == "user":
                state["last_user_at"] = ts
            elif role == "assistant" and state["last_user_at"]:
                latency = ts - state["last_user_at"]
                logger.info("delegation_latency_s=%.2f", latency)
                state["last_user_at"] = None
        except Exception as e:  # noqa: BLE001 — logging must never break the call
            logger.debug("latency logging hook failed (non-fatal): %s", e)

    session.on("conversation_item_added", _on_item)


# ── Greeting — situational opening (ADR-083 Nachschliff, Marks Feedback) ──
#
# Vorherige Version: JEDE Begruessung rechnete Tasks/Approvals-Zahlen in den
# ersten Satz ("10 Tasks im Board, Mark. Womit fangen wir an?") — genau das
# war der Fund aus Marks erstem GPT-Live-Anruf: "das ist nicht natuerlich,
# er berichtet direkt beim Einstieg". Ersetzt durch ein SITUATIVES Oeffnen
# (Variante b aus der Review-Diskussion):
#
#   - Gruss + Vokativ, tageszeit-abhaengig, KEINE Zahlen.
#   - NUR wenn es einen echten Anlass gibt (Approval wartet, oder ein Task
#     haengt in "blocked" fest) — EIN kurzer, natuerlicher Zusatz-Satz.
#     Sonst bleibt es bei Gruss + offener Frage, wie bei einem Kollegen.
#
# (Zwei verworfene Alternativen, siehe PR/ADR: (a) Jarvis erwaehnt das
# Briefing GAR NICHT beim Einstieg, nur auf Nachfrage — verworfen, weil ein
# echtes Approval/blocked-Task dann untergeht, bis Mark zufaellig danach
# fragt; (c) Jarvis wartet 1-2s und laesst Mark zuerst reden — verworfen,
# ein GPT-Live-Call OHNE jede erste Aeusserung wirkt wie eine tote Leitung.)
_GREETINGS_PLAIN = [
    "Hey{vok}. Was liegt an?",
    "Servus{vok}, was machst du?",
    "Hi{vok} — was steht an?",
    "Bereit{vok}. Sag an.",
    "Hallo{vok}. Was brauchst du?",
    "Abend{vok}. Was treibst du?",
]
_GREETINGS_FALLBACK = [
    "Hi{vok}, bin da. Was machst du?",
    "Ich hoere{vok} — was brauchst du?",
    "Bereit{vok}. Sag an.",
]
# Zusatz-Satz NUR bei echtem Anlass, angehaengt an eine Plain-Greeting.
_URGENT_APPROVAL_SINGLE = [
    "Ein Approval wartet auf dich.",
    "Da haengt ein Approval, wenn du magst.",
]
_URGENT_APPROVAL_MULTI = [
    "{appr} Approvals warten auf dich.",
    "Es haengen {appr} Approvals, falls du Zeit hast.",
]
_URGENT_BLOCKED_NAMED = [
    "Uebrigens, '{title}' haengt fest — magst du kurz reinschauen?",
    "Ach, '{title}' ist blockiert, falls du das noch siehst.",
]
_URGENT_BLOCKED_GENERIC = [
    "Uebrigens, ein Task haengt gerade fest.",
    "Ach, da ist was blockiert, falls du kurz Zeit hast.",
]


def _urgent_note(briefing: dict) -> str | None:
    """Genau EIN kurzer Zusatz-Satz, NUR bei echtem Anlass — sonst None.

    "Echter Anlass" = ein offenes Approval (braucht Mark explizit) ODER ein
    Task mit status="blocked" (das einzige "etwas ist schiefgelaufen"-Signal,
    das die Briefing-API liefert — "failed" Tasks stehen NICHT in
    open_tasks, siehe backend/app/routers/vault.py). Reine Anzahl offener
    Tasks (inbox/in_progress/review) ist explizit KEIN Anlass mehr — das war
    genau der als unnatuerlich kritisierte Status-Report-Ton.
    """
    n_appr = briefing.get("open_approvals_count", 0) or 0
    if n_appr == 1:
        return random.choice(_URGENT_APPROVAL_SINGLE)
    if n_appr > 1:
        return random.choice(_URGENT_APPROVAL_MULTI).format(appr=n_appr)

    blocked = [t for t in (briefing.get("open_tasks") or []) if t.get("status") == "blocked"]
    if blocked:
        title = (blocked[0].get("title") or "").strip()
        # Kurz genug fuer einen gesprochenen Nebensatz; ein langer/generischer
        # Titel klingt vorgelesen statt erzaehlt — dann lieber generisch.
        if title and len(title) <= 40:
            return random.choice(_URGENT_BLOCKED_NAMED).format(title=title)
        return random.choice(_URGENT_BLOCKED_GENERIC)

    return None


def _build_greeting(briefing: dict | None, operator_name: str | None = None) -> str:
    """Situative Begruessung: Gruss + Vokativ, plus EIN Zusatz-Satz nur bei
    echtem Anlass (siehe ``_urgent_note()``) — nie ein Zahlen-Status-Report.

    Faellt kein Briefing an (Backend nicht erreichbar beim Session-Start),
    nutzt den Fallback-Pool — Jarvis erwaehnt dann nichts Inhaltliches.

    ``operator_name`` ist der Anzeigename aus ``mc_client.get_operator``. Ohne
    Namen bleibt der Vokativ leer und die Begruessung kommt ganz ohne Anrede.
    """
    vok = f", {operator_name.strip()}" if (operator_name or "").strip() else ""

    if not briefing:
        line = random.choice(_GREETINGS_FALLBACK).format(vok=vok)
        return (
            f"Sag GENAU diesen Text auf Deutsch, WORTWOERTLICH und NICHTS "
            f"SONST — kein Zusatz-Satz, keine Einleitung: '{line}'"
        )

    line = random.choice(_GREETINGS_PLAIN).format(vok=vok)
    extra = _urgent_note(briefing)
    if extra:
        line = f"{line} {extra}"

    return (
        f"Sag GENAU diesen Text auf Deutsch, WORTWOERTLICH und NICHTS "
        f"SONST — kein Zusatz-Satz, keine Einleitung, keine Nachfrage "
        f"danach, auch wenn dir spontan noch etwas Freundliches einfaellt "
        f"(Schweizer-Hochdeutsche Aussprache, kein englischer Akzent, "
        f"klingt wie ein kurzer Gruss unter Kollegen, NICHT wie ein "
        f"Statusreport): '{line}'"
    )


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
