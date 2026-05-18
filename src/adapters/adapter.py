"""
Base Adapter class for all input/output and integration adapters.
All adapters must inherit from this class and implement the required interface.
"""
from abc import ABC, abstractmethod
from typing import Any

class Adapter(ABC):
    """Base class for all adapters (webui, telegram, cron, etc)."""

    def __init__(self, config: dict[str, Any] = None):
        self.config = config or {}
        self.name = self.config.get("name", self.__class__.__name__.lower())

    @abstractmethod
    async def handle(self, user_id: str, agent_id: str, message: str, context: dict = None) -> dict:
        """
        Handle a message from a user to an agent.
        Args:
            user_id: Unique user/channel/session identifier (webui: login, telegram: chat_id, etc)
            agent_id: Target agent id
            message: User message
            context: Optional context dict
        Returns:
            dict with at least 'result' key (agent response)
        """
        pass
