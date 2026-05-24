"""
TelegramAdapter: Adapter for Telegram bot integration (unified interface).
Handles incoming messages from Telegram users and routes them to agents via orchestrator.
"""
import asyncio
from collections import defaultdict
import json
import re
import traceback
from typing import Any
from adapters.adapter import Adapter
from core.logging_utils import log
from memory.tools import MemoryTools

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
        super().__init__(config, adapter_id="telegram")
        self.orchestrator = orchestrator
        self._base_adapter_config = self.assemble_runtime_config(envid=None)
        self._apply_adapter_config(self._base_adapter_config)
        root_dir = str((config or {}).get("root") or "")
        self.memory_tools = MemoryTools(root_dir, config or {})
        
        self.app = None
        self.bot = None
        self._stop_event = asyncio.Event()
        self._polling_active = False
        self._recent_group_messages: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._bot_usernames: set[str] = set()
        
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

    def _apply_adapter_config(self, telegram_config: dict[str, Any]) -> None:
        """Apply assembled adapter config to runtime fields.

        Input: resolved adapter config section.
        Output: instance fields updated.
        """

        cfg = telegram_config if isinstance(telegram_config, dict) else {}
        self.enabled = bool(cfg.get("enabled", False))
        self.token = cfg.get("token")
        self.default_agent = str(cfg.get("default_agent", "chat_group_helper"))
        self.polling = bool(cfg.get("polling", True))
        self.polling_timeout = int(cfg.get("polling_timeout", 30))
        self.timeout_seconds = max(1, int(cfg.get("timeoutSeconds", 100)))
        self.listen_private = bool(cfg.get("listen_private", True))
        self.listen_groups = bool(cfg.get("listen_groups", True))
        self.show_typing = bool(cfg.get("show_typing", False))
        self.dialog_log_type = str(cfg.get("dialog_log_type", "telegram_dialog"))

    def _message_mentions_bot(self, update: "Update") -> bool:
        """Return true when the message explicitly mentions this bot username."""

        msg = getattr(update, "message", None)
        text = str(getattr(msg, "text", "") or "")
        if not text:
            return False
        lowered = text.lower()
        if any(name and f"@{name}" in lowered for name in self._bot_usernames):
            return True
        entities = getattr(msg, "entities", None) or []
        for ent in entities:
            ent_type = str(getattr(ent, "type", "")).lower()
            if ent_type != "mention":
                continue
            offset = int(getattr(ent, "offset", 0))
            length = int(getattr(ent, "length", 0))
            fragment = text[offset : offset + length].strip().lstrip("@").lower()
            if fragment and fragment in self._bot_usernames:
                return True
        return False

    def _is_direct_address(self, text: str) -> bool:
        """Heuristic direct-address detector for group chat messages."""

        clean = str(text or "").strip().lower()
        if not clean:
            return False
        base_names = {"helper", "assistant", "бот", "bot"}
        names = base_names | set(self._bot_usernames)
        for name in names:
            if not name:
                continue
            if clean.startswith(f"{name} ") or clean.startswith(f"{name},") or clean.startswith(f"{name}:"):
                return True
        return False

    def _remember_recent_group_message(self, update: "Update") -> None:
        """Cache small rolling group history for best-effort join ingest."""

        chat = getattr(update, "effective_chat", None)
        user = getattr(update, "effective_user", None)
        msg = getattr(update, "message", None)
        text = str(getattr(msg, "text", "") or "").strip()
        chat_id = str(getattr(chat, "id", "") or "").strip()
        if not chat_id or not text:
            return

        bucket = self._recent_group_messages[chat_id]
        bucket.append(
            {
                "role": "user",
                "message_id": getattr(msg, "message_id", None),
                "date": str(getattr(msg, "date", "") or ""),
                "user_id": getattr(user, "id", None),
                "display_name": self._display_name(user),
                "username": getattr(user, "username", None),
                "text": text,
            }
        )
        self._recent_group_messages[chat_id] = bucket[-80:]

    def _remember_recent_assistant_message(self, chat_id: int, text: str) -> None:
        """Cache bot outgoing message in recent history as assistant role."""

        clean_text = str(text or "").strip()
        if not clean_text:
            return
        bucket = self._recent_group_messages[str(chat_id)]
        username = sorted(self._bot_usernames)[0] if self._bot_usernames else ""
        display_name = f"@{username}" if username else "assistant"
        bucket.append(
            {
                "role": "assistant",
                "message_id": None,
                "date": "",
                "user_id": None,
                "display_name": display_name,
                "username": username or None,
                "text": clean_text,
            }
        )
        self._recent_group_messages[str(chat_id)] = bucket[-80:]

    @staticmethod
    def _display_name(user: Any) -> str:
        """Build compact user display name from Telegram user fields."""

        first = str(getattr(user, "first_name", "") or "").strip()
        last = str(getattr(user, "last_name", "") or "").strip()
        full = " ".join(part for part in (first, last) if part).strip()
        if full:
            return full
        username = str(getattr(user, "username", "") or "").strip()
        if username:
            return f"@{username}"
        user_id = getattr(user, "id", None)
        return f"user_{user_id}" if user_id is not None else "unknown"

    def _resolve_envid_for_chat(self, chat: Any) -> str | None:
        """Resolve envid for one chat descriptor with Telegram matching fields."""

        event_ctx = {
            "chat_id": getattr(chat, "id", None),
            "chat_username": getattr(chat, "username", ""),
            "chat_type": getattr(chat, "type", ""),
        }
        try:
            return self.resolve_envid(event_ctx, explicit_envid=None)
        except Exception as e:
            log("adapters.telegram", "warning", f"join ingest envid resolution failed: {e}")
            return None

    def _safe_corpus_id(self, chat_id: str) -> str:
        """Build corpus id safe for storage using chat id as source."""

        clean = "".join(ch for ch in str(chat_id) if ch.isalnum() or ch in ("-", "_"))
        return f"telegram_group_{clean or 'unknown'}"

    def _ingest_group_history_on_join(self, update: "Update") -> None:
        """Best-effort initial join ingest into episodic memory and RAG queue."""

        chat = getattr(update, "effective_chat", None)
        user = getattr(update, "effective_user", None)
        status = getattr(getattr(update, "my_chat_member", None), "new_chat_member", None)
        chat_id = str(getattr(chat, "id", "") or "").strip()
        if not chat_id:
            return
        envid = self._resolve_envid_for_chat(chat)
        recent = self._recent_group_messages.get(chat_id, [])[-30:]
        message = getattr(update, "effective_message", None)
        message_id = str(getattr(message, "message_id", "") or "")
        status_name = str(getattr(status, "status", "") or "")
        dedup_key = f"initial_join:{chat_id}:{message_id or status_name}"

        profile_id = f"join_import:{chat_id}"
        try:
            existing = self.memory_tools.get_profile_memory(
                agent_id="chat_group_helper",
                profile_id=profile_id,
                limit=40,
                envid=envid,
            )
            if any(dedup_key in str(item.get("text", "")) for item in existing):
                return
        except Exception:
            existing = []

        lines = [
            f"join_dedup_key={dedup_key}",
            f"adapter=telegram chat_id={chat_id} import_type=initial_join chat_type={getattr(chat, 'type', '')}",
            f"chat_title={self._chat_name(update)}",
            f"added_by_user_id={getattr(user, 'id', None)} added_by_username={getattr(user, 'username', None)}",
            "note=telegram_api_has_no_full_history_endpoint_for_bots; using available recent seen messages",
        ]
        if recent:
            lines.append("recent_messages:")
            for item in recent:
                lines.append(
                    f"- id={item.get('message_id')} ts={item.get('date')} user={item.get('user_id')} @{item.get('username')}: {str(item.get('text', ''))[:240]}"
                )
        ingest_text = "\n".join(lines)

        try:
            self.memory_tools.record_episode(
                agent_id="chat_group_helper",
                text=ingest_text,
                task_id=None,
                outcome="initial_join_import",
                envid=envid,
            )
            self.memory_tools.remember_profile_fact(
                agent_id="chat_group_helper",
                profile_id=profile_id,
                text=f"{dedup_key} source=telegram chat_id={chat_id}",
                scope="join_import",
                importance=0.7,
                envid=envid,
            )
            if len(ingest_text) >= 800:
                self.memory_tools.ingest_document(
                    source={
                        "type": "text",
                        "text": ingest_text,
                        "metadata": {
                            "adapter": "telegram",
                            "chat_id": chat_id,
                            "import_type": "initial_join",
                            "dedup_key": dedup_key,
                        },
                    },
                    corpus_id=self._safe_corpus_id(chat_id),
                    title=f"telegram join import {chat_id}",
                    tags=["telegram", "group", "initial_join", chat_id],
                )
        except Exception as e:
            log("adapters.telegram", "warning", f"join ingest failed for chat={chat_id}: {e}")

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

    @staticmethod
    def _extract_reply_marker(text: str) -> tuple[str, int | None]:
        """Extract __REPLY__:<message_id> marker from model text.

        Input: raw model output text.
        Output: cleaned text and optional reply target message id.
        """

        raw = str(text or "")
        match = re.search(r"__REPLY__\s*:\s*(\d+)", raw)
        if not match:
            return raw, None
        reply_to = int(match.group(1))
        cleaned = (raw[: match.start()] + raw[match.end() :]).strip()
        return cleaned, reply_to


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
        context = context or {}
        event_ctx = {
            "chat_id": context.get("chat_id", user_id),
            "chat_username": context.get("chat_username", ""),
            "chat_type": context.get("chat_type", ""),
        }
        explicit_envid = str(context.get("envid", "")).strip() or None
        try:
            envid = self.resolve_envid(event_ctx, explicit_envid=explicit_envid)
        except Exception as e:
            log("adapters.telegram", "warning", f"envid resolution failed: {e}")
            return {"error": f"envid resolution failed: {e}"}

        runtime_cfg = self.assemble_runtime_config(envid=envid)
        runtime_enabled = bool(runtime_cfg.get("enabled", self.enabled))
        default_agent = str(runtime_cfg.get("default_agent", self.default_agent) or self.default_agent)
        timeout_seconds = max(1, int(runtime_cfg.get("timeoutSeconds", self.timeout_seconds)))

        if not runtime_enabled or not self.bot:
            log("adapters.telegram", "debug", "adapter disabled or bot not ready")
            return {"error": "Telegram adapter not enabled"}

        agent_id = agent_id or default_agent
        chat_id = int(user_id)
        context = {**context, "envid": envid} if envid else dict(context)
        
        log(
            "adapters.telegram",
            "info",
            f"received message from user {chat_id} envid={envid or '-'}: '{message[:50]}{'...' if len(message) > 50 else ''}'",
        )
        log("adapters.telegram", "debug", f"routing to agent '{agent_id}' envid={envid or '-'}")

        # Submit task to orchestrator
        try:
            task_id = await self.orchestrator.submit(agent_id, message, context)
            log("adapters.telegram", "debug", f"task submitted: {task_id}")
        except Exception as e:
            log("adapters.telegram", "error", f"failed to submit task: {e}")
            return {"error": f"task submission failed: {e}"}

        # Show typing indicator while waiting for response
        if self.show_typing:
            await self.send_typing_action(chat_id)
        
        # Wait for completion (polling, since orchestrator is in-memory)
        poll_interval = 0.5
        max_attempts = max(1, int(timeout_seconds / poll_interval))
        for attempt in range(max_attempts):
            task = self.orchestrator.get_task(task_id)
            if task and task.get("status") in ("done", "error"):
                result = task.get("result") or {"error": "no result"}
                status = "success" if not result.get("error") else "error"
                log("adapters.telegram", "info", f"task completed ({status}): {result.get('error') or result.get('result', 'ok')[:50]}")
                return result
            # Re-send typing indicator every 5 seconds
            if attempt > 0 and attempt % int(5 / poll_interval) == 0:
                if self.show_typing:
                    await self.send_typing_action(chat_id)
            await asyncio.sleep(poll_interval)

        log("adapters.telegram", "warning", f"task timeout after {timeout_seconds}s: {task_id}")
        return {"skip_send": True, "skip_reason": "timeout"}

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

    async def send_message(self, chat_id: int, text: str, reply_to_message_id: int | None = None) -> bool:
        """
        Send a message via Telegram Bot API.
        Args:
            chat_id: Telegram chat_id
            text: Message text
            reply_to_message_id: Optional Telegram message id to send as reply
        Returns:
            True if sent successfully, False otherwise
        """
        if not self.enabled or not self.bot:
            log("adapters.telegram", "debug", f"adapter disabled or bot not ready, cannot send message")
            return False
        
        try:
            truncated_text = text[:100] + ('...' if len(text) > 100 else '')
            log("adapters.telegram", "debug", f"sending message to chat {chat_id}: '{truncated_text}'")
            kwargs: dict[str, Any] = {"chat_id": chat_id, "text": text}
            if reply_to_message_id is not None:
                kwargs["reply_to_message_id"] = int(reply_to_message_id)
            await self.bot.send_message(**kwargs)
            log("adapters.telegram", "info", f"message sent to chat {chat_id}")
            return True
        except Exception as e:
            err_text = str(e)
            # Fallback: if reply target no longer exists, send same message without reply target.
            if reply_to_message_id is not None and "Message to be replied not found" in err_text:
                log(
                    "adapters.telegram",
                    "warning",
                    f"reply target not found for chat {chat_id} reply_to={reply_to_message_id}; retrying without reply_to_message_id",
                )
                try:
                    await self.bot.send_message(chat_id=chat_id, text=text)
                    log("adapters.telegram", "info", f"message sent to chat {chat_id} (fallback without reply target)")
                    return True
                except Exception as retry_e:
                    log("adapters.telegram", "error", f"fallback send failed for chat {chat_id}: {retry_e}")
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
            try:
                me = await self.bot.get_me()
                username = str(getattr(me, "username", "") or "").strip().lower()
                if username:
                    self._bot_usernames.add(username)
            except Exception as e:
                log("adapters.telegram", "warning", f"cannot resolve bot username: {e}")
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
        mentioned = self._message_mentions_bot(update)
        direct_address = self._is_direct_address(text)
        self._remember_recent_group_message(update)
        
        log("adapters.telegram", "debug", f"message handler: user {username} ({chat_id}), text_len={len(text)}")
        
        try:
            # Route message through adapter
            recent_messages = list(self._recent_group_messages.get(str(chat_id), []))
            result = await self.handle(
                str(chat_id),
                "",
                text,
                context={
                    "adapter": "telegram",
                    "chat_id": chat_id,
                    "chat_type": getattr(update.effective_chat, "type", ""),
                    "chat_username": getattr(update.effective_chat, "username", ""),
                    "message_id": getattr(update.message, "message_id", None),
                    "user_id": getattr(update.effective_user, "id", None),
                    "username": getattr(update.effective_user, "username", ""),
                    "display_name": self._display_name(update.effective_user),
                    "mentioned": mentioned,
                    "direct_address": direct_address,
                    "recent_messages": recent_messages,
                },
            )

            if bool(result.get("skip_send", False)):
                self._log_dialog_event(
                    update=update,
                    incoming_text=text,
                    response_text=None,
                    response_sent=False,
                    error=str(result.get("skip_reason", "ignored_by_model_decision")),
                )
                return
            
            # Send response back to Telegram
            response_text = result.get("result") or result.get("error") or "No response"
            if isinstance(response_text, dict):
                response_text = str(response_text)
            response_text, reply_to_message_id = self._extract_reply_marker(str(response_text))
            if not str(response_text).strip():
                response_text = "No response"
            
            log("adapters.telegram", "debug", f"sending response to user {username}")
            sent = await self.send_message(chat_id, str(response_text), reply_to_message_id=reply_to_message_id)
            if sent:
                self._remember_recent_assistant_message(chat_id, str(response_text))
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
        joined_statuses = {"member", "administrator"}
        non_member_statuses = {"left", "kicked"}
        if new_status in joined_statuses and old_status in non_member_statuses:
            self._ingest_group_history_on_join(update)
