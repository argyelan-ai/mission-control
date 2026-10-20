"""Alarm-Dedup fuer #mc-alerts: max. 1x pro 60 min je Schluessel.

Task bf8d9fa4: Der Kanal ertrinkt in Wiederholungen (98x runtime.unreachable
in 7 Tagen). Regelwerk, hier festgenagelt:

  1. Schluessel: (event_type, betroffene Runtime-/Agent-ID). Erste Meldung
     geht raus; alles Weitere im 60-Minuten-Fenster wird in Redis gezaehlt
     (Key ``mc:alert:dedup:<key>``, TTL 3600) und unterdrueckt.
  2. Beim naechsten erlaubten Senden haengt der Text
     "(+N gleiche Meldungen in der letzten Stunde)" an.
  3. ``critical`` geht IMMER sofort raus und wird NIE dedupt.

Redis kommt aus der bestehenden Fake-Redis-Isolation (conftest), Discord-
Versand wird an derselben Stelle abgefangen wie in test_discord_alert_noise.
"""

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


RUNTIME_DETAIL = {"slug": "qwen-general"}


# ── Pflichtfall 1: Erste Meldung geht raus ───────────────────────────────

@pytest.mark.asyncio
async def test_erste_meldung_geht_raus(sent):
    result = await discord_notify.notify_event(
        "runtime.unreachable", "qwen-general: endpoint unreachable", "error",
        detail=RUNTIME_DETAIL,
    )
    assert result == "sent"
    assert len(sent) == 1


# ── Pflichtfall 2: 2-5 im Fenster gehen NICHT raus, Zaehler = 4 ──────────

@pytest.mark.asyncio
async def test_wiederholungen_im_fenster_werden_gezaehlt_und_unterdrueckt(sent, _redis):
    first = await discord_notify.notify_event(
        "runtime.unreachable", "qwen-general: endpoint unreachable", "error",
        detail=RUNTIME_DETAIL,
    )
    results = [
        await discord_notify.notify_event(
            "runtime.unreachable", "qwen-general: endpoint unreachable", "error",
            detail=RUNTIME_DETAIL,
        )
        for _ in range(4)
    ]
    assert first == "sent"
    assert results == ["suppressed"] * 4, "Im 60-min-Fenster darf nur die erste Meldung raus"
    assert len(sent) == 1

    counter = await _redis.get("mc:alert:dedupcnt:runtime.unreachable|qwen-general")
    assert counter == "4", f"Zaehler muss auf 4 stehen, steht auf {counter}"


@pytest.mark.asyncio
async def test_zaehler_key_hat_ttl_3600(sent, _redis):
    await discord_notify.notify_event(
        "runtime.unreachable", "qwen-general: endpoint unreachable", "error",
        detail=RUNTIME_DETAIL,
    )
    ttl = await _redis.ttl("mc:alert:dedup:runtime.unreachable|qwen-general")
    assert 0 < ttl <= 3600


# ── Pflichtfall 3: Nach TTL-Ablauf geht die naechste raus, mit "+4" ──────

@pytest.mark.asyncio
async def test_nach_ablauf_geht_naechste_mit_plus4_raus(sent, _redis):
    await discord_notify.notify_event(
        "runtime.unreachable", "qwen-general: endpoint unreachable", "error",
        detail=RUNTIME_DETAIL,
    )
    for _ in range(4):
        await discord_notify.notify_event(
            "runtime.unreachable", "qwen-general: endpoint unreachable", "error",
            detail=RUNTIME_DETAIL,
        )

    # Nur die Sperre ablaufen lassen — der Zaehler ueberlebt das Fenster
    await _redis.delete("mc:alert:dedup:runtime.unreachable|qwen-general")

    result = await discord_notify.notify_event(
        "runtime.unreachable", "qwen-general: endpoint unreachable", "error",
        detail=RUNTIME_DETAIL,
    )
    assert result == "sent"
    assert len(sent) == 2
    text = sent[1]["description"]
    assert "(+4 gleiche Meldungen in der letzten Stunde)" in text, f"+4 fehlt: {text}"


@pytest.mark.asyncio
async def test_ohne_wiederholungen_kein_plus_anhang(sent):
    await discord_notify.notify_event(
        "runtime.unreachable", "qwen-general: endpoint unreachable", "error",
        detail=RUNTIME_DETAIL,
    )
    assert "+0" not in sent[0]["description"]
    assert "gleiche Meldungen" not in sent[0]["description"]


# ── Pflichtfall 4: critical umgeht die Dedup komplett ────────────────────

@pytest.mark.asyncio
async def test_critical_geht_immer_raus_und_wird_nie_dedupt(sent, _redis):
    for _ in range(5):
        result = await discord_notify.notify_event(
            "db.outage", "Datenbank nicht erreichbar", "critical",
            detail=RUNTIME_DETAIL,
        )
        assert result == "sent"
    assert len(sent) == 5, "critical darf nie dedupt werden"


@pytest.mark.asyncio
async def test_critical_hinterlaesst_keinen_dedup_key(sent, _redis):
    await discord_notify.notify_event(
        "db.outage", "Datenbank nicht erreichbar", "critical",
        detail=RUNTIME_DETAIL,
    )
    keys = [k async for k in _redis.scan_iter("mc:alert:dedup:*")]
    assert keys == [], "critical darf den Zaehler nicht anfassen"


@pytest.mark.asyncio
async def test_critical_geht_raus_waehrend_gleicher_key_gesperrt_ist(sent, _redis):
    await discord_notify.notify_event(
        "runtime.unreachable", "qwen-general: endpoint unreachable", "error",
        detail=RUNTIME_DETAIL,
    )
    blocked = await discord_notify.notify_event(
        "runtime.unreachable", "qwen-general: endpoint unreachable", "error",
        detail=RUNTIME_DETAIL,
    )
    critical = await discord_notify.notify_event(
        "runtime.unreachable", "qwen-general: endpoint unreachable", "critical",
        detail=RUNTIME_DETAIL,
    )
    assert blocked == "suppressed"
    assert critical == "sent"


# ── Schluessel-Bildung: (event_type, betroffene ID) ──────────────────────

@pytest.mark.asyncio
async def test_verschiedene_runtimes_werden_nicht_zusammen_gededupt(sent):
    a = await discord_notify.notify_event(
        "runtime.unreachable", "qwen-general: endpoint unreachable", "error",
        detail={"slug": "qwen-general"},
    )
    b = await discord_notify.notify_event(
        "runtime.unreachable", "deepseek-v4: endpoint unreachable", "error",
        detail={"slug": "deepseek-v4"},
    )
    assert a == "sent" and b == "sent"
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_agent_name_bildet_eigenen_schluessel(sent, _redis):
    await discord_notify.notify_event(
        "agent.restart_failed", "Rex: Neustart fehlgeschlagen", "error",
        detail={"agent_name": "Rex"},
    )
    again = await discord_notify.notify_event(
        "agent.restart_failed", "Rex: Neustart fehlgeschlagen", "error",
        detail={"agent_name": "Rex"},
    )
    assert again == "suppressed"
    keys = [k async for k in _redis.scan_iter("mc:alert:dedup:*")]
    assert keys == ["mc:alert:dedup:agent.restart_failed|Rex"]


@pytest.mark.asyncio
async def test_gleiche_id_andere_event_type_geht_raus(sent):
    await discord_notify.notify_event(
        "runtime.unreachable", "qwen-general: endpoint unreachable", "error",
        detail=RUNTIME_DETAIL,
    )
    result = await discord_notify.notify_event(
        "runtime.latency", "qwen-general: antwortet langsam", "error",
        detail=RUNTIME_DETAIL,
    )
    assert result == "sent", "Der Schluessel ist (event_type, ID) — andere Art, anderes Fenster"


@pytest.mark.asyncio
async def test_ohne_detail_faellt_key_auf_thema_zurueck(sent, _redis):
    """Emitter ohne detail-duengte ID: Sperre greift trotzdem (Fallback)."""
    await discord_notify.notify_event(
        "db.outage", "Datenbank nicht erreichbar", "error",
    )
    again = await discord_notify.notify_event(
        "db.outage", "Datenbank nicht erreichbar", "error",
    )
    assert again == "suppressed"
    keys = [k async for k in _redis.scan_iter("mc:alert:dedup:*")]
    assert len(keys) == 1 and keys[0].startswith("mc:alert:dedup:db.outage|")


# ── Pflichtfall 5: Eskalation ist keine Wiederholung ─────────────────────
#
# Anlass 2026-10-20 (Watcher-Takt 30 s, ADR-096 §11.4): die schlichte
# Warnung "endpoint unreachable (3 consecutive probes)" oeffnet das Fenster
# fuer (runtime.unreachable, <slug>). Bestaetigt der Anker den Engine-Tod
# erst danach (Proben 4..9), kommt derselbe Schluessel als ``error`` mit der
# Ursache — und wurde bisher als Wiederholung verschluckt. Eine dringendere
# Meldung zum selben Thema ist Neuigkeit: sie geht raus, uebernimmt das
# Fenster, und die ueberholte Warnung faellt aus der Sammlung.


@pytest.mark.asyncio
async def test_fehler_nach_warnung_zum_selben_thema_geht_sofort_raus(sent, _redis):
    first = await discord_notify.notify_event(
        "runtime.unreachable", "qwen-general: endpoint unreachable (3 consecutive probes)",
        "warning", detail=RUNTIME_DETAIL,
    )
    death = await discord_notify.notify_event(
        "runtime.unreachable", "qwen-general auf Box A beendet: Exit 137", "error",
        detail=RUNTIME_DETAIL,
    )
    assert first == "queued"
    assert death == "sent", "Die Ursache darf nicht als Wiederholung der Warnung verschluckt werden"
    assert [s["severity"] for s in sent] == ["error"]
    assert sent[0]["title"] == "qwen-general auf Box A beendet: Exit 137"
    # Das Fenster gehoert jetzt dem Fehler — volle Stunde ab der Eskalation.
    key = "mc:alert:dedup:runtime.unreachable|qwen-general"
    assert await _redis.get(key) == "error"
    assert 3590 <= await _redis.ttl(key) <= 3600


@pytest.mark.asyncio
async def test_nach_der_eskalation_wird_wieder_gesperrt(sent):
    await discord_notify.notify_event(
        "runtime.unreachable", "qwen-general: endpoint unreachable", "warning",
        detail=RUNTIME_DETAIL,
    )
    await discord_notify.notify_event(
        "runtime.unreachable", "qwen-general beendet", "error", detail=RUNTIME_DETAIL,
    )
    again = [
        await discord_notify.notify_event(
            "runtime.unreachable", "qwen-general beendet", severity, detail=RUNTIME_DETAIL,
        )
        for severity in ("error", "warning")
    ]
    assert again == ["suppressed", "suppressed"]
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_warnung_nach_fehler_bleibt_gesperrt(sent, _redis):
    """Nur aufwaerts ist Neuigkeit: eine leisere Meldung nach einem Fehler
    ist weiterhin eine Wiederholung und landet auch nicht in der Sammlung."""
    await discord_notify.notify_event(
        "runtime.unreachable", "qwen-general beendet", "error", detail=RUNTIME_DETAIL,
    )
    later = await discord_notify.notify_event(
        "runtime.unreachable", "qwen-general: endpoint unreachable", "warning",
        detail=RUNTIME_DETAIL,
    )
    assert later == "suppressed"
    assert await _redis.llen(discord_notify.DIGEST_KEY) == 0
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_eskalation_nimmt_die_ueberholte_warnung_aus_der_sammlung(sent, _redis):
    """Sonst kaeme die Warnung ohne Ursache bis zu 30 min NACH dem Fehler mit
    Ursache noch einmal an. Warnungen zu anderen Themen bleiben liegen."""
    await discord_notify.notify_event(
        "runtime.unreachable", "qwen-general: endpoint unreachable", "warning",
        detail=RUNTIME_DETAIL,
    )
    await discord_notify.notify_event(
        "runtime.unreachable", "deepseek-v4: endpoint unreachable", "warning",
        detail={"slug": "deepseek-v4"},
    )
    await discord_notify.notify_event(
        "runtime.unreachable", "qwen-general beendet", "error", detail=RUNTIME_DETAIL,
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(discord_notify, "DIGEST_WINDOW_SECONDS", 0)
        flushed = await discord_notify.flush_digest()
    assert flushed == 1
    digest = sent[-1]
    assert "deepseek-v4" in digest["description"]
    assert "qwen-general" not in digest["description"]


@pytest.mark.asyncio
async def test_alter_sperrschluessel_ohne_stufe_laesst_einen_fehler_durch(sent, _redis):
    """Schluessel von vor der Eskalationsregel tragen "1" statt der Stufe.
    Sie zaehlen als Warnung: lieber eine Meldung zu viel als eine verschluckte
    (dieselbe Abwaegung wie bei den Ziffern im Titel)."""
    await _redis.set("mc:alert:dedup:runtime.unreachable|qwen-general", 1, ex=3600)
    error = await discord_notify.notify_event(
        "runtime.unreachable", "qwen-general beendet", "error", detail=RUNTIME_DETAIL,
    )
    warning = await discord_notify.notify_event(
        "runtime.unreachable", "qwen-general: endpoint unreachable", "warning",
        detail={"slug": "qwen-general"},
    )
    assert error == "sent"
    assert warning == "suppressed"
