"""Discord-Alarme: Wiederholungssperre + Sammelmeldung fuer Warnungen.

Gemessener Anlass (Live-DB, 7 Tage): 164 Alarm-Events, davon 156 Warnungen.
Dieselbe Meldung kam bis zu 13x wortgleich an ("endpoint unreachable"), und
jede Warnung ging sofort einzeln raus. Mark hat abgeschaltet hingeschaut.

Zwei Regeln, hier festgenagelt:
  1. Wiederholungssperre: dasselbe Thema innerhalb des Zeitfensters geht nur
     EINMAL raus. Zahlen im Titel (Millisekunden, Zaehler) machen kein neues
     Thema — sonst greift die Sperre nie.
  2. Dringlichkeit entscheidet den Weg: error/critical sofort, Warnungen
     gesammelt als eine Nachricht.

Das ActivityEvent selbst bleibt davon unberuehrt — die Historie in der UI
darf keine Luecken bekommen, nur Discord wird leiser.
"""

import json

import pytest

from app.services import discord_notify


@pytest.fixture(autouse=True)
def _redis(fake_redis, monkeypatch):
    """discord_notify holt Redis ueber get_redis() — auf fakeredis zeigen."""
    async def _get():
        return fake_redis
    monkeypatch.setattr(discord_notify, "get_redis", _get)
    return fake_redis


@pytest.fixture
def sent(monkeypatch):
    """Faengt ab, was tatsaechlich an Discord rausginge."""
    calls = []

    async def _fake_send(title, description, severity="warning"):
        calls.append({"title": title, "description": description, "severity": severity})

    monkeypatch.setattr(discord_notify, "_deliver", _fake_send)
    return calls


# ── Dringlichkeit ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_error_geht_sofort_raus(sent):
    result = await discord_notify.notify_event(
        "agent.recreate_failed", "Rex: Container-Neuerstellung fehlgeschlagen", "error",
    )
    assert result == "sent"
    assert len(sent) == 1
    assert sent[0]["severity"] == "error"


@pytest.mark.asyncio
async def test_critical_geht_sofort_raus(sent):
    result = await discord_notify.notify_event(
        "system.component_down", "Datenbank nicht erreichbar", "critical",
    )
    assert result == "sent"
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_warnung_wird_gesammelt_statt_sofort_gesendet(sent):
    result = await discord_notify.notify_event(
        "runtime.unreachable", "qwen-general: nicht erreichbar", "warning",
    )
    assert result == "queued"
    assert sent == [], "Eine einzelne Warnung darf Mark nicht sofort stoeren"


@pytest.mark.asyncio
async def test_info_geht_gar_nicht_nach_discord(sent):
    result = await discord_notify.notify_event(
        "task.created", "Neue Aufgabe angelegt", "info",
    )
    assert result == "skipped"
    assert sent == []


# ── Wiederholungssperre ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gleiche_meldung_zweimal_geht_nur_einmal_raus(sent):
    first = await discord_notify.notify_event(
        "agent.recreate_failed", "Rex: Container-Neuerstellung fehlgeschlagen", "error",
    )
    second = await discord_notify.notify_event(
        "agent.recreate_failed", "Rex: Container-Neuerstellung fehlgeschlagen", "error",
    )
    assert first == "sent"
    assert second == "suppressed"
    assert len(sent) == 1, "Die Wiederholung darf Mark nicht ein zweites Mal erreichen"


@pytest.mark.asyncio
async def test_zahlen_im_titel_machen_kein_neues_thema(sent):
    """'antwortet langsam (342ms)' und '(501ms)' sind dasselbe Problem.

    Ohne diese Normalisierung greift die Sperre nie: der Watchdog misst alle
    30s einen anderen Wert und jede Messung waere ein neues Thema.
    """
    a = await discord_notify.notify_event(
        "system.slow_response", "Datenbank antwortet langsam (342ms)", "error",
    )
    b = await discord_notify.notify_event(
        "system.slow_response", "Datenbank antwortet langsam (501ms)", "error",
    )
    assert a == "sent"
    assert b == "suppressed"
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_verschiedene_themen_werden_nicht_verschluckt(sent):
    a = await discord_notify.notify_event(
        "runtime.unreachable", "qwen-general: nicht erreichbar", "error",
    )
    b = await discord_notify.notify_event(
        "runtime.unreachable", "deepseek-v4: nicht erreichbar", "error",
    )
    assert a == "sent" and b == "sent"
    assert len(sent) == 2, "Zwei verschiedene Runtimes sind zwei echte Meldungen"


@pytest.mark.asyncio
async def test_nach_ablauf_der_sperre_meldet_es_sich_wieder(sent, _redis):
    """Ein dauerhaftes Problem darf nicht fuer immer verstummen."""
    await discord_notify.notify_event(
        "runtime.unreachable", "qwen-general: nicht erreichbar", "error",
    )
    # Sperrschluessel ablaufen lassen, statt echte Zeit zu vergehen
    keys = [k async for k in _redis.scan_iter("mc:discord:seen:*")]
    assert keys, "Die Sperre muss ueberhaupt einen Schluessel setzen"
    for k in keys:
        await _redis.delete(k)

    again = await discord_notify.notify_event(
        "runtime.unreachable", "qwen-general: nicht erreichbar", "error",
    )
    assert again == "sent"
    assert len(sent) == 2


# ── Sammelmeldung ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_leere_sammlung_sendet_nichts(sent):
    flushed = await discord_notify.flush_digest()
    assert flushed == 0
    assert sent == []


@pytest.mark.asyncio
async def test_sammlung_wartet_solange_fenster_und_menge_klein_sind(sent):
    await discord_notify.notify_event(
        "runtime.unreachable", "qwen-general: nicht erreichbar", "warning",
    )
    flushed = await discord_notify.flush_digest()
    assert flushed == 0, "Eine frische, einzelne Warnung wird noch gesammelt"
    assert sent == []


@pytest.mark.asyncio
async def test_sammlung_geht_raus_wenn_zu_viele_warten(sent):
    for i in range(discord_notify.DIGEST_MAX_ITEMS):
        await discord_notify.notify_event(
            "runtime.unreachable", f"runtime-{i}: nicht erreichbar", "warning",
        )
    flushed = await discord_notify.flush_digest()
    assert flushed == discord_notify.DIGEST_MAX_ITEMS
    assert len(sent) == 1, "Viele Warnungen ergeben EINE Nachricht"


@pytest.mark.asyncio
async def test_sammlung_geht_raus_wenn_das_fenster_abgelaufen_ist(sent, _redis):
    await discord_notify.notify_event(
        "runtime.unreachable", "qwen-general: nicht erreichbar", "warning",
    )
    # Aeltesten Eintrag kuenstlich altern lassen
    raw = await _redis.lindex(discord_notify.DIGEST_KEY, 0)
    item = json.loads(raw)
    item["ts"] = item["ts"] - discord_notify.DIGEST_WINDOW_SECONDS - 1
    await _redis.lset(discord_notify.DIGEST_KEY, 0, json.dumps(item))

    flushed = await discord_notify.flush_digest()
    assert flushed == 1
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_sammlung_ist_danach_leer(sent):
    for i in range(discord_notify.DIGEST_MAX_ITEMS):
        await discord_notify.notify_event(
            "runtime.unreachable", f"runtime-{i}: nicht erreichbar", "warning",
        )
    await discord_notify.flush_digest()
    zweiter = await discord_notify.flush_digest()
    assert zweiter == 0, "Was einmal gemeldet wurde, darf nicht erneut kommen"
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_sammelmeldung_zaehlt_wiederholungen_zusammen(sent, _redis):
    """13x dieselbe Zeile soll als '13x ...' erscheinen, nicht 13 Zeilen."""
    for _ in range(3):
        await discord_notify.notify_event(
            "runtime.unreachable", "qwen-general: nicht erreichbar", "warning",
        )
        # Sperre loeschen, damit alle drei in der Sammlung landen
        async for k in _redis.scan_iter("mc:discord:seen:*"):
            await _redis.delete(k)
    for i in range(discord_notify.DIGEST_MAX_ITEMS - 3):
        await discord_notify.notify_event(
            "runtime.unreachable", f"anderes-{i}: nicht erreichbar", "warning",
        )

    await discord_notify.flush_digest()
    assert len(sent) == 1
    text = sent[0]["description"]
    assert "3x" in text or "3×" in text, f"Wiederholungen muessen gebuendelt sein: {text}"


@pytest.mark.asyncio
async def test_sammelmeldung_nennt_die_anzahl_im_titel(sent):
    for i in range(discord_notify.DIGEST_MAX_ITEMS):
        await discord_notify.notify_event(
            "runtime.unreachable", f"runtime-{i}: nicht erreichbar", "warning",
        )
    await discord_notify.flush_digest()
    assert str(discord_notify.DIGEST_MAX_ITEMS) in sent[0]["title"]


@pytest.mark.asyncio
async def test_aehnliche_namen_mit_ziffer_bleiben_getrennt(sent):
    """Nur eingeklammerte Messwerte sind Rauschen — Ziffern im Namen nicht.

    Beim Bauen fiel diese Falle auf: eine pauschale Zahlen-Normalisierung
    machte aus 'deepseek-v4' und 'deepseek-v5' ein Thema und haette die
    zweite Runtime stumm geschaltet. Zwei verschiedene Anlagen sind zwei
    verschiedene Meldungen.
    """
    a = await discord_notify.notify_event(
        "runtime.unreachable", "deepseek-v4-flash: nicht erreichbar", "error",
    )
    b = await discord_notify.notify_event(
        "runtime.unreachable", "deepseek-v5-flash: nicht erreichbar", "error",
    )
    assert a == "sent" and b == "sent"
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_eingeklammerter_messwert_ist_rauschen(sent):
    a = await discord_notify.notify_event(
        "runtime.unreachable", "qwen-general: nicht erreichbar (3 Versuche)", "error",
    )
    b = await discord_notify.notify_event(
        "runtime.unreachable", "qwen-general: nicht erreichbar (7 Versuche)", "error",
    )
    assert a == "sent"
    assert b == "suppressed"
    assert len(sent) == 1
