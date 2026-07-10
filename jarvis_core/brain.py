"""JarvisBrain — Text-Modus mit OpenAI Function-Calling (ADR-061).

Der Voice-Kanal nutzt das LiveKit-Realtime-Modell (Sprache rein, Sprache raus).
Text-Kanaele (Telegram) haben kein Realtime-Modell — sie brauchen einen
klassischen Chat-Completions-Loop mit Function-Calling:

1. System-Prompt (kanal-spezifische Persona) + History + User-Text an OpenAI.
2. Antwortet das Modell mit tool_calls → jeden ueber ``jarvis_core.tools.dispatch``
   ausfuehren, Ergebnis als ``role:"tool"``-Message anhaengen, erneut fragen.
3. Kommt reiner Text zurueck → das ist die finale Antwort.

Bewusst ueber ``httpx`` direkt gegen die OpenAI-REST-API statt ueber das
``openai``-SDK — das Backend hat ``httpx`` bereits, und so kommt keine neue
schwergewichtige Dependency (+ Lock-Regeneration) dazu. In Tests wird ein
gemockter HTTP-Client injiziert; es gibt nie echte API-Calls.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from jarvis_core import tools as jtools
from jarvis_core.channels import Channel

logger = logging.getLogger("jarvis_core.brain")

OPENAI_BASE_URL = "https://api.openai.com/v1"


@dataclass
class BrainResult:
    """Ergebnis eines ``JarvisBrain.respond``-Aufrufs."""

    text: str
    #: Ausgefuehrte Tool-Calls: [{"name", "arguments", "result"}]. Fuer das
    #: Bestaetigungs-Echo ("Task #42 angelegt") im Aufrufer.
    actions: list[dict[str, Any]] = field(default_factory=list)
    #: Neue Konversations-Turns (user + assistant, ohne System/Tool-Zwischenschritte)
    #: die der Aufrufer der History anhaengen und persistieren kann.
    new_turns: list[dict[str, str]] = field(default_factory=list)


class JarvisBrain:
    """Text-Modus-Gehirn fuer einen Kanal.

    Args:
        api_key: OpenAI API-Key.
        model: Chat-Modell (z.B. ``gpt-4o-mini``).
        client: ``mc_client``-artiges Objekt (die Tool-Koroutinen).
        channel: aktiver ``Channel`` (bestimmt verfuegbare Tools + Persona).
        system_prompt: fertige Persona-Instruktionen (aus ``persona.build_instructions``).
        http_client: optionaler ``httpx.AsyncClient`` (Tests injizieren einen Mock).
        max_tool_iters: Sicherheitslimit gegen Endlos-Tool-Loops.
        base_url: OpenAI-Basis-URL (fuer Tests/Proxys uebersteuerbar).
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        client,
        channel: Channel,
        system_prompt: str,
        http_client: httpx.AsyncClient | None = None,
        max_tool_iters: int = 5,
        base_url: str = OPENAI_BASE_URL,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._client = client
        self._channel = channel
        self._system_prompt = system_prompt
        self._http = http_client
        self._owns_http = http_client is None
        self._max_tool_iters = max_tool_iters
        self._base_url = base_url.rstrip("/")

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=60.0)
            self._owns_http = True
        return self._http

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None and not self._http.is_closed:
            await self._http.aclose()

    async def _chat_completion(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        http = await self._get_http()
        resp = await http.post(
            f"{self._base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "model": self._model,
                "messages": messages,
                "tools": jtools.openai_tool_schemas(self._channel),
                "tool_choice": "auto",
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def respond(
        self, user_text: str, history: list[dict[str, str]] | None = None
    ) -> BrainResult:
        """Beantwortet eine User-Nachricht, fuehrt noetige Tool-Calls aus.

        ``history`` ist eine Liste von ``{"role", "content"}``-Turns (user/assistant)
        aus vorherigen Runden. Tool-Zwischenschritte werden NICHT in die History
        aufgenommen — nur die sichtbaren user/assistant-Turns.
        """
        messages: list[dict[str, Any]] = [{"role": "system", "content": self._system_prompt}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": user_text})

        actions: list[dict[str, Any]] = []

        for _ in range(self._max_tool_iters):
            data = await self._chat_completion(messages)
            choices = data.get("choices") or []
            if not choices:
                logger.warning("OpenAI returned no choices: %s", data)
                break
            message = choices[0].get("message") or {}
            tool_calls = message.get("tool_calls") or []

            if not tool_calls:
                text = (message.get("content") or "").strip()
                return BrainResult(
                    text=text,
                    actions=actions,
                    new_turns=[
                        {"role": "user", "content": user_text},
                        {"role": "assistant", "content": text},
                    ],
                )

            # Assistant-Message mit den tool_calls muss VOR den tool-Results stehen.
            messages.append(
                {
                    "role": "assistant",
                    "content": message.get("content"),
                    "tool_calls": tool_calls,
                }
            )

            for call in tool_calls:
                fn = call.get("function") or {}
                name = fn.get("name") or ""
                raw_args = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except json.JSONDecodeError:
                    logger.warning("Bad tool arguments for %s: %r", name, raw_args)
                    args = {}
                result = await jtools.dispatch(name, self._client, self._channel, args)
                actions.append({"name": name, "arguments": args, "result": result})
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id"),
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )

        # Tool-Iterationslimit erreicht — letzte Modellantwort ohne weitere Tools holen.
        logger.warning("JarvisBrain hit max_tool_iters=%d", self._max_tool_iters)
        data = await self._chat_completion(messages)
        message = (data.get("choices") or [{}])[0].get("message") or {}
        text = (message.get("content") or "").strip()
        return BrainResult(
            text=text,
            actions=actions,
            new_turns=[
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": text},
            ],
        )


async def transcribe_audio(
    audio_bytes: bytes,
    *,
    filename: str,
    api_key: str,
    model: str,
    http_client: httpx.AsyncClient | None = None,
    base_url: str = OPENAI_BASE_URL,
) -> str:
    """Transkribiert Audio ueber die OpenAI-Transcription-API.

    Telegram-Sprachnotizen sind ogg/opus — die OpenAI-Transcription-API
    akzeptiert ogg direkt, also keine ffmpeg-Konvertierung noetig.

    Wirft bei HTTP-Fehlern (der Aufrufer faengt + meldet dem Operator).
    """
    own = http_client is None
    http = http_client or httpx.AsyncClient(timeout=60.0)
    try:
        resp = await http.post(
            f"{base_url.rstrip('/')}/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            data={"model": model},
            files={"file": (filename, audio_bytes, "audio/ogg")},
        )
        resp.raise_for_status()
        return (resp.json().get("text") or "").strip()
    finally:
        if own:
            await http.aclose()
