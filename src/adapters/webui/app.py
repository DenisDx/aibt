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
        Route message to agent via orchestrator. Returns agent response dict.
        """
        # Optionally: context can include session, webui info, etc.
        task_id = await self.orchestrator.submit(agent_id, message, context)
        # Wait for completion (polling, since orchestrator is in-memory)
        import asyncio
        for _ in range(120):  # up to 60s
            task = self.orchestrator.get_task(task_id)
            if task and task.get("status") in ("done", "error"):
                return task.get("result") or {"error": "no result"}
            await asyncio.sleep(0.5)
        return {"error": "timeout"}
