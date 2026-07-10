"""Frontier-Delegation fuer Jarvis (ADR-062).

Jarvis ist ein Concierge, kein Denker — die Realtime-/Text-Modelle sind auf
schnelle Konversation optimiert, nicht auf schwere Analyse, Planung oder
Wissensfragen. Fuer solche Fragen delegiert Jarvis ueber ``ask_frontier`` an ein
starkes OpenAI-Textmodell und gibt dessen Antwort in eigenen Worten wieder.

Bewusst ueber ``httpx`` direkt gegen die OpenAI-Chat-Completions-API — exakt wie
``jarvis_core.brain`` (ADR-061). Kein neues SDK, keine Lock-Regeneration; in Tests
wird ein gemockter HTTP-Client injiziert, es gibt nie echte API-Calls.

Modellwahl (Env ``JARVIS_FRONTIER_MODEL``, sonst ``DEFAULT_FRONTIER_MODEL``):
Die folgende Liste ist ein **Snapshot vom 10.07.2026, kein Vertrag** — das
Modell-Angebot aendert sich, ``JARVIS_FRONTIER_MODEL`` uebersteuert jederzeit.
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

import asyncio
import logging
import os

import httpx

logger = logging.getLogger("jarvis_core.frontier")

OPENAI_BASE_URL = "https://api.openai.com/v1"

# Harter Gesamtdeckel fuer den ask_frontier-TOOL-Aufruf (inkl. Fallback). Der
# Concierge darf nicht minutenlang haengen; laeuft es laenger, ehrlich abbrechen.
ASK_FRONTIER_HARD_CAP_SECONDS = 90.0
# Token-Deckel (Kosten/Latenz). ``max_completion_tokens`` ist der aktuelle Param
# (Reasoning-Modelle wie gpt-5.x lehnen ``max_tokens`` ab; gpt-4o akzeptiert beide).
ASK_FRONTIER_MAX_TOKENS = 800
BRIEFING_MAX_TOKENS = 500

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


def _should_fallback(exc: Exception) -> bool:
    """Fallback nur bei transienten/behebbaren Fehlern des primaeren Modells.

    JA: Timeout, 5xx (Server), 404/400 mit Modell-Bezug (Modell zurueckgezogen).
    NEIN: 401/403 (Auth) und andere 4xx (Bad Request) — ein anderes Modell heilt
    das nicht, also ehrlich durchreichen statt Kosten fuer einen zweiten Fehlversuch.
    """
    if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status >= 500:
            return True
        if status == 404:
            return True
        if status == 400:
            body = ""
            try:
                body = exc.response.text.lower()
            except Exception:  # noqa: BLE001
                body = ""
            return "model" in body
        return False  # 401/403/429/other 4xx → no fallback
    # Unknown/transport error → allow one fallback attempt.
    return isinstance(exc, httpx.HTTPError)


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
    max_tokens: int | None = None,
) -> str:
    """Ein einzelner Chat-Completions-Aufruf ohne Tools → reiner Text.

    Geteilt zwischen dem ``ask_frontier``-Tool (Jarvis) und dem Morning-Briefing-Job
    (ADR-062). Wirft bei endgueltigem Fehlschlag (auch der Fallback scheitert) —
    der Aufrufer faengt und meldet ehrlich.

    Die Fallback-Kette greift NUR bei transienten/behebbaren Fehlern des primaeren
    Modells (Timeout, 5xx, Modell-not-found — siehe ``_should_fallback``), nicht bei
    401/403/Bad-Request und nicht bei einer leeren-aber-erfolgreichen Antwort.
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
        body: dict = {"model": model_name, "messages": messages}
        if max_tokens is not None:
            # max_completion_tokens: aktueller Param, den auch Reasoning-Modelle
            # (gpt-5.x) akzeptieren; max_tokens wuerde dort einen 400 werfen.
            body["max_completion_tokens"] = max_tokens
        resp = await http.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        return ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""

    try:
        try:
            text = await _call(primary)
        except Exception as e:  # noqa: BLE001 — decide whether a fallback can help
            if fallback_model and fallback_model != primary and _should_fallback(e):
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
        # Harter Gesamtdeckel ueber den ganzen Tool-Call (inkl. Fallback): der
        # Concierge darf nicht minutenlang stumm haengen.
        answer = await asyncio.wait_for(
            complete_text(
                system=system,
                user=user,
                api_key=api_key,
                model=model,
                http_client=http_client,
                base_url=base_url,
                timeout=timeout,
                max_tokens=ASK_FRONTIER_MAX_TOKENS,
            ),
            timeout=ASK_FRONTIER_HARD_CAP_SECONDS,
        )
        if not answer:
            return {"ok": False, "error": "Das Frontier-Modell kam ohne Antwort zurueck."}
        return {"ok": True, "answer": answer, "model": resolve_model(model)}
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning("ask_frontier hit hard cap %.0fs", ASK_FRONTIER_HARD_CAP_SECONDS)
        return {
            "ok": False,
            "reason": "timeout",
            "message": "Die Analyse dauert zu lange, ich breche ab.",
            "error": f"ask_frontier exceeded {ASK_FRONTIER_HARD_CAP_SECONDS:.0f}s",
        }
    except Exception as e:  # noqa: BLE001 — surface as data, Jarvis narrates
        logger.exception("ask_frontier failed")
        return {"ok": False, "error": str(e)}
