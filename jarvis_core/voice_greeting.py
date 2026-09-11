"""Jarvis' Begruessung, GPT-Live-Stimmen-Validierung, Latenz-Tracking — pure,
kein ``livekit``-Import (ADR-083 Review-Fund, 10.09.2026).

Warum ein eigenes Modul statt in ``voice_worker/main.py``: dieses Modul
importiert ``livekit`` auf Modulebene, also skippte JEDER Test, der
``_import_main()`` braucht, still in CI (kein ``livekit`` im Backend-venv) —
genau das Muster aus der Lehre vom 21.08.2026, das ``jarvis_core/
voice_provider.py`` schon vermeidet. Eine Sabotage-Probe im Review bestaetigte
es konkret: die Begruessungs-Zahlen-Regression und eine kaputte
Stimmen-Validierung wurden von CI NICHT gefangen, weil beide Tests
``_import_main()`` brauchten und lautlos uebersprungen wurden.

``voice_worker/main.py`` importiert die Funktionen hier nur noch und
verdrahtet sie mit livekit-spezifischen Dingen (``session.on(...)``,
``AgentSession``) — die eigentliche Logik ist hier, livekit-frei, und laeuft
im gewoehnlichen Backend-Testjob.
"""
from __future__ import annotations

import logging
import random

logger = logging.getLogger("jarvis_core.voice_greeting")


# ── GPT-Live-Stimmen-Validierung (ADR-083) ──────────────────────────────
#
# Aus dem PR-Code selbst (nicht aus Doku-Vermutungen) —
# `GPTLiveVoices = Literal["aster", "beacon", "cinder", "marin", "stone",
# "vesper"]` und `DEFAULT_VOICE = "marin"` in gpt_live_model.py (PR #7212,
# SHA de3c5ce, Stand 10.09.2026). "marin" (auch OpenAI-Realtime-Default) ist
# also tatsaechlich GUELTIG fuer gpt-live-1 — keine Fehlkonfiguration.
GPT_LIVE_KNOWN_VOICES = frozenset({"aster", "beacon", "cinder", "marin", "stone", "vesper"})
GPT_LIVE_DEFAULT_VOICE = "marin"


def validate_live_voice(voice: str | None) -> str:
    """Validiert eine (aus ``VoiceChoice.voice``) bereits aufgeloeste Stimme
    gegen ``GPT_LIVE_KNOWN_VOICES`` — laute Warnung + Fallback auf
    ``GPT_LIVE_DEFAULT_VOICE`` bei Unbekanntem, statt eine vermutlich falsche
    Stimme (z.B. ein xAI-Realtime-Name wie "ara", der fuer diese API nicht
    gilt) stillschweigend an die API durchzureichen.
    """
    raw = (voice or "").strip()
    if not raw:
        return GPT_LIVE_DEFAULT_VOICE
    if raw.lower() not in GPT_LIVE_KNOWN_VOICES:
        logger.warning(
            "voice %r is not a known gpt-live-1 voice (known: %s) — falling "
            "back to default %r. If OpenAI added a new voice name, add it to "
            "GPT_LIVE_KNOWN_VOICES in jarvis_core/voice_greeting.py.",
            raw, sorted(GPT_LIVE_KNOWN_VOICES), GPT_LIVE_DEFAULT_VOICE,
        )
        return GPT_LIVE_DEFAULT_VOICE
    return raw.lower()


# ── Greeting — situational opening (ADR-083 Nachschliff, Marks Feedback) ──
#
# Vorherige Version: JEDE Begruessung rechnete Tasks/Approvals-Zahlen in den
# ersten Satz ("10 Tasks im Board, Mark. Womit fangen wir an?") — genau das
# war der Fund aus Marks erstem GPT-Live-Anruf: "das ist nicht natuerlich,
# er berichtet direkt beim Einstieg". Ersetzt durch ein SITUATIVES Oeffnen
# (Variante b aus der Review-Diskussion):
#
#   - Gruss + Vokativ, ECHT tageszeit-abhaengig (Review-Fund: eine fruehere
#     Version behauptete das nur im Kommentar, "Abend" konnte per
#     random.choice auch morgens fallen), KEINE Zahlen.
#   - NUR wenn es einen echten Anlass gibt (Approval wartet, oder ein Task
#     haengt in "blocked" fest) — EIN kurzer, natuerlicher Zusatz-Satz.
#     Sonst bleibt es bei Gruss + offener Frage, wie bei einem Kollegen.
#
# (Zwei verworfene Alternativen, siehe PR/ADR: (a) Jarvis erwaehnt das
# Briefing GAR NICHT beim Einstieg, nur auf Nachfrage — verworfen, weil ein
# echtes Approval/blocked-Task dann untergeht, bis Mark zufaellig danach
# fragt; (c) Jarvis wartet 1-2s und laesst Mark zuerst reden — verworfen,
# ein GPT-Live-Call OHNE jede erste Aeusserung wirkt wie eine tote Leitung.)
_GREETINGS_NEUTRAL = [
    "Hey{vok}. Was liegt an?",
    "Servus{vok}, was machst du?",
    "Hi{vok} — was steht an?",
    "Bereit{vok}. Sag an.",
    "Hallo{vok}. Was brauchst du?",
]
# Zusaetzliche, ECHT tageszeit-passende Varianten — nur in den Auswahl-Pool
# gemischt, wenn ``briefing["current_time_of_day_de"]`` (backend/app/routers/
# vault.py::_time_of_day_de, Buckets "morgens"/"mittags"/"nachmittags"/
# "abends"/"nachts") tatsaechlich passt. "mittags"/"nachmittags" haben keine
# eigenen Worte — die neutralen Gruesse passen dort ohnehin.
_GREETINGS_BY_TIME_OF_DAY: dict[str, list[str]] = {
    "morgens": ["Morgen{vok}. Was liegt an?", "Guten Morgen{vok}, was steht an?"],
    "abends": ["Abend{vok}. Was treibst du?", "Guten Abend{vok}, was liegt an?"],
    "nachts": ["Hey{vok}, spaet noch unterwegs? Was brauchst du?"],
}
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


def urgent_note(briefing: dict) -> str | None:
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


def build_greeting(briefing: dict | None, operator_name: str | None = None) -> str:
    """Situative Begruessung: Gruss + Vokativ, plus EIN Zusatz-Satz nur bei
    echtem Anlass (siehe ``urgent_note()``) — nie ein Zahlen-Status-Report.

    Gruss-Wortwahl ist ECHT tageszeit-abhaengig, wenn das Briefing
    ``current_time_of_day_de`` liefert (siehe ``_GREETINGS_BY_TIME_OF_DAY``);
    sonst bleibt der zeitneutrale Pool.

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

    time_of_day = (briefing.get("current_time_of_day_de") or "").strip().lower()
    pool = list(_GREETINGS_NEUTRAL) + _GREETINGS_BY_TIME_OF_DAY.get(time_of_day, [])
    line = random.choice(pool).format(vok=vok)
    extra = urgent_note(briefing)
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


# ── Delegation-Latenz — pure Berechnung (ADR-083 Nachschliff) ───────────
#
# session.on("conversation_item_added") liefert Events in beliebiger
# Reihenfolge/Haeufigkeit; das eigentliche "letzter User-Turn bis erste
# Assistant-Antwort"-Tracking ist reine Arithmetik und braucht kein livekit.
# voice_worker/main.py's _attach_latency_logging() haelt nur noch den
# Zustand (ein dict) und ruft diese Funktion pro Event.
def track_latency(last_user_at: float | None, role: str | None, ts: float) -> tuple[float | None, float | None]:
    """Gibt ``(neues last_user_at, Latenz-oder-None)`` zurueck.

    - role == "user": merkt sich ts als neuen last_user_at, keine Latenz.
    - role == "assistant" UND es gibt einen offenen last_user_at: Latenz =
      ts - last_user_at, last_user_at wird zurueckgesetzt (naechste Runde).
    - alles andere (unbekannte Rolle, oder assistant ohne vorherigen User-
      Turn): unveraendert durchreichen, keine Latenz.
    """
    if role == "user":
        return ts, None
    if role == "assistant" and last_user_at:
        return None, ts - last_user_at
    return last_user_at, None
