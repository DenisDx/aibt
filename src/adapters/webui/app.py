"""
WebUIAdapter: Adapter for WebUI agent dialogue (unified interface).
"""
from typing import Any
from adapters.adapter import Adapter

class WebUIAdapter(Adapter):
    """Adapter for handling WebUI agent dialogue requests."""

    def __init__(self, orchestrator, config: dict[str, Any] = None):
        super().__init__(config)
        self.orchestrator = orchestrator

    async def handle(self, user_id: str, agent_id: str, message: str, context: dict = None) -> dict:
        """
        Route message to agent via orchestrator. Returns submitted task metadata.
        """
        task_id = await self.orchestrator.submit(agent_id, message, context)
        return {"task_id": task_id, "agent": agent_id, "submitted_by": user_id}
