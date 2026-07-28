"""Ausgehende Spiegelung: Thread-Nachricht -> Telegram-Thema (P2.3).

Seit ADR-072 ist dies nur noch **Telegrams Einstieg** in die kanal-neutrale
Pipeline: die Regeln (was ueberhaupt gespiegelt wird, wer spricht, wie laut es
ankommt, in welchen Raum) liegen in ``chat_outbound``, das Telegram-Verhalten
(Prefix statt Absender-Identitaet, Thema als Raum) in
``chat_telegram.TelegramChatAdapter``. Verhalten unveraendert — nur der Sitz
der Regeln.

Diese Funktion bleibt der dokumentierte Injektionspunkt: Topic-Client und Bot
werden hier hereingereicht, damit Tests ohne Netz laufen.

── Schleifenschutz (P2.4 baut darauf) ──────────────────────────────────────
Eine aus Telegram *eingehende* Nachricht (P2.4) darf nicht wieder nach Telegram
gespiegelt werden, sonst Endlosschleife. Es gibt ZWEI unabhaengige Sperren:

  1. `sender_type == "user"` wird nie gespiegelt. Mark ist die einzige
     Nutzerquelle; er hat die Nachricht selbst geschrieben (im Web oder aus
     Telegram). Das allein bricht die Schleife fuer den heutigen Inbound-Pfad,
     der eingehende Telegram-Nachrichten als sender_type="user" ablegt.
  2. Expliziter Herkunfts-Schalter `post_message(..., mirror_to_telegram=False)`.
     P2.4 setzt ihn beim Ingest aus Telegram — der dokumentierte Diskriminator,
     der auch dann schuetzt, wenn ein kuenftiger Inbound-Pfad einen anderen
     sender_type verwenden sollte.

Guertel und Hosentraeger: (1) ist semantisch, (2) ist explizit. Zusammen
garantieren sie, dass nichts, was aus Telegram kam, nach Telegram zurueckläuft.
Beide Sperren sitzen jetzt in ``chat_outbound``/``chat_inbound`` und gelten
damit fuer jeden Kanal.
"""
from datetime import datetime

from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.thread import Message

# Re-Export: die Ping-/Nachtruhe-Regeln sind kanal-neutral und wohnen in
# chat_outbound. Hier bleiben sie importierbar, weil sie unter diesen Namen
# eingefuehrt wurden (und getestet werden).
from app.services.chat_outbound import (  # noqa: F401
    NIGHT_END_HOUR,
    NIGHT_START_HOUR,
    OPERATOR_TZ,
    _is_night,
    _mentions_mark,
    _ping_is_loud,
    _should_disable_notification,
    _skip_reason,
)
from app.services.chat_telegram import TelegramChatAdapter
from app.services.telegram_topics import ForumTopicClient


async def mirror_message_to_telegram(
    session: AsyncSession,
    message: Message,
    *,
    topic_client: ForumTopicClient,
    bot,
    now: datetime | None = None,
) -> bool:
    """Spiegle eine Thread-Message in ihr Telegram-Thema.

    Gibt True zurueck, wenn ein Sendeversuch lief, sonst False (uebersprungen
    oder Telegram nicht bereit). Wirft NIE — jeder Fehler wird geloggt, damit der
    Aufrufer (`post_message`) und die Agentenarbeit nie kippen.
    """
    from app.services.chat_outbound import mirror_message

    adapter = TelegramChatAdapter(topic_client=topic_client, bot=bot)
    return await mirror_message(session, message, adapter, now=now)
