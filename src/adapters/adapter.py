"""
Base Adapter class for all input/output and integration adapters.
All adapters must inherit from this class and implement the required interface.
"""
from abc import ABC, abstractmethod
from typing import Any

from core.envid_runtime import assemble_component_config, resolve_envid

class Adapter(ABC):
    """Base class for all adapters (webui, telegram, cron, etc)."""

    def __init__(self, config: dict[str, Any] = None, adapter_id: str | None = None):
        self.config = config or {}
        self.adapter_id = str(adapter_id or self.config.get("name") or self.__class__.__name__.lower()).strip()
        self.name = self.adapter_id

    def resolve_envid(self, event_context: dict[str, Any] | None = None, explicit_envid: str | None = None) -> str | None:
        """Resolve envid for one adapter event.

        Input: event context and optional explicit envid.
        Output: resolved envid or None.
        """

        return resolve_envid(self.config, self.adapter_id, event_context=event_context, explicit_envid=explicit_envid)

    def assemble_runtime_config(self, envid: str | None = None) -> dict[str, Any]:
        """Assemble final adapter config via shared core overlay logic.

        Input: optional resolved envid.
        Output: final adapter config section.
        """

        return assemble_component_config(
            component_type="adapter",
            component_id=self.adapter_id,
            envid=envid,
            root_config=self.config,
            local_config=None,
        )

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
