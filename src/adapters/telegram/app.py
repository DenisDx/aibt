"""
TelegramAdapter: Adapter for Telegram bot integration (unified interface).
Handles incoming messages from Telegram users and routes them to agents via orchestrator.
"""
import asyncio
from typing import Any
from adapters.adapter import Adapter
from core.logging_utils import log

try:
    from telegram import Bot, Update
    from telegram.constants import ChatAction
    from telegram.ext import Application, CommandHandler, MessageHandler, filters
except ImportError:
    log("adapters.telegram", "warning", "python-telegram-bot not installed; TelegramAdapter disabled")
    Bot = None
    Update = None
    Application = None
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
        
        self.app = None
        self.bot = None
        
        if self.enabled and self.token and Bot:
            self.bot = Bot(token=self.token)
            log("adapters.telegram", "info", f"TelegramAdapter initialized (token={'***' + self.token[-4:]}, polling={self.polling}, timeout={self.polling_timeout}s)")
        elif self.enabled:
            log("adapters.telegram", "error", "TelegramAdapter enabled but no token or python-telegram-bot not installed")


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
        
        # Create Application and pass bot to it
        self.app = Application.builder().token(self.token).build()
        
        # Add handlers
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message))
        self.app.add_handler(CommandHandler("start", self._handle_start))
        
        log("adapters.telegram", "info", f"starting polling (timeout={self.polling_timeout}s)")
        try:
            async with self.app:
                await self.app.start()
                log("adapters.telegram", "info", "polling started, listening for messages")
                await self.app.updater.start_polling(allowed_updates=Update.ALL_TYPES, read_timeout=self.polling_timeout)
                await self.app.updater.stop()
                await self.app.stop()
        except Exception as e:
            log("adapters.telegram", "error", f"polling error: {e}")

    async def _handle_message(self, update: "Update", context) -> None:
        """Internal handler for text messages."""
        if not update.message or not update.message.text:
            log("adapters.telegram", "debug", "received message with no text, ignoring")
            return
        
        chat_id = update.effective_chat.id
        username = update.effective_user.username or f"user_{chat_id}"
        text = update.message.text
        
        log("adapters.telegram", "debug", f"message handler: user {username} ({chat_id})")
        
        try:
            # Route message through adapter
            result = await self.handle(str(chat_id), "", text, context={"update": update})
            
            # Send response back to Telegram
            response_text = result.get("result") or result.get("error") or "No response"
            if isinstance(response_text, dict):
                response_text = str(response_text)
            
            log("adapters.telegram", "debug", f"sending response to user {username}")
            await self.send_message(chat_id, str(response_text))
        except Exception as e:
            log("adapters.telegram", "error", f"error handling message from user {username}: {e}")
            try:
                await self.send_message(chat_id, f"Error: {str(e)[:100]}")
            except Exception as send_err:
                log("adapters.telegram", "error", f"failed to send error message: {send_err}")

    async def _handle_start(self, update: "Update", context) -> None:
        """Internal handler for /start command."""
        chat_id = update.effective_chat.id
        username = update.effective_user.username or f"user_{chat_id}"
        log("adapters.telegram", "info", f"user {username} ({chat_id}) started the bot")
        try:
            await update.message.reply_text(f"Hi! I'm an agent bot. Ask me anything! Default agent: {self.default_agent}")
        except Exception as e:
            log("adapters.telegram", "error", f"failed to send /start reply to user {username}: {e}")
