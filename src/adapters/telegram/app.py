"""
TelegramAdapter: Adapter for Telegram bot integration (unified interface).
Handles incoming messages from Telegram users and routes them to agents via orchestrator.
"""
import asyncio
import json
import traceback
from typing import Any
from adapters.adapter import Adapter
from core.logging_utils import log

try:
    from telegram import Bot, Update
    from telegram.constants import ChatAction
    from telegram.ext import Application, ChatMemberHandler, CommandHandler, MessageHandler, filters
except ImportError:
    log("adapters.telegram", "warning", "python-telegram-bot not installed; TelegramAdapter disabled")
    Bot = None
    Update = None
    Application = None
    ChatMemberHandler = None
    ChatAction = None


class TelegramAdapter(Adapter):
    """Adapter for handling Telegram bot messages and routing to agents."""

    def __init__(self, orchestrator, config: dict[str, Any] = None):
        """
        Initialize TelegramAdapter.
        Args:
            orchestrator: AgentOrchestrator instance
            config: Full application config (reads adapters.items.telegram)
        """
        super().__init__(config)
        self.orchestrator = orchestrator
        
        # Read Telegram settings from adapters.items.telegram in config
        telegram_config = self.config.get("adapters", {}).get("items", {}).get("telegram", {})
        self.enabled = telegram_config.get("enabled", False)
        self.token = telegram_config.get("token")
        self.default_agent = telegram_config.get("default_agent", "echo")
        self.polling = telegram_config.get("polling", True)
        self.polling_timeout = telegram_config.get("polling_timeout", 30)
        self.listen_private = bool(telegram_config.get("listen_private", True))
        self.listen_groups = bool(telegram_config.get("listen_groups", True))
        self.dialog_log_type = str(telegram_config.get("dialog_log_type", "telegram_dialog"))
        
        self.app = None
        self.bot = None
        self._stop_event = asyncio.Event()
        self._polling_active = False
        
        if self.enabled and self.token and Bot:
            self.bot = Bot(token=self.token)
            log(
                "adapters.telegram",
                "info",
                f"TelegramAdapter initialized (token={'***' + self.token[-4:]}, polling={self.polling}, timeout={self.polling_timeout}s, private={self.listen_private}, groups={self.listen_groups})",
            )
        elif self.enabled:
            log(
                "adapters.telegram",
                "error",
                f"TelegramAdapter enabled but cannot start (has_token={bool(self.token)}, dependency_ready={bool(Bot and Application and ChatAction)})",
            )

    def _is_chat_allowed(self, update: "Update") -> bool:
        """Check if incoming chat type is enabled in Telegram adapter settings."""
        chat = getattr(update, "effective_chat", None)
        chat_type = getattr(chat, "type", "unknown")
        if chat_type == "private":
            return self.listen_private
        if chat_type in ("group", "supergroup"):
            return self.listen_groups
        return False

    def _chat_name(self, update: "Update") -> str:
        """Return best-effort chat title/identifier for logging."""
        chat = getattr(update, "effective_chat", None)
        title = getattr(chat, "title", None)
        if title:
            return str(title)
        username = getattr(chat, "username", None)
        if username:
            return f"@{username}"
        return str(getattr(chat, "id", "unknown"))

    def _log_dialog_event(
        self,
        *,
        update: "Update",
        incoming_text: str,
        response_text: str | None,
        response_sent: bool,
        error: str | None,
        event: str = "message",
    ) -> None:
        """Write one dialog record with incoming message and bot reaction to a dedicated log file."""
        chat = getattr(update, "effective_chat", None)
        user = getattr(update, "effective_user", None)
        message = getattr(update, "effective_message", None)
        payload = {
            "event": event,
            "chat_id": getattr(chat, "id", None),
            "chat_type": getattr(chat, "type", None),
            "chat_name": self._chat_name(update),
            "user_id": getattr(user, "id", None),
            "username": getattr(user, "username", None),
            "message_id": getattr(message, "message_id", None),
            "incoming_text": incoming_text,
            "response_text": response_text,
            "response_sent": response_sent,
            "error": error,
        }
        try:
            line = json.dumps(payload, ensure_ascii=False)
        except Exception:
            line = str(payload)
        log(self.dialog_log_type, "info", line)


    async def handle(self, user_id: str, agent_id: str, message: str, context: dict = None) -> dict:
        """
        Handle a message from a Telegram user and route to agent.
        Args:
            user_id: Telegram chat_id (unique per user/channel)
            agent_id: Target agent id (or default if empty)
            message: User message text
            context: Optional context (update, message_id, etc)
        Returns:
            dict with 'result' key containing agent response
        """
        if not self.enabled or not self.bot:
            log("adapters.telegram", "debug", f"adapter disabled or bot not ready")
            return {"error": "Telegram adapter not enabled"}
        
        context = context or {}
        agent_id = agent_id or self.default_agent
        chat_id = int(user_id)
        
        log("adapters.telegram", "info", f"received message from user {chat_id}: '{message[:50]}{'...' if len(message) > 50 else ''}'")
        log("adapters.telegram", "debug", f"routing to agent '{agent_id}'")

        # Submit task to orchestrator
        try:
            task_id = await self.orchestrator.submit(agent_id, message, context)
            log("adapters.telegram", "debug", f"task submitted: {task_id}")
        except Exception as e:
            log("adapters.telegram", "error", f"failed to submit task: {e}")
            return {"error": f"task submission failed: {e}"}

        # Show typing indicator while waiting for response
        await self.send_typing_action(chat_id)
        
        # Wait for completion (polling, since orchestrator is in-memory)
        for attempt in range(120):  # up to 60s
            task = self.orchestrator.get_task(task_id)
            if task and task.get("status") in ("done", "error"):
                result = task.get("result") or {"error": "no result"}
                status = "success" if not result.get("error") else "error"
                log("adapters.telegram", "info", f"task completed ({status}): {result.get('error') or result.get('result', 'ok')[:50]}")
                return result
            # Re-send typing indicator every 5 seconds
            if attempt > 0 and attempt % 10 == 0:
                await self.send_typing_action(chat_id)
            await asyncio.sleep(0.5)

        log("adapters.telegram", "error", f"task timeout after 60s: {task_id}")
        return {"error": "timeout"}

    async def send_typing_action(self, chat_id: int) -> bool:
        """
        Send typing indicator to show that bot is processing.
        Args:
            chat_id: Telegram chat_id
        Returns:
            True if sent successfully, False otherwise
        """
        if not self.enabled or not self.bot or not ChatAction:
            return False
        
        try:
            await self.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            log("adapters.telegram", "debug", f"typing indicator sent to chat {chat_id}")
            return True
        except Exception as e:
            log("adapters.telegram", "debug", f"failed to send typing indicator to chat {chat_id}: {e}")
            return False

    async def send_message(self, chat_id: int, text: str) -> bool:
        """
        Send a message via Telegram Bot API.
        Args:
            chat_id: Telegram chat_id
            text: Message text
        Returns:
            True if sent successfully, False otherwise
        """
        if not self.enabled or not self.bot:
            log("adapters.telegram", "debug", f"adapter disabled or bot not ready, cannot send message")
            return False
        
        try:
            truncated_text = text[:100] + ('...' if len(text) > 100 else '')
            log("adapters.telegram", "debug", f"sending message to chat {chat_id}: '{truncated_text}'")
            await self.bot.send_message(chat_id=chat_id, text=text)
            log("adapters.telegram", "info", f"message sent to chat {chat_id}")
            return True
        except Exception as e:
            log("adapters.telegram", "error", f"failed to send message to chat {chat_id}: {e}")
            return False

    async def start_polling(self) -> None:
        """
        Start polling for incoming Telegram messages.
        This is a blocking call; run in a separate task.
        """
        if not self.enabled or not Application:
            log("adapters.telegram", "warning", "Telegram adapter not enabled or dependencies missing")
            return
        if not self.polling:
            log("adapters.telegram", "warning", "Telegram adapter is configured without polling support")
            return
        if self._polling_active:
            log("adapters.telegram", "warning", "Telegram polling start requested while already active")
            return
        
        # Create Application and pass bot to it
        self.app = Application.builder().token(self.token).build()
        self._stop_event.clear()
        
        # Add handlers
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message))
        self.app.add_handler(CommandHandler("start", self._handle_start))
        if ChatMemberHandler is not None:
            self.app.add_handler(ChatMemberHandler(self._handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

        if self.listen_groups:
            log(
                "adapters.telegram",
                "notice",
                "Group listening is enabled. If full group messages are missing, disable Privacy Mode for this bot in BotFather.",
            )
        log(
            self.dialog_log_type,
            "notice",
            json.dumps(
                {
                    "event": "dialog_logger_started",
                    "default_agent": self.default_agent,
                    "listen_private": self.listen_private,
                    "listen_groups": self.listen_groups,
                },
                ensure_ascii=False,
            ),
        )
        
        log("adapters.telegram", "info", f"starting polling (timeout={self.polling_timeout}s, agent={self.default_agent})")
        try:
            await self.app.initialize()
            log("adapters.telegram", "debug", "Telegram application initialized")
            await self.app.start()
            log("adapters.telegram", "debug", "Telegram application started")
            if not self.app.updater:
                raise RuntimeError("Telegram updater is unavailable")
            await self.app.updater.start_polling(
                allowed_updates=Update.ALL_TYPES,
                timeout=self.polling_timeout,
            )
            self._polling_active = True
            log("adapters.telegram", "info", "polling started, listening for messages")
            await self._stop_event.wait()
            log("adapters.telegram", "notice", "polling stop requested")
        except Exception as e:
            log("adapters.telegram", "error", f"polling error: {e}\n{traceback.format_exc()}")
            raise
        finally:
            self._polling_active = False
            if self.app:
                try:
                    if self.app.updater and self.app.updater.running:
                        await self.app.updater.stop()
                        log("adapters.telegram", "debug", "Telegram updater stopped")
                except Exception as e:
                    log("adapters.telegram", "error", f"failed to stop updater: {e}\n{traceback.format_exc()}")
                try:
                    if self.app.running:
                        await self.app.stop()
                        log("adapters.telegram", "debug", "Telegram application stopped")
                except Exception as e:
                    log("adapters.telegram", "error", f"failed to stop application: {e}\n{traceback.format_exc()}")
                try:
                    await self.app.shutdown()
                    log("adapters.telegram", "debug", "Telegram application shutdown completed")
                except Exception as e:
                    log("adapters.telegram", "error", f"failed to shutdown application: {e}\n{traceback.format_exc()}")
            self.app = None

    async def stop(self) -> None:
        """Request Telegram polling shutdown and wait for current loop cleanup."""
        if not self.enabled:
            return
        log("adapters.telegram", "notice", "Telegram adapter stop requested")
        self._stop_event.set()

    async def _handle_message(self, update: "Update", context) -> None:
        """Internal handler for text messages."""
        if not update.message or not update.message.text:
            log("adapters.telegram", "debug", "received message with no text, ignoring")
            return
        if not self._is_chat_allowed(update):
            chat = getattr(update, "effective_chat", None)
            log(
                "adapters.telegram",
                "debug",
                f"ignoring message from chat {getattr(chat, 'id', '?')} type={getattr(chat, 'type', 'unknown')} by config",
            )
            return
        
        chat_id = update.effective_chat.id
        username = update.effective_user.username or f"user_{chat_id}"
        text = update.message.text
        
        log("adapters.telegram", "debug", f"message handler: user {username} ({chat_id}), text_len={len(text)}")
        
        try:
            # Route message through adapter
            result = await self.handle(str(chat_id), "", text, context={"update": update})
            
            # Send response back to Telegram
            response_text = result.get("result") or result.get("error") or "No response"
            if isinstance(response_text, dict):
                response_text = str(response_text)
            
            log("adapters.telegram", "debug", f"sending response to user {username}")
            sent = await self.send_message(chat_id, str(response_text))
            self._log_dialog_event(
                update=update,
                incoming_text=text,
                response_text=str(response_text),
                response_sent=sent,
                error=None if sent else "send_message_failed",
            )
        except Exception as e:
            log("adapters.telegram", "error", f"error handling message from user {username}: {e}\n{traceback.format_exc()}")
            self._log_dialog_event(
                update=update,
                incoming_text=text,
                response_text=None,
                response_sent=False,
                error=str(e),
            )
            try:
                await self.send_message(chat_id, f"Error: {str(e)[:100]}")
            except Exception as send_err:
                log("adapters.telegram", "error", f"failed to send error message: {send_err}\n{traceback.format_exc()}")

    async def _handle_start(self, update: "Update", context) -> None:
        """Internal handler for /start command."""
        if not self._is_chat_allowed(update):
            return
        chat_id = update.effective_chat.id
        username = update.effective_user.username or f"user_{chat_id}"
        log("adapters.telegram", "info", f"user {username} ({chat_id}) started the bot")
        start_reply = f"Hi! I'm an agent bot. Ask me anything! Default agent: {self.default_agent}"
        try:
            await update.message.reply_text(start_reply)
            self._log_dialog_event(
                update=update,
                incoming_text=update.message.text or "/start",
                response_text=start_reply,
                response_sent=True,
                error=None,
                event="command",
            )
        except Exception as e:
            log("adapters.telegram", "error", f"failed to send /start reply to user {username}: {e}\n{traceback.format_exc()}")
            self._log_dialog_event(
                update=update,
                incoming_text=update.message.text or "/start",
                response_text=None,
                response_sent=False,
                error=str(e),
                event="command",
            )

    async def _handle_my_chat_member(self, update: "Update", context) -> None:
        """Log when the bot is added/removed/promoted in chats (especially groups)."""
        if not update.my_chat_member:
            return
        chat = update.effective_chat
        old_status = update.my_chat_member.old_chat_member.status
        new_status = update.my_chat_member.new_chat_member.status
        log(
            "adapters.telegram",
            "notice",
            f"chat member update: chat={getattr(chat, 'id', '?')} type={getattr(chat, 'type', 'unknown')} title='{self._chat_name(update)}' status {old_status}->{new_status}",
        )
