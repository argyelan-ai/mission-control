"""ChatAdapter TCK — conformance suite every chat channel must pass (ADR-072).

The team chat's rules were born inside the Telegram modules. This suite is what
makes a SECOND channel cheap: it parametrises over the registered adapters
(``chat_adapter._registry()``) and asserts, for each of them, the laws the
neutral pipeline depends on. A Slack adapter that registers itself + ships a
harness (``tests/chat_harnesses.py``) must pass this file unchanged.

The laws, and why each exists:

  1. **Identity is data, degradation is visible.** Telegram sends everything
     from one bot, so an agent name can only be a text prefix; Slack can set a
     per-message sender. A channel may render identity how it likes, but it may
     never DROP it (the operator must always know who said something).
  2. **The neutral rules are not reimplemented.** Loop protection, night quiet
     hours and the loudness rule are asserted THROUGH ``adapter.mirror_message``
     — an adapter that bypasses ``chat_outbound`` fails here.
  3. **A chat channel never breaks agent work.** Every operation degrades on a
     dead transport (False/None/0 + log) instead of raising.
  4. **Never guess an inbound room.** An unknown room resolves to None so the
     neutral path can ask back.
  5. **The switch is real.** Several channels may run at once; no channel at
     all is a silent, error-free state.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime

import pytest

from app.config import settings
from app.models.agent import Agent
from app.models.task import Task
from app.models.thread import Message, Thread
from app.services import chat_adapter as reg
from app.services.chat_adapter import (
    BaseChatAdapter,
    ChatAdapter,
    ChatCapabilities,
    ChatSender,
    OutboundChatMessage,
)
from tests.chat_harnesses import CHAT_HARNESS_FACTORIES, ChatHarness, SentRecord

DAY = datetime(2026, 7, 27, 14, 0, 0)     # 14:00 — Tag
NIGHT = datetime(2026, 7, 27, 2, 0, 0)    # 02:00 — Nachtruhe

_REGISTERED_KEYS = sorted(reg._registry())


@pytest.fixture(params=_REGISTERED_KEYS)
def harness(request, monkeypatch) -> ChatHarness:
    """One harness per registered adapter — the suite runs for every channel."""
    factory = CHAT_HARNESS_FACTORIES.get(request.param)
    if factory is None:
        pytest.fail(
            f"chat adapter '{request.param}' is registered but has no harness in "
            f"tests/chat_harnesses.py — it would silently skip the TCK."
        )
    h = factory()
    h.enable(monkeypatch)
    monkeypatch.setattr(settings, "chat_channels", "", raising=False)
    return h


# ── Helfer ────────────────────────────────────────────────────────────────


async def _thread(session, *, kind="task") -> Thread:
    t = Thread(kind=kind)
    if kind == "dm":
        t.agent_id = uuid.uuid4()
    session.add(t)
    await session.commit()
    await session.refresh(t)
    return t


async def _agent(session, name="Rex") -> Agent:
    a = Agent(name=name)
    session.add(a)
    await session.commit()
    await session.refresh(a)
    return a


def _msg(thread, *, sender_type="agent", sender_id=None, body="fertig",
         message_type="message", mentions=None, question_meta=None) -> Message:
    return Message(
        thread_id=thread.id,
        seq=1,
        sender_type=sender_type,
        sender_id=sender_id,
        message_type=message_type,
        body=body,
        mentions=mentions if mentions is not None else [],
        question_meta=question_meta,
    )


# ── 0. Registry-Form ──────────────────────────────────────────────────────


@pytest.mark.parametrize("key", _REGISTERED_KEYS)
def test_every_registered_adapter_has_a_harness(key):
    """Registrieren ohne Harness hiesse: der Kanal laeuft ungeprueft mit."""
    assert key in CHAT_HARNESS_FACTORIES, (
        f"adapter '{key}' is registered but has no TCK harness"
    )


@pytest.mark.parametrize("key", _REGISTERED_KEYS)
def test_adapter_declares_its_identity_and_capabilities(key):
    adapter = reg.get_chat_adapter(key)
    assert isinstance(adapter, ChatAdapter)
    assert adapter.key == key and adapter.key.strip()
    assert adapter.label.strip(), "a channel needs a display label"
    assert isinstance(adapter.capabilities, ChatCapabilities)


@pytest.mark.parametrize("key", _REGISTERED_KEYS)
def test_switch_flags_answer_without_raising(key):
    """Die Schalter werden bei JEDER Nachricht abgefragt — sie duerfen unter
    keiner Konfiguration werfen (sonst kippt post_message)."""
    adapter = reg.get_chat_adapter(key)
    assert isinstance(adapter.is_enabled(), bool)
    assert isinstance(adapter.is_configured(), bool)


def test_registry_keys_are_unique_and_lowercase():
    keys = list(reg._registry())
    assert len(keys) == len(set(keys))
    assert all(k == k.lower() for k in keys)


# ── 1. Absender-Identitaet ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sender_identity_is_carried_or_visibly_degraded(async_session, harness):
    """Gesetz 1: Wer spricht, geht nie verloren.

    Kanal mit Identitaet -> eigenes Absenderfeld. Kanal ohne (Telegram: ein
    einziger Bot) -> der Name MUSS sichtbar im Text stehen.
    """
    thread = await _thread(async_session)
    await harness.bind_room(async_session, thread)
    agent = await _agent(async_session, "Rex")

    ok = await harness.adapter.mirror_message(
        async_session, _msg(thread, sender_id=agent.id, body="fertig"), now=DAY
    )

    assert ok is True
    assert harness.sent, "the message must reach the channel transport"
    rec: SentRecord = harness.sent[-1]
    if harness.adapter.capabilities.sender_identity:
        assert rec.sender_name == "Rex"
    else:
        assert "Rex" in rec.text, (
            "a channel that cannot carry identity must degrade it visibly, not drop it"
        )
    assert "fertig" in rec.text


@pytest.mark.asyncio
async def test_system_messages_are_attributed_too(async_session, harness):
    thread = await _thread(async_session)
    await harness.bind_room(async_session, thread)

    await harness.adapter.mirror_message(
        async_session, _msg(thread, sender_type="system", body="Watchdog"), now=DAY
    )

    rec = harness.sent[-1]
    assert (rec.sender_name == "System") or ("System" in rec.text)


@pytest.mark.asyncio
async def test_bot_notice_carries_no_sender(async_session, harness):
    """``sender=None`` = der Kanal spricht selbst (Rueckfrage an den Operator).
    Dann darf kein fremder Name drangeklebt werden."""
    room = await harness.bind_room(async_session, await _thread(async_session))

    sent = await harness.adapter.send(
        room, OutboundChatMessage(body="Zu welcher Aufgabe gehört das?")
    )

    assert sent is True
    assert harness.sent[-1].text == "Zu welcher Aufgabe gehört das?"


# ── 2. Neutrale Regeln werden benutzt, nicht nachgebaut ───────────────────


@pytest.mark.asyncio
async def test_operator_messages_are_never_mirrored_back(async_session, harness):
    """Schleifenschutz: was aus dem Chat kam (sender_type="user"), geht nie
    zurueck in den Chat."""
    thread = await _thread(async_session)
    await harness.bind_room(async_session, thread)

    ok = await harness.adapter.mirror_message(
        async_session, _msg(thread, sender_type="user", body="mach das anders"), now=DAY
    )

    assert ok is False
    assert harness.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "now,kind,expect_silent",
    [
        (DAY, "plain", True),        # Alltagsnachricht: stumm
        (DAY, "question", False),    # Frage an Mark: laut
        (DAY, "mention", False),     # @Mark: laut
        (NIGHT, "question", True),   # Nachtruhe schlaegt die Ping-Regel
        (NIGHT, "critical", False),  # ausser: kritisch
    ],
)
async def test_ping_rule_and_night_quiet_hours(async_session, harness, now, kind, expect_silent):
    thread = await _thread(async_session)
    await harness.bind_room(async_session, thread)
    kwargs = {
        "plain": {},
        "question": {"message_type": "question", "question_meta": {"awaiting": True}},
        "mention": {"mentions": ["@Mark"]},
        "critical": {
            "message_type": "question",
            "question_meta": {"awaiting": True, "priority": "critical"},
        },
    }[kind]

    await harness.adapter.mirror_message(async_session, _msg(thread, **kwargs), now=now)

    assert harness.sent[-1].silent is expect_silent


# ── 3. Ein Chat-Ausfall darf nie Agentenarbeit kippen ─────────────────────


@pytest.mark.asyncio
async def test_send_degrades_instead_of_raising(async_session, harness):
    room = await harness.bind_room(async_session, await _thread(async_session))
    harness.break_transport()

    assert await harness.adapter.send(room, OutboundChatMessage(body="hi")) is False


@pytest.mark.asyncio
async def test_mirror_degrades_instead_of_raising(async_session, harness):
    thread = await _thread(async_session)
    await harness.bind_room(async_session, thread)
    harness.break_transport()

    assert await harness.adapter.mirror_message(async_session, _msg(thread), now=DAY) is False


@pytest.mark.asyncio
async def test_ensure_room_degrades_when_the_channel_is_not_ready(async_session, harness):
    if not harness.adapter.capabilities.rooms:
        pytest.skip(f"{harness.key}: channel has no per-thread rooms")
    thread = await _thread(async_session)  # noch ohne Raum
    harness.break_transport()

    assert await harness.adapter.ensure_room(async_session, thread) is None


@pytest.mark.asyncio
async def test_ensure_room_is_idempotent(async_session, harness):
    if not harness.adapter.capabilities.rooms:
        pytest.skip(f"{harness.key}: channel has no per-thread rooms")
    thread = await _thread(async_session)

    first = await harness.adapter.ensure_room(async_session, thread)
    second = await harness.adapter.ensure_room(async_session, thread)

    assert first is not None and first == second


@pytest.mark.asyncio
async def test_handle_task_done_never_raises(async_session, harness):
    task = Task(title="fertig", status="done", board_id=uuid.uuid4())
    async_session.add(task)
    await async_session.commit()
    await async_session.refresh(task)
    harness.break_transport()

    await harness.adapter.handle_task_done(async_session, task)  # darf nicht werfen


@pytest.mark.asyncio
async def test_purge_rooms_returns_a_count_and_never_raises(async_session, harness):
    harness.break_transport()

    purged = await harness.adapter.purge_rooms(30)

    assert isinstance(purged, int) and purged >= 0


# ── 4. Eingehend: niemals raten ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_room_resolves_to_none(async_session, harness):
    """Ein Raum, den MC nicht kennt, darf NICHT auf irgendeinen Thread fallen.

    Es gibt bewusst zwei Koeder-Threads (einer davon mit Raum): eine
    Implementierung, die „nimm halt den naechstbesten" macht, faellt hier auf —
    ohne Koeder waere der Test in einer leeren DB trivial gruen (genau das hat
    die Sabotage-Probe gezeigt).
    """
    await harness.bind_room(async_session, await _thread(async_session))
    await _thread(async_session)

    assert await harness.adapter.resolve_thread_for_room(
        async_session, harness.unknown_room
    ) is None


@pytest.mark.asyncio
async def test_known_room_resolves_to_its_thread(async_session, harness):
    thread = await _thread(async_session)
    room = await harness.bind_room(async_session, thread)

    found = await harness.adapter.resolve_thread_for_room(async_session, room)

    assert found is not None and found.id == thread.id


# ── 5. Der Kanal-Schalter ─────────────────────────────────────────────────


class _DummyAdapter(BaseChatAdapter):
    """Ein zweiter Kanal — genau das, wofuer der Kontrakt existiert.
    Roomless und mit eigener Absender-Identitaet, also das Gegenteil von
    Telegram: beweist, dass die neutrale Pipeline beide Formen bedient."""

    key = "dummy"
    label = "Dummy"
    capabilities = ChatCapabilities(sender_identity=True, rooms=False)

    def __init__(self):
        self.sent: list[SentRecord] = []
        self.enabled = True
        self.configured = True

    def is_enabled(self) -> bool:
        return self.enabled

    def is_configured(self) -> bool:
        return self.configured

    async def send(self, room, message: OutboundChatMessage) -> bool:
        self.sent.append(
            SentRecord(
                room=room,
                text=message.body,
                silent=message.silent,
                sender_name=message.sender.display_name if message.sender else None,
            )
        )
        return True


@pytest.fixture
def two_channels(monkeypatch):
    """Telegram + ein zweiter Kanal, beide registriert."""
    dummy = _DummyAdapter()
    monkeypatch.setattr(
        reg, "_ADAPTERS", {**reg._registry(), "dummy": dummy}, raising=False
    )
    return dummy


def test_empty_switch_means_no_explicit_selection(monkeypatch):
    monkeypatch.setattr(settings, "chat_channels", "", raising=False)
    assert reg.selected_channel_keys() is None


def test_switch_selects_several_channels_at_once(monkeypatch, two_channels):
    monkeypatch.setattr(settings, "telegram_team_chat_enabled", True, raising=False)
    monkeypatch.setattr(settings, "telegram_bot_token", "t", raising=False)
    monkeypatch.setattr(settings, "telegram_chat_id", "1", raising=False)
    monkeypatch.setattr(settings, "chat_channels", "telegram,dummy", raising=False)

    keys = {a.key for a in reg.sendable_chat_adapters()}

    assert keys == {"telegram", "dummy"}


def test_switch_silences_a_channel_without_removing_it(monkeypatch, two_channels):
    """Der Operator will Telegram ruhigstellen — der Code bleibt, der Kanal
    schweigt."""
    monkeypatch.setattr(settings, "telegram_team_chat_enabled", True, raising=False)
    monkeypatch.setattr(settings, "telegram_bot_token", "t", raising=False)
    monkeypatch.setattr(settings, "telegram_chat_id", "1", raising=False)
    monkeypatch.setattr(settings, "chat_channels", "dummy", raising=False)

    keys = {a.key for a in reg.sendable_chat_adapters()}

    assert keys == {"dummy"}
    assert reg.get_chat_adapter("telegram") is not None, "der Kanal bleibt registriert"


def test_unknown_channel_key_is_ignored_not_fatal(monkeypatch):
    monkeypatch.setattr(settings, "chat_channels", "carrier-pigeon", raising=False)

    assert reg.selected_channel_keys() == {"carrier-pigeon"}
    assert reg.sendable_chat_adapters() == []


@pytest.mark.asyncio
async def test_no_active_channel_is_a_silent_no_op(async_session, monkeypatch, caplog):
    """Sauberer Aus-Zustand: nichts passiert, nichts wirft, nichts wird als
    Fehler geloggt."""
    from app.services.chat_outbound import mirror_message_to_all
    from app.services.chat_rooms import handle_task_done, purge_rooms_tick

    monkeypatch.setattr(settings, "chat_channels", "", raising=False)
    monkeypatch.setattr(settings, "telegram_team_chat_enabled", False, raising=False)
    thread = await _thread(async_session)
    task = Task(title="t", status="done", board_id=uuid.uuid4())
    async_session.add(task)
    await async_session.commit()

    with caplog.at_level(logging.WARNING):
        assert reg.enabled_chat_adapters() == []
        assert reg.sendable_chat_adapters() == []
        assert await mirror_message_to_all(async_session, _msg(thread)) == 0
        await handle_task_done(async_session, task)
        assert await purge_rooms_tick(30) == 0

    assert [r.message for r in caplog.records if r.levelno >= logging.WARNING] == []


@pytest.mark.asyncio
async def test_every_active_channel_gets_the_message(async_session, monkeypatch, two_channels):
    """Mehrere Kanaele gleichzeitig: die Nachricht geht an alle."""
    from app.services.chat_outbound import mirror_message_to_all

    monkeypatch.setattr(settings, "chat_channels", "dummy", raising=False)
    thread = await _thread(async_session)
    agent = await _agent(async_session, "Sparky")

    count = await mirror_message_to_all(
        async_session, _msg(thread, sender_id=agent.id, body="läuft"), now=DAY
    )

    assert count == 1
    assert two_channels.sent[-1].sender_name == "Sparky"
    assert two_channels.sent[-1].room is None, (
        "a roomless channel must not be asked to resolve a room"
    )


@pytest.mark.asyncio
async def test_one_failing_channel_does_not_take_down_the_others(
    async_session, monkeypatch, two_channels
):
    from app.services.chat_outbound import mirror_message_to_all

    class _Broken(BaseChatAdapter):
        key, label = "broken", "Broken"
        capabilities = ChatCapabilities(sender_identity=False, rooms=False)

        def is_enabled(self):
            return True

        def is_configured(self):
            return True

        async def mirror_message(self, session, message, *, now=None):
            raise RuntimeError("channel exploded")

    monkeypatch.setattr(
        reg, "_ADAPTERS", {"broken": _Broken(), "dummy": two_channels}, raising=False
    )
    monkeypatch.setattr(settings, "chat_channels", "", raising=False)
    thread = await _thread(async_session)

    count = await mirror_message_to_all(async_session, _msg(thread), now=DAY)

    assert count == 1
    assert two_channels.sent, "the healthy channel still received the message"


def test_catalog_reports_every_channel(monkeypatch, two_channels):
    monkeypatch.setattr(settings, "chat_channels", "dummy", raising=False)

    catalog = {row["key"]: row for row in reg.chat_channel_catalog()}

    # Deliberately a subset check, not an exact set: registering a channel must
    # be the only edit needed (the promise of ADR-072), and an exact set makes
    # THIS file the edit — as it did the moment Slack was registered.
    assert {"telegram", "dummy"} <= set(catalog)
    assert catalog["dummy"]["selected"] is True
    assert catalog["telegram"]["selected"] is False
    assert set(catalog["dummy"]["capabilities"]) == {"sender_identity", "rooms"}


def test_sender_is_a_value_not_a_string():
    """Absender-Identitaet ist ein eigenes Konzept — kein Formatierungsdetail.
    Wer sie rendert, entscheidet der Kanal."""
    sender = ChatSender(kind="agent", display_name="Rex", agent_id=uuid.uuid4())
    assert sender.display_name == "Rex"
    assert OutboundChatMessage(body="x").sender is None
