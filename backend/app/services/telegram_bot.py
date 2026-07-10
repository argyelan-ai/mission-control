"""
Telegram Bot Service — approval URL buttons for the operator.

Sends approvals as a Telegram message with URL buttons (no polling).
URLs point to quick-resolve endpoints (GET = confirmation page, POST = action).
Tokens are random one-time tokens in Redis (TTL 48h, single-use).

Stores message_id in Redis (TTL 2 days) for later message editing.

Pattern: Singleton (no more background loop — polling disabled).
"""

import asyncio
import json
import logging
import secrets
import uuid

import httpx

from app.config import settings
from app.redis_client import get_redis
from app.utils import utcnow

logger = logging.getLogger("mc.telegram_bot")

TELEGRAM_API = "https://api.telegram.org/bot{token}"
POLL_INTERVAL = 3  # seconds (legacy, unused)
REDIS_TTL = 172800  # 2 days
ACTION_TOKEN_TTL = 48 * 3600  # 48h


def _redis_key(approval_id: str) -> str:
    return f"mc:telegram:approval:{approval_id}"


# ── One-Time Token Helpers ──────────────────────────────────────────────


async def create_approval_tokens(approval_id: uuid.UUID) -> tuple[str, str]:
    """Generate 2 single-use tokens (approve + reject), store in Redis (TTL 48h)."""
    approve_token = secrets.token_urlsafe(32)
    reject_token = secrets.token_urlsafe(32)
    redis = await get_redis()

    await redis.set(
        f"mc:telegram:action_token:{approve_token}",
        json.dumps({"approval_id": str(approval_id), "action": "approve", "created_at": utcnow().isoformat()}),
        ex=ACTION_TOKEN_TTL,
    )
    await redis.set(
        f"mc:telegram:action_token:{reject_token}",
        json.dumps({"approval_id": str(approval_id), "action": "reject", "created_at": utcnow().isoformat()}),
        ex=ACTION_TOKEN_TTL,
    )
    # Sibling-Lookup for cleanup
    await redis.set(
        f"mc:telegram:approval_tokens:{approval_id}",
        json.dumps({"approve": approve_token, "reject": reject_token}),
        ex=ACTION_TOKEN_TTL,
    )
    return approve_token, reject_token


async def peek_action_token(token: str) -> dict | None:
    """Read token data WITHOUT consuming it. Returns {approval_id, action} or None."""
    redis = await get_redis()
    data = await redis.get(f"mc:telegram:action_token:{token}")
    if not data:
        return None
    return json.loads(data)


async def consume_action_token(token: str) -> dict | None:
    """Consume token (single-use). Returns {approval_id, action} or None."""
    redis = await get_redis()
    key = f"mc:telegram:action_token:{token}"
    data = await redis.get(key)
    if not data:
        return None  # Expired or already used

    payload = json.loads(data)
    # Delete this token (single-use)
    await redis.delete(key)
    # Delete sibling token too
    siblings_key = f"mc:telegram:approval_tokens:{payload['approval_id']}"
    siblings_raw = await redis.get(siblings_key)
    if siblings_raw:
        siblings = json.loads(siblings_raw)
        for t in siblings.values():
            await redis.delete(f"mc:telegram:action_token:{t}")
        await redis.delete(siblings_key)
    return payload


class TelegramBotService:
    def __init__(self):
        self._running = False
        self._task: asyncio.Task | None = None
        self._offset: int = 0  # getUpdates offset
        self._client: httpx.AsyncClient | None = None
        # Jarvis Telegram-Inbound (ADR-061). The handler stays inert unless the
        # feature is gated on (JARVIS_TELEGRAM_ENABLED + keys present).
        from app.services.jarvis_telegram import JarvisTelegramHandler
        self._jarvis = JarvisTelegramHandler(self)

    @property
    def configured(self) -> bool:
        return bool(settings.telegram_bot_token and settings.telegram_chat_id)

    def _api_url(self, method: str) -> str:
        return f"{TELEGRAM_API.format(token=settings.telegram_bot_token)}/{method}"

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30)
        return self._client

    # ── Bot API Methods ─────────────────────────────────────────────────

    async def send_message(
        self, text: str, reply_markup: dict | None = None
    ) -> int | None:
        """Send message, return message_id or None on failure."""
        client = await self._get_client()
        payload: dict = {
            "chat_id": settings.telegram_chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)

        try:
            resp = await client.post(self._api_url("sendMessage"), data=payload)
            data = resp.json()
            if data.get("ok"):
                return data["result"]["message_id"]
            logger.warning("sendMessage failed: %s", data.get("description"))
        except Exception as e:
            logger.warning("sendMessage error: %s", e)
        return None

    async def send_photo(
        self, photo_path: str, caption: str | None = None
    ) -> int | None:
        """Send photo from local file, return message_id or None."""
        import pathlib
        path = pathlib.Path(photo_path)
        if not path.exists() or path.stat().st_size < 1024:
            logger.warning("send_photo skipped: %s (missing or too small)", photo_path)
            return None
        client = await self._get_client()
        data: dict = {
            "chat_id": settings.telegram_chat_id,
        }
        if caption:
            data["caption"] = caption[:1024]  # Telegram caption limit
            data["parse_mode"] = "HTML"
        try:
            with open(photo_path, "rb") as f:
                resp = await client.post(
                    self._api_url("sendPhoto"),
                    data=data,
                    files={"photo": (path.name, f, "image/png")},
                )
            result = resp.json()
            if result.get("ok"):
                return result["result"]["message_id"]
            logger.warning("sendPhoto failed: %s", result.get("description"))
        except Exception as e:
            logger.warning("sendPhoto error: %s", e)
        return None

    async def edit_message_text(self, message_id: int, text: str) -> bool:
        """Edit message text (removes inline keyboard). Returns success."""
        client = await self._get_client()
        payload = {
            "chat_id": settings.telegram_chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
        }
        try:
            resp = await client.post(
                self._api_url("editMessageText"), data=payload
            )
            data = resp.json()
            if not data.get("ok"):
                logger.warning("editMessageText failed: %s", data.get("description"))
                return False
            return True
        except Exception as e:
            logger.warning("editMessageText error: %s", e)
            return False

    async def get_file_bytes(self, file_id: str) -> bytes | None:
        """Resolve a Telegram file_id → download its bytes (voice notes etc.).

        Two-step Telegram flow: getFile returns a temporary file_path, then the
        binary is fetched from the /file/bot<token>/<path> URL. Returns None on
        any failure (caller degrades gracefully).
        """
        client = await self._get_client()
        try:
            resp = await client.get(self._api_url("getFile"), params={"file_id": file_id})
            data = resp.json()
            if not data.get("ok"):
                logger.warning("getFile failed: %s", data.get("description"))
                return None
            file_path = data["result"]["file_path"]
            file_url = (
                f"https://api.telegram.org/file/bot"
                f"{settings.telegram_bot_token}/{file_path}"
            )
            file_resp = await client.get(file_url)
            if file_resp.status_code != 200:
                logger.warning("Telegram file download failed: %s", file_resp.status_code)
                return None
            return file_resp.content
        except Exception as e:
            logger.warning("get_file_bytes error: %s", e)
            return None

    async def answer_callback_query(
        self, callback_query_id: str, text: str
    ) -> None:
        """Acknowledge button click."""
        client = await self._get_client()
        try:
            await client.post(
                self._api_url("answerCallbackQuery"),
                data={"callback_query_id": callback_query_id, "text": text},
            )
        except Exception as e:
            logger.warning("answerCallbackQuery error: %s", e)

    # ── Approval-specific ────────────────────────────────────────────────

    async def send_approval_telegram(
        self,
        approval_id: uuid.UUID,
        agent_name: str,
        task_title: str,
        blocker_comment: str,
    ) -> None:
        """Send approval notification with URL buttons (no polling needed)."""
        if not self.configured:
            return

        approve_token, reject_token = await create_approval_tokens(approval_id)

        base = settings.mc_base_url.rstrip("/")
        approve_url = f"{base}/api/v1/approvals/{approval_id}/quick-resolve?token={approve_token}"
        reject_url = f"{base}/api/v1/approvals/{approval_id}/quick-resolve?token={reject_token}"

        text = (
            f"<b>Approval noetig</b>\n\n"
            f"<b>Agent:</b> {_escape_html(agent_name)}\n"
            f"<b>Task:</b> {_escape_html(task_title)}\n\n"
            f"<b>Blocker:</b>\n{_escape_html(blocker_comment[:500])}"
        )

        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "Entblocken", "url": approve_url},
                    {"text": "Abbrechen", "url": reject_url},
                ]
            ]
        }

        message_id = await self.send_message(text, reply_markup)
        if message_id:
            redis = await get_redis()
            await redis.set(
                _redis_key(str(approval_id)),
                str(message_id),
                ex=REDIS_TTL,
            )
            logger.info(
                "Approval %s sent to Telegram (msg=%d, URL buttons)", approval_id, message_id
            )

    async def update_resolved_telegram(
        self, approval_id: uuid.UUID, status: str, resolver_note: str | None = None
    ) -> None:
        """Update Telegram message when approval is resolved (via UI or button)."""
        if not self.configured:
            return

        redis = await get_redis()
        key = _redis_key(str(approval_id))
        message_id_str = await redis.get(key)
        if not message_id_str:
            return

        message_id = int(message_id_str)
        emoji = "✅" if status == "approved" else "❌"
        source = "Telegram" if not resolver_note else "Dashboard"
        note_line = f"\n\n<b>Notiz:</b> {_escape_html(resolver_note)}" if resolver_note else ""

        text = (
            f"{emoji} <b>Approval {status}</b> (via {source})"
            f"{note_line}"
        )

        success = await self.edit_message_text(message_id, text)
        if success:
            await redis.delete(key)

    # ── Poller ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        # Approval notifications use URL buttons (no polling needed). Polling is
        # (re)started ONLY for Jarvis Telegram-Inbound (ADR-061), gated behind
        # JARVIS_TELEGRAM_ENABLED. With the feature off, behaviour is unchanged:
        # no getUpdates loop at all.
        if settings.jarvis_telegram_enabled and not self._jarvis.core_available:
            logger.warning(
                "JARVIS_TELEGRAM_ENABLED=true but jarvis_core is not importable "
                "— Telegram-Jarvis disabled (check the ./jarvis_core mount)."
            )
        if not self._jarvis.enabled:
            logger.info("Telegram bot ready (inbound disabled — URL-button approvals only)")
            return
        if not self.configured:
            logger.info("Telegram bot not configured — skipping Jarvis inbound poll")
            return
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("Jarvis Telegram inbound poller started (interval=%ds)", POLL_INTERVAL)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
        logger.info("Telegram poller stopped")

    async def _poll_loop(self) -> None:
        await asyncio.sleep(5)  # Grace period
        while self._running:
            try:
                await self._poll_updates()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Telegram poll error: %s", e)
            await asyncio.sleep(POLL_INTERVAL)

    async def _poll_updates(self) -> None:
        client = await self._get_client()
        try:
            resp = await client.get(
                self._api_url("getUpdates"),
                params={
                    "offset": self._offset,
                    "timeout": 1,
                    # ADR-061: subscribe to message updates too (Jarvis inbound).
                    "allowed_updates": json.dumps(["callback_query", "message"]),
                },
            )
            data = resp.json()
        except Exception as e:
            logger.debug("getUpdates failed: %s", e)
            return

        if not data.get("ok"):
            return

        for update in data.get("result", []):
            self._offset = update["update_id"] + 1
            callback = update.get("callback_query")
            if callback:
                await self._handle_callback(callback)
                continue
            message = update.get("message")
            if message:
                await self._handle_inbound_message(message)

    async def _handle_inbound_message(self, message: dict) -> None:
        """Route an inbound Telegram message to the Jarvis handler (ADR-061).

        Wrapped so a handler error never breaks the poll loop. The chat_id gate
        lives inside JarvisTelegramHandler.handle_message.
        """
        try:
            await self._jarvis.handle_message(message)
        except Exception as e:  # noqa: BLE001 — isolate per-message failures
            logger.exception("Jarvis inbound handler error: %s", e)

    async def _handle_callback(self, callback: dict) -> None:
        callback_id = callback["id"]
        from_user = callback.get("from", {})
        chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))

        # Security: only accept the operator's chat ID
        if chat_id != settings.telegram_chat_id:
            logger.warning(
                "Callback from unauthorized chat %s (user: %s)",
                chat_id,
                from_user.get("username", "unknown"),
            )
            await self.answer_callback_query(callback_id, "Nicht autorisiert.")
            return

        data = callback.get("data", "")
        if ":" not in data:
            await self.answer_callback_query(callback_id, "Ungueltige Aktion.")
            return

        action, approval_id_str = data.split(":", 1)
        if action not in ("approve", "reject"):
            await self.answer_callback_query(callback_id, "Unbekannte Aktion.")
            return

        try:
            approval_id = uuid.UUID(approval_id_str)
        except ValueError:
            await self.answer_callback_query(callback_id, "Ungueltige ID.")
            return

        # Resolve approval
        resolved = await self._resolve_approval(approval_id, action)
        if resolved:
            status_text = "Entblockt" if action == "approve" else "Abgebrochen"
            await self.answer_callback_query(callback_id, f"{status_text}!")
            await self.update_resolved_telegram(approval_id, "approved" if action == "approve" else "rejected")
        else:
            await self.answer_callback_query(callback_id, "Bereits erledigt.")

    async def _resolve_approval(self, approval_id: uuid.UUID, action: str) -> bool:
        """Resolve approval in DB. Returns True if actually resolved."""
        from sqlmodel.ext.asyncio.session import AsyncSession
        from app.database import engine
        from app.models.approval import Approval
        from app.models.task import Task, TaskComment
        from app.services.activity import emit_event
        from app.utils import utcnow

        status = "approved" if action == "approve" else "rejected"

        async with AsyncSession(engine, expire_on_commit=False) as session:
            approval = await session.get(Approval, approval_id)
            if not approval or approval.status != "pending":
                return False

            approval.status = status
            approval.resolved_at = utcnow()
            approval.resolver_note = f"Via Telegram ({status})"
            session.add(approval)
            await session.commit()

            # Blocker decision: unblock/fail the task
            if approval.action_type == "blocker_decision" and approval.task_id:
                task = await session.get(Task, approval.task_id)
                if task and task.status == "blocked":
                    if status == "approved":
                        task.status = "in_progress"
                        task.updated_at = utcnow()
                        session.add(task)
                        await session.commit()
                        # Notify agent via TaskComment (runtime-agnostic delivery
                        # channel — cli-bridge / host poll /agent/me/comments).
                        # Phase 29 D-10: replaces the former gateway chat path.
                        if task.assigned_agent_id:
                            session.add(TaskComment(
                                task_id=task.id,
                                author_type="user",
                                content=(
                                    f'**UNBLOCKED:** "{task.title}"\n\n'
                                    f"Anweisung des Operators: Via Telegram entblockt.\n\n"
                                    f"**Aktion:** Weiterarbeiten."
                                ),
                                comment_type="resolution",
                            ))
                            await session.commit()
                    elif status == "rejected":
                        task.status = "failed"
                        task.updated_at = utcnow()
                        # Auto-unassign — a failed task in agent_poll would otherwise
                        # trigger a cancel loop. The operator explicitly cancelled
                        # the task via Telegram.
                        from app.services.task_lifecycle import apply_terminal_unassign
                        await apply_terminal_unassign(session, task, "failed")
                        session.add(task)
                        await session.commit()

            await emit_event(
                session,
                "approval.resolved",
                f"Approval {status} via Telegram: {approval.description}",
                board_id=approval.board_id,
                agent_id=approval.agent_id,
                detail={"status": status, "source": "telegram"},
            )

        return True


def _escape_html(text: str) -> str:
    """Escape HTML special chars for Telegram HTML parse mode."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# Singleton
telegram_bot = TelegramBotService()
