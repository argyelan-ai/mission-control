"""Frontier-Delegation fuer Jarvis (ADR-062).

Jarvis ist ein Concierge, kein Denker — die Realtime-/Text-Modelle sind auf
schnelle Konversation optimiert, nicht auf schwere Analyse, Planung oder
Wissensfragen. Fuer solche Fragen delegiert Jarvis ueber ``ask_frontier`` an ein
starkes OpenAI-Textmodell und gibt dessen Antwort in eigenen Worten wieder.

Bewusst ueber ``httpx`` direkt gegen die OpenAI-Chat-Completions-API — exakt wie
``jarvis_core.brain`` (ADR-061). Kein neues SDK, keine Lock-Regeneration; in Tests
wird ein gemockter HTTP-Client injiziert, es gibt nie echte API-Calls.

Modellwahl (Env ``JARVIS_FRONTIER_MODEL``, sonst ``DEFAULT_FRONTIER_MODEL``):
Am 10.07.2026 lieferte ``GET /v1/models`` mit dem Operator-Key u.a. diese
Text-/Reasoning-Modelle (Auszug, absteigend nach Faehigkeit):

    gpt-5.6-luna / gpt-5.6-sol / gpt-5.6-terra   (5.6-Codenamen, mehrdeutig)
    gpt-5.5-pro / gpt-5.5                         (5.5-Flaggschiff)
    gpt-5.4-pro / gpt-5.4 · gpt-5.2-pro · gpt-5-pro · gpt-5
    gpt-4.1 · gpt-4o · o3 · o1 · o4-mini          (aeltere Generationen)

Default = ``gpt-5.5``: das neueste klar benannte, allgemein verfuegbare
Flaggschiff. Bewusst NICHT die ``-pro``-Stufe (sehr hohe Reasoning-Latenz, sprengt
gerne das 120s-Budget einer Concierge-Antwort) und NICHT die 5.6-Codenamen
(luna/sol/terra sind unklar spezialisiert, kein dokumentiertes General-Flaggschiff).
Der Operator kann per ``JARVIS_FRONTIER_MODEL`` jederzeit uebersteuern.

Fallback-Kette: konfiguriertes/Default-Modell → bei Aufruf-Fehler einmalig
``FALLBACK_FRONTIER_MODEL`` (gpt-4o, existiert praktisch immer) → sonst ehrlicher
Fehler an den Aufrufer.
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger("jarvis_core.frontier")

OPENAI_BASE_URL = "https://api.openai.com/v1"

# Siehe Modul-Docstring: /v1/models-Befund am 10.07.2026.
DEFAULT_FRONTIER_MODEL = "gpt-5.5"
# Letzte Ausweichstufe, wenn das konfigurierte Modell einen Fehler wirft
# (z.B. Modellname zurueckgezogen, Kontingent, transienter 5xx).
FALLBACK_FRONTIER_MODEL = "gpt-4o"

# Grosszuegig: schwere Analyse-/Planungs-Antworten duerfen dauern. Der
# Concierge sagt vorher an ("einen Moment, ich denke kurz nach").
DEFAULT_TIMEOUT_SECONDS = 120.0


def resolve_model(explicit: str | None = None) -> str:
    """Bestimmt das Frontier-Modell: expliziter Arg → Env → Default."""
    return explicit or os.environ.get("JARVIS_FRONTIER_MODEL") or DEFAULT_FRONTIER_MODEL


async def complete_text(
    *,
    system: str,
    user: str,
    api_key: str | None = None,
    model: str | None = None,
    http_client: httpx.AsyncClient | None = None,
    base_url: str = OPENAI_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    fallback_model: str | None = FALLBACK_FRONTIER_MODEL,
) -> str:
    """Ein einzelner Chat-Completions-Aufruf ohne Tools → reiner Text.

    Geteilt zwischen dem ``ask_frontier``-Tool (Jarvis) und dem Morning-Briefing-Job
    (ADR-062). Wirft bei endgueltigem Fehlschlag (auch der Fallback scheitert) —
    der Aufrufer faengt und meldet ehrlich.

    Die Fallback-Kette greift nur bei einem echten Aufruf-Fehler des primaeren
    Modells (HTTP-Fehler/Timeout), nicht bei einer leeren-aber-erfolgreichen
    Antwort.
    """
    key = api_key or os.environ.get("OPENAI_API_KEY") or ""
    if not key:
        raise RuntimeError("OPENAI_API_KEY fehlt — Frontier-Delegation nicht moeglich.")

    primary = resolve_model(model)
    own_http = http_client is None
    http = http_client or httpx.AsyncClient(timeout=timeout)

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    async def _call(model_name: str) -> str:
        resp = await http.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model_name, "messages": messages},
        )
        resp.raise_for_status()
        data = resp.json()
        return ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""

    try:
        try:
            text = await _call(primary)
        except Exception as e:  # noqa: BLE001 — primary failed, try fallback once
            if fallback_model and fallback_model != primary:
                logger.warning(
                    "Frontier model %r failed (%s) — falling back to %r",
                    primary, e, fallback_model,
                )
                text = await _call(fallback_model)
            else:
                raise
        return text.strip()
    finally:
        if own_http:
            await http.aclose()


async def ask_frontier(
    question: str,
    context_hint: str | None = None,
    *,
    api_key: str | None = None,
    model: str | None = None,
    http_client: httpx.AsyncClient | None = None,
    base_url: str = OPENAI_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    """Delegiert eine schwere Frage an das Frontier-Modell → strukturierte Antwort.

    Returnt ``{"ok": True, "answer": "...", "model": "..."}`` oder bei Fehler
    ``{"ok": False, "error": "..."}`` — Jarvis narrativiert beides (die Antwort in
    eigenen Worten, den Fehler als ehrliche kurze Meldung).
    """
    system = (
        "Du bist ein praeziser, analytischer Assistent, der einem Sprach-Concierge "
        "namens Jarvis zuarbeitet. Beantworte die Frage gruendlich, aber kompakt und "
        "strukturiert. Jarvis liest deine Antwort dem Operator vor bzw. gibt sie in "
        "eigenen Worten wieder — schreibe daher klar, ohne Fuellwoerter, ohne "
        "Meta-Kommentar ueber dich selbst. Deutsch, es sei denn die Frage ist englisch."
    )
    user = question if not context_hint else f"Kontext: {context_hint}\n\nFrage: {question}"
    try:
        answer = await complete_text(
            system=system,
            user=user,
            api_key=api_key,
            model=model,
            http_client=http_client,
            base_url=base_url,
            timeout=timeout,
        )
        if not answer:
            return {"ok": False, "error": "Das Frontier-Modell kam ohne Antwort zurueck."}
        return {"ok": True, "answer": answer, "model": resolve_model(model)}
    except Exception as e:  # noqa: BLE001 — surface as data, Jarvis narrates
        logger.exception("ask_frontier failed")
        return {"ok": False, "error": str(e)}
