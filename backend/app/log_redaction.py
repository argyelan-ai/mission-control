"""Redact secrets from log records before they reach any handler.

Live-Befund 26.07.2026: die Backend-Logs enthielten in 6 Stunden 2723 Zeilen
mit dem vollen Telegram-Bot-Token, weil `httpx` jede ausgehende Request-URL
auf INFO loggt (`api.telegram.org/bot<TOKEN>/getUpdates`). Dazu JWTs aus
`?token=…`-Query-Params, die `uvicorn.access` mitschreibt — SSE/EventSource
und WebSocket koennen keine Authorization-Header setzen, der Token MUSS also
in die URL.

Beide Logger sind fachlich wertvoll (Request-Tracing) und bleiben an; nur die
Geheimnisse werden ersetzt. Der Filter haengt an Root UND an jedem Logger mit
eigenem Handler bzw. abgeschalteter Propagation (siehe
``install_log_redaction``) — Root allein reicht nachweislich nicht: uvicorn
gibt ``uvicorn.access`` einen eigenen Handler mit ``propagate=False``, dessen
Records die Root-Handler nie erreichen.

Wichtig: Der Filter arbeitet auf der GERENDERTEN Message (`getMessage()`),
nicht auf `record.msg` — uvicorn formatiert seine Access-Zeilen ueber
%-Args, deren Secrets sonst am Filter vorbeilaufen.
"""
from __future__ import annotations

import logging
import re

REDACTED = "<REDACTED>"

# Billiger Vorab-Check: nur Zeilen mit einem dieser Marker werden ueberhaupt
# durch die Regex-Kette geschickt. Access-Logs sind hochfrequent — die
# ueberwaeltigende Mehrheit trifft keinen Marker und kostet dann nur einen
# Substring-Scan.
_MARKERS = ("/bot", "token", "Bearer", "key=", "apikey", "secret")

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Telegram-Bot-Token im Pfad: /bot<id>:<secret>/method — ID mitredigieren,
    # sie identifiziert den Bot eindeutig.
    (re.compile(r"/bot\d+:[A-Za-z0-9_-]+"), f"/bot{REDACTED}"),
    # Credential-Query-Parameter. Wert endet am naechsten &, Whitespace oder
    # Anfuehrungszeichen — andere Parameter bleiben lesbar.
    (
        re.compile(
            r"\b(token|access_token|refresh_token|api_key|apikey|key|secret|password)"
            r"=[^&\s\"'<]+",
            re.IGNORECASE,
        ),
        rf"\1={REDACTED}",
    ),
    # Authorization-Header, falls eine Library ihre Headers loggt.
    (re.compile(r"\bBearer\s+[A-Za-z0-9._\-]+"), f"Bearer {REDACTED}"),
)


def redact_secrets(text: str) -> str:
    """Ersetze bekannte Secret-Muster in `text`. Idempotent: der Platzhalter
    enthaelt `<`/`>` und wird von keinem Muster erneut erfasst."""
    if not any(marker in text for marker in _MARKERS):
        return text
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _redact_arg(value):
    """Ein einzelnes Log-Argument saeubern — typ-erhaltend, wo es geht.

    Strings direkt. Nicht-Strings ueber ihre Textform, denn genau da versteckt
    sich der haeufigste Fall: httpx loggt ``request.url`` als ``httpx.URL``-
    OBJEKT, nicht als str (`'HTTP Request: %s %s ...', method, request.url`).
    Ein reiner ``isinstance(str)``-Test liess den Telegram-Token dort ungefiltert
    durch (Live-Befund 26.07.2026, dritter Anlauf).

    Objekte ohne Secret werden UNVERAENDERT durchgereicht — Zahlen bleiben
    Zahlen (``%d``!), fremde Typen behalten ihre Identitaet.
    """
    if isinstance(value, str):
        return redact_secrets(value)
    try:
        text = str(value)
    except Exception:  # noqa: BLE001 — kaputtes __str__ nie eskalieren lassen
        return value
    cleaned = redact_secrets(text)
    return cleaned if cleaned != text else value


class SecretRedactingFilter(logging.Filter):
    """Logging-Filter, der Secrets aus der Message entfernt.

    Gibt IMMER True zurueck — ein Filter, der False liefert, verschluckt die
    Log-Zeile komplett; wir wollen die Zeile behalten, nur ohne Geheimnis.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            original = record.getMessage()
        except Exception:  # noqa: BLE001 — kaputte %-Args nie zum Crash fuehren lassen
            return True

        if redact_secrets(original) == original:
            return True  # nichts zu tun — haeufigster Fall, kein Anfassen

        # An Ort und Stelle redigieren: msg UND jedes Arg einzeln. Die
        # %-Struktur muss erhalten bleiben — uvicorns AccessFormatter entpackt
        # record.args in genau fuenf Felder, ein geleertes Tupel liesse ihn
        # werfen ("--- Logging error ---" statt Access-Zeile, Live-Befund
        # 26.07.2026). Nicht-Strings bleiben unangetastet, sonst bricht %d.
        if isinstance(record.msg, str):
            record.msg = redact_secrets(record.msg)

        if isinstance(record.args, dict):
            record.args = {k: _redact_arg(v) for k, v in record.args.items()}
        elif isinstance(record.args, tuple):
            record.args = tuple(_redact_arg(a) for a in record.args)
        return True


def _attach(target) -> None:
    """Filter an einen Logger ODER Handler haengen (idempotent)."""
    if not any(isinstance(f, SecretRedactingFilter) for f in target.filters):
        target.addFilter(SecretRedactingFilter())


def install_log_redaction() -> None:
    """Haenge den Filter flotten-weit an (idempotent, mehrfach aufrufbar).

    Zwei Ebenen, weil eine allein nachweislich nicht reicht (Live-Befund
    26.07.2026, direkt nach dem ersten Deploy: httpx-Zeilen waren sauber,
    uvicorn-Access-Zeilen leakten weiter den JWT):

    * **Handler-Filter** fangen alles, was bei einem Handler ankommt — auch
      Records, die von Child-Loggern hochpropagiert wurden (httpx & Co.).
    * **Logger-Filter** braucht es fuer Logger mit EIGENEM Handler und
      ``propagate=False`` — genau so konfiguriert uvicorn ``uvicorn.access``.
      Deren Records erreichen die Root-Handler nie. Ein Filter am Logger
      selbst greift dagegen fuer jeden Record, der dort entsteht.

    Der Aufruf ist idempotent und soll BEIDE Male laufen: einmal beim Import
    (fuer alles, was bereits konfiguriert ist) und einmal im lifespan-Startup
    (fuer Logger, die uvicorn erst beim Server-Start anlegt).
    """
    root = logging.getLogger()
    _attach(root)
    for handler in root.handlers:
        _attach(handler)

    # Alle bereits existierenden Logger — Snapshot ueber list(), weil das
    # Anlegen eines Loggers waehrend der Iteration das Dict veraendern kann.
    for name in list(logging.Logger.manager.loggerDict):
        logger = logging.getLogger(name)
        if not isinstance(logger, logging.Logger):  # PlaceHolder-Eintraege
            continue
        # Nur wo es noetig ist: eigener Handler (Records enden dort) oder
        # abgeschaltete Propagation (Records erreichen Root nie).
        if logger.handlers or not logger.propagate:
            _attach(logger)
            for handler in logger.handlers:
                _attach(handler)
