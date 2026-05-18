"""
CronAdapter: Adapter for scheduled task execution (unified interface).
Handles periodic tasks and runs agents on a schedule.
"""
from typing import Any
from adapters.adapter import Adapter


class CronAdapter(Adapter):
    """Adapter for handling scheduled task execution via cron."""

    def __init__(self, orchestrator, config: dict[str, Any] = None):
        """
        Initialize CronAdapter.
        Args:
            orchestrator: AgentOrchestrator instance
            config: Configuration dict with cron schedules and tasks
        """
        super().__init__(config)
        self.orchestrator = orchestrator
        self.schedules = self.config.get("cron", {}).get("schedules", [])
        self.tasks = self.config.get("cron", {}).get("tasks", [])

    async def handle(self, user_id: str, agent_id: str, message: str, context: dict = None) -> dict:
        """
        Handle a scheduled task execution.
        Args:
            user_id: Task scheduler identifier (e.g., "cron_system")
            agent_id: Target agent id
            message: Task description or parameters
            context: Optional context (schedule_name, trigger_time, etc)
        Returns:
            dict with 'result' key and execution details
        """
        context = context or {}
        trigger_time = context.get("trigger_time")
        schedule_name = context.get("schedule_name", f"cron_{agent_id}")

        # Submit task to orchestrator
        task_id = await self.orchestrator.submit(agent_id, message, context)

        # Wait for completion (polling, since orchestrator is in-memory)
        import asyncio
        for _ in range(120):  # up to 60s
            task = self.orchestrator.get_task(task_id)
            if task and task.get("status") in ("done", "error"):
                result = task.get("result") or {"error": "no result"}
                # Log cron execution
                self._log_cron_execution(schedule_name, agent_id, result, trigger_time)
                return result
            await asyncio.sleep(0.5)

        error_result = {"error": "timeout"}
        self._log_cron_execution(schedule_name, agent_id, error_result, trigger_time)
        return error_result

    async def execute_schedules(self) -> list:
        """
        Execute all scheduled tasks that are due.
        Returns:
            list of execution results
        """
        results = []
        for task_config in self.tasks:
            agent_id = task_config.get("agent")
            schedule = task_config.get("schedule")
            message = task_config.get("message", "")
            enabled = task_config.get("enabled", True)

            if not enabled or not agent_id or not schedule:
                continue

            # Check if task is due (simplified: in production, use schedule parsing)
            # TODO: Implement proper cron schedule parsing (e.g., APScheduler)

            context = {
                "schedule_name": task_config.get("name", f"task_{agent_id}"),
                "trigger_time": None,  # Will be set by cron.py
            }

            result = await self.handle("cron_system", agent_id, message, context)
            results.append(result)

        return results

    def _log_cron_execution(self, schedule_name: str, agent_id: str, result: dict, trigger_time: Any = None) -> None:
        """Log cron task execution details."""
        from core.logging_utils import log
        status = "success" if result.get("error") is None else "error"
        log(
            "cron",
            "info",
            f"{schedule_name} ({agent_id}) {status}: {result.get('error') or result.get('result', 'ok')}"
        )
