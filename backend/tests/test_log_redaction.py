"""Tests for secret redaction in log records.

Live-Befund 26.07.2026: `httpx` loggt jede ausgehende Request-URL auf INFO —
inklusive des Telegram-Bot-Tokens im Pfad (`api.telegram.org/bot<TOKEN>/…`),
2723 Zeilen in 6 Stunden. `uvicorn.access` loggt eingehende Request-Zeilen
inklusive `?token=<JWT>` (SSE/EventSource + WebSocket können keine
Authorization-Header setzen, der Token MUSS in die Query).

Beide Logger sind fachlich nützlich und sollen an bleiben — nur die Secrets
gehören raus. Der Filter arbeitet auf der GERENDERTEN Message, damit er auch
uvicorn's %-Args-Formatierung erwischt.
"""
import logging

import pytest

from app.log_redaction import SecretRedactingFilter, install_log_redaction


@pytest.fixture
def redact():
    f = SecretRedactingFilter()

    def _run(msg, *args, name="test"):
        record = logging.LogRecord(
            name=name, level=logging.INFO, pathname=__file__, lineno=1,
            msg=msg, args=args or None, exc_info=None,
        )
        assert f.filter(record) is True  # never drops records
        return record.getMessage()

    return _run


# ── Telegram bot tokens (httpx) ───────────────────────────────────────────

def test_redacts_telegram_bot_token_in_url(redact):
    out = redact(
        'HTTP Request: GET https://api.telegram.org/bot1234567890:FAKEfakeFAKE'
        'fakeFAKEfakeFAKEfake123/getUpdates?offset=124594650 "HTTP/1.1 200 OK"'
    )
    assert "FAKEfakeFAKE" not in out
    assert "1234567890" not in out
    # Kontext bleibt lesbar: Host, Methode, Endpunkt, Status
    assert "api.telegram.org" in out
    assert "getUpdates" in out
    assert "200 OK" in out


def test_redacts_telegram_token_passed_via_args(redact):
    """uvicorn/httpx formatieren oft per %-args — der Filter muss die
    gerenderte Message prüfen, nicht nur record.msg."""
    out = redact("HTTP Request: %s %s", "POST",
                 "https://api.telegram.org/bot123456:SECRETVALUE123/sendMessage")
    assert "SECRETVALUE123" not in out
    assert "sendMessage" in out


# ── JWT / token query params (uvicorn.access) ─────────────────────────────

def test_redacts_token_query_param(redact):
    out = redact(
        '172.18.0.26:55584 - "WebSocket /api/v1/vault/voice-display'
        '?token=HEADERPART.PAYLOADPART.SIGPART" [accepted]'
    )
    assert "HEADERPART.PAYLOADPART.SIGPART" not in out
    assert "voice-display" in out
    assert "[accepted]" in out


def test_redacts_token_param_mid_query_keeping_other_params(redact):
    out = redact('GET /api/v1/stream?token=JWTHEAD.abc.def&since=42 HTTP/1.1" 200 OK')
    assert "JWTHEAD.abc.def" not in out
    assert "since=42" in out  # harmlose Parameter bleiben erhalten
    assert "200 OK" in out


@pytest.mark.parametrize("param", ["access_token", "api_key", "apikey", "secret"])
def test_redacts_other_credential_query_params(redact, param):
    out = redact(f'GET /x?{param}=SUPERSECRETVALUE HTTP/1.1" 200 OK')
    assert "SUPERSECRETVALUE" not in out
    assert "200 OK" in out


def test_redacts_bearer_header_value(redact):
    out = redact("Retrying with headers {'Authorization': 'Bearer abc123def456ghi'}")
    assert "abc123def456ghi" not in out


# ── Darf NICHTS kaputt machen ─────────────────────────────────────────────

def test_leaves_ordinary_messages_untouched(redact):
    msg = 'INFO 172.18.0.7 - "GET /api/v1/agent/me/poll HTTP/1.1" 200 OK'
    assert redact(msg) == msg


def test_leaves_uuids_and_task_ids_untouched(redact):
    msg = "dispatched task bca6d5bd-4d1e-4575-90ea-75b7422702cb (Messlauf)"
    assert redact(msg) == msg


def test_does_not_redact_word_token_without_value(redact):
    msg = "agent token refreshed successfully"
    assert redact(msg) == msg


def test_filter_never_drops_records(redact):
    """Ein Filter, der False liefert, verschluckt die Zeile — hier nie."""
    assert redact("anything") == "anything"


def test_idempotent_across_multiple_handlers(redact):
    """Mehrere Handler filtern denselben Record nacheinander — die zweite
    Runde darf die bereits redigierte Message nicht weiter zerlegen."""
    f = SecretRedactingFilter()
    record = logging.LogRecord(
        name="httpx", level=logging.INFO, pathname=__file__, lineno=1,
        msg="GET https://api.telegram.org/bot99:TOKENVALUE/getMe", args=None,
        exc_info=None,
    )
    f.filter(record)
    first = record.getMessage()
    f.filter(record)
    assert record.getMessage() == first
    assert "TOKENVALUE" not in first


# ── Installation ──────────────────────────────────────────────────────────

def test_install_attaches_filter_to_root_handlers():
    root = logging.getLogger()
    had = [h for h in root.handlers]
    if not had:  # pytest kann ohne Handler laufen
        root.addHandler(logging.NullHandler())
    try:
        install_log_redaction()
        for handler in logging.getLogger().handlers:
            assert any(isinstance(f, SecretRedactingFilter) for f in handler.filters), (
                f"handler {handler!r} hat keinen Redaction-Filter"
            )
    finally:
        for handler in logging.getLogger().handlers:
            for filt in list(handler.filters):
                if isinstance(filt, SecretRedactingFilter):
                    handler.removeFilter(filt)


def test_install_is_idempotent():
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(logging.NullHandler())
    try:
        install_log_redaction()
        install_log_redaction()
        for handler in root.handlers:
            filters = [f for f in handler.filters if isinstance(f, SecretRedactingFilter)]
            assert len(filters) <= 1, "Filter wurde doppelt angehängt"
    finally:
        for handler in root.handlers:
            for filt in list(handler.filters):
                if isinstance(filt, SecretRedactingFilter):
                    handler.removeFilter(filt)


# ── Logger mit EIGENEN Handlern (uvicorn.access-Klasse) ───────────────────
#
# Live-Befund 26.07. NACH dem ersten Deploy: httpx-Zeilen waren redigiert,
# uvicorn-Access-Zeilen NICHT. Grund: uvicorn konfiguriert `uvicorn.access`
# mit einem EIGENEN Handler und `propagate=False` — der Record erreicht die
# Root-Handler nie, an denen der Filter hing. Ein Filter am Logger selbst
# greift dagegen fuer jeden Record, der an diesem Logger entsteht.

def _isolated_logger(name):
    lg = logging.getLogger(name)
    lg.handlers.clear()
    lg.filters.clear()
    lg.propagate = False  # wie uvicorn.access
    lg.setLevel(logging.INFO)  # sonst schluckt der geerbte Level die Zeile
    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    lg.addHandler(_Capture())
    return lg, records


def test_redacts_on_logger_with_own_handler_and_no_propagate():
    """Der uvicorn.access-Fall: eigener Handler + propagate=False."""
    lg, records = _isolated_logger("uvicorn.access")
    try:
        install_log_redaction()
        lg.info('1.2.3.4 - "GET /api/v1/tasks/stream?token=SECRETJWTVALUE HTTP/1.1" 200')
        assert records, "kein Record aufgezeichnet"
        assert "SECRETJWTVALUE" not in records[-1]
        assert "tasks/stream" in records[-1]
        assert "200" in records[-1]
    finally:
        lg.handlers.clear()
        lg.filters.clear()
        lg.propagate = True


def test_redacts_uvicorn_access_with_percent_args():
    """uvicorn formatiert Access-Zeilen ueber %-Args."""
    lg, records = _isolated_logger("uvicorn.access")
    try:
        install_log_redaction()
        lg.info('%s - "%s %s HTTP/1.1" %d', "1.2.3.4", "GET",
                "/api/v1/vault/voice-display?token=ANOTHERSECRET", 200)
        assert records and "ANOTHERSECRET" not in records[-1]
        assert "voice-display" in records[-1]
    finally:
        lg.handlers.clear()
        lg.filters.clear()
        lg.propagate = True


def test_install_covers_late_configured_loggers():
    """uvicorn kann seine Logger NACH dem App-Import konfigurieren — ein
    erneuter install_log_redaction()-Aufruf (z.B. im lifespan-Startup) muss
    die dann neu entstandenen Handler ebenfalls erfassen."""
    install_log_redaction()
    lg, records = _isolated_logger("some.late.logger")
    try:
        install_log_redaction()  # zweiter Durchlauf nach der Handler-Anlage
        lg.info("GET /x?api_key=LATESECRET")
        assert records and "LATESECRET" not in records[-1]
    finally:
        lg.handlers.clear()
        lg.filters.clear()
        lg.propagate = True


def test_no_duplicate_filters_on_repeat_install():
    lg, _ = _isolated_logger("dupe.check")
    try:
        install_log_redaction()
        install_log_redaction()
        install_log_redaction()
        own = [f for f in lg.filters if isinstance(f, SecretRedactingFilter)]
        assert len(own) <= 1
        for h in lg.handlers:
            assert len([f for f in h.filters if isinstance(f, SecretRedactingFilter)]) <= 1
    finally:
        lg.handlers.clear()
        lg.filters.clear()
        lg.propagate = True
