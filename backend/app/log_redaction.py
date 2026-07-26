"""Redact secrets from log records before they reach any handler.

Live-Befund 26.07.2026: die Backend-Logs enthielten in 6 Stunden 2723 Zeilen
mit dem vollen Telegram-Bot-Token, weil `httpx` jede ausgehende Request-URL
auf INFO loggt (`api.telegram.org/bot<TOKEN>/getUpdates`). Dazu JWTs aus
`?token=…`-Query-Params, die `uvicorn.access` mitschreibt — SSE/EventSource
und WebSocket koennen keine Authorization-Header setzen, der Token MUSS also
in die URL.

Beide Logger sind fachlich wertvoll (Request-Tracing) und bleiben an; nur die
Geheimnisse werden ersetzt. Der Filter haengt an den ROOT-Handlern, damit er
unabhaengig vom Logger-Namen greift — auch fuer Libraries, die wir nicht
kennen.

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

        cleaned = redact_secrets(original)
        if cleaned != original:
            # Message ist bereits gerendert -> Args entfernen, sonst wuerde
            # ein zweiter getMessage()-Aufruf erneut zu formatieren versuchen.
            record.msg = cleaned
            record.args = ()
        return True


def install_log_redaction() -> None:
    """Haenge den Filter an alle Root-Handler (idempotent).

    Handler-Ebene statt Logger-Ebene: Filter auf einem Logger greifen NICHT
    fuer Records, die von Child-Loggern hochpropagiert werden — Handler-Filter
    sehen dagegen jeden Record, der tatsaechlich ausgegeben wird.
    """
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, SecretRedactingFilter) for f in handler.filters):
            handler.addFilter(SecretRedactingFilter())
