"""AgentBase: abstract chain-based agent on top of LangChain."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class AgentBase(ABC):
    """Base class for agents that execute a LangChain runnable chain."""

    def __init__(self, app_config: dict[str, Any], agent_config: dict[str, Any] | None = None):
        self.app_config = app_config or {}
        self.agent_config = agent_config or {}
        self.name = self.agent_config.get("name", self.__class__.__name__.lower())
        self.chain = self.build_chain()

    @abstractmethod
    def build_chain(self):
        """Build and return LangChain runnable chain."""

    async def handle(self, query: str, context=None) -> dict:
        """Execute LangChain chain and return unified result payload."""
        payload = {"query": query, "context": context or {}}
        out = await self.chain.ainvoke(payload)
        content = getattr(out, "content", out)
        return {"result": content}
