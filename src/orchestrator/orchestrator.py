"""AgentOrchestrator: LangGraph-based routing, retries and task tracking."""
from __future__ import annotations

import asyncio
import traceback
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, TypedDict

from langgraph.graph import END, StateGraph

from agents.echo.agent import EchoAgent
from agents.math.agent import MathAgent
from agents.graph_echo.agent import GraphEchoAgent
from core.logging_utils import log


class OrchestratorState(TypedDict, total=False):
    """State object passed through LangGraph nodes."""

    task_id: str
    agent: str
    query: str
    context: dict[str, Any]
    status: str
    result: Any
    error: str
    retries: int
    max_retries: int
    fallback_agent: str


class AgentOrchestrator:
    """Routes tasks to agents through a LangGraph workflow."""

    def __init__(self, config=None):
        self.config = config or {}
        self.agents = {}
        self.tasks: Dict[str, dict] = {}  # id -> {status, agent, query, result}
        self._init_agents()
        self.graph = self._build_graph()

    def _init_agents(self):
        """Initialize built-in agents and apply optional config overrides."""
        items = self.config.get("agents", {}).get("items", {})

        echo_cfg = items.get("echo", {}) if isinstance(items, dict) else {}
        math_cfg = items.get("math", {}) if isinstance(items, dict) else {}
        graph_echo_cfg = items.get("graph_echo", {}) if isinstance(items, dict) else {}

        if not echo_cfg.get("enabled", True):
            pass
        else:
            self.agents["echo"] = EchoAgent(self.config, {"name": "echo", **echo_cfg})

        if not math_cfg.get("enabled", True):
            pass
        else:
            self.agents["math"] = MathAgent(self.config, {"name": "math", **math_cfg})

        if not graph_echo_cfg.get("enabled", True):
            pass
        else:
            self.agents["graph_echo"] = GraphEchoAgent(
                self.config,
                {"name": "graph_echo", **graph_echo_cfg},
            )

    def _build_graph(self):
        """Build LangGraph workflow for routing, execution and retry/fallback."""
        graph = StateGraph(OrchestratorState)
        graph.add_node("route", self._node_route)
        graph.add_node("execute", self._node_execute)
        graph.add_node("retry_delay", self._node_retry_delay)
        graph.add_node("fallback", self._node_fallback)

        graph.set_entry_point("route")
        graph.add_conditional_edges(
            "route",
            self._after_route,
            {"execute": "execute", "error": END},
        )
        graph.add_conditional_edges(
            "execute",
            self._after_execute,
            {"done": END, "retry": "retry_delay", "fallback": "fallback", "error": END},
        )
        graph.add_edge("retry_delay", "execute")
        graph.add_conditional_edges(
            "fallback",
            self._after_fallback,
            {"done": END, "error": END},
        )
        return graph.compile()

    def _task_cfg(self) -> dict[str, Any]:
        """Return orchestrator options from config."""
        return self.config.get("agents", {}).get("orchestrator", {})

    def _utcnow_iso(self) -> str:
        """Return current UTC time in ISO format."""
        return datetime.now(timezone.utc).isoformat()

    def _set_task(self, task_id: str, **kwargs: Any) -> None:
        """Update task fields in shared task storage."""
        if task_id in self.tasks:
            self.tasks[task_id].update(kwargs)
            self.tasks[task_id]["updated_at"] = self._utcnow_iso()

    async def _node_route(self, state: OrchestratorState) -> OrchestratorState:
        """Validate requested agent and mark task as running."""
        task_id = state["task_id"]
        agent_name = state.get("agent", "")
        if agent_name not in self.agents:
            err = f"Agent '{agent_name}' not found"
            self._set_task(task_id, status="error", result={"error": err})
            return {"status": "error", "error": err}

        self._set_task(task_id, status="running")
        return {"status": "running"}

    def _after_route(self, state: OrchestratorState) -> str:
        """Choose next edge after route validation."""
        return "error" if state.get("status") == "error" else "execute"

    async def _node_execute(self, state: OrchestratorState) -> OrchestratorState:
        """Run selected agent once and capture result or exception."""
        task_id = state["task_id"]
        agent_name = state["agent"]
        query = state["query"]
        context = state.get("context", {})

        try:
            result = await self.agents[agent_name].handle(query, context)
            if isinstance(result, dict) and result.get("error"):
                raise RuntimeError(str(result.get("error")))
            self._set_task(task_id, status="done", result=result)
            return {"status": "done", "result": result, "error": ""}
        except Exception as e:
            retries = int(state.get("retries", 0))
            max_retries = int(state.get("max_retries", 1))
            fallback_agent = str(state.get("fallback_agent", "")).strip()
            log(
                "core",
                "error",
                f"Task {task_id} failed in agent={agent_name} on retry={retries}: {e}\n{traceback.format_exc()}",
                tag="orchestrator",
            )
            self._set_task(task_id, status="retrying", result={"error": str(e), "retries": retries})

            if retries < max_retries:
                return {"status": "retry", "error": str(e)}
            if fallback_agent and fallback_agent in self.agents and fallback_agent != agent_name:
                return {"status": "fallback", "error": str(e)}

            self._set_task(task_id, status="error", result={"error": str(e)})
            return {"status": "error", "error": str(e)}

    def _after_execute(self, state: OrchestratorState) -> str:
        """Choose edge after agent execution."""
        status = state.get("status", "error")
        if status in ("done", "retry", "fallback", "error"):
            return status
        return "error"

    async def _node_retry_delay(self, state: OrchestratorState) -> OrchestratorState:
        """Wait before retry and increment retry counter."""
        retries = int(state.get("retries", 0)) + 1
        delay = float(self._task_cfg().get("retry_delay_sec", 0.5))
        task_id = state["task_id"]
        self._set_task(task_id, status="retrying", result={"retries": retries})
        if delay > 0:
            await asyncio.sleep(delay)
        return {"retries": retries}

    async def _node_fallback(self, state: OrchestratorState) -> OrchestratorState:
        """Run fallback agent if the primary agent failed permanently."""
        task_id = state["task_id"]
        fallback_agent = str(state.get("fallback_agent", "")).strip()
        if not fallback_agent or fallback_agent not in self.agents:
            err = "Fallback agent is not configured"
            self._set_task(task_id, status="error", result={"error": err})
            return {"status": "error", "error": err}

        query = state["query"]
        context = state.get("context", {})
        self._set_task(task_id, status="running", result={"fallback": fallback_agent})
        try:
            result = await self.agents[fallback_agent].handle(query, context)
            self._set_task(task_id, status="done", result={"via_fallback": fallback_agent, **result})
            return {"status": "done", "result": {"via_fallback": fallback_agent, **result}}
        except Exception as e:
            log(
                "core",
                "error",
                f"Task {task_id} fallback agent={fallback_agent} failed: {e}\n{traceback.format_exc()}",
                tag="orchestrator",
            )
            self._set_task(task_id, status="error", result={"error": str(e)})
            return {"status": "error", "error": str(e)}

    def _after_fallback(self, state: OrchestratorState) -> str:
        """Choose final edge after fallback execution."""
        return "done" if state.get("status") == "done" else "error"

    def list_agents(self):
        return list(self.agents.keys())

    def get_agent_info(self, agent_name: str, limit: int = 20) -> dict[str, Any]:
        """Return runtime metadata and recent task history for one agent."""
        if agent_name not in self.agents:
            return {}

        safe_limit = max(1, min(100, int(limit)))
        agent = self.agents[agent_name]
        tasks = []
        stats = {"pending": 0, "running": 0, "retrying": 0, "done": 0, "error": 0, "total": 0}

        for task_id, task in self.tasks.items():
            if task.get("agent") != agent_name:
                continue
            status = str(task.get("status", "pending"))
            stats["total"] += 1
            if status in stats:
                stats[status] += 1

            tasks.append(
                {
                    "task_id": task_id,
                    "status": status,
                    "query": task.get("query", ""),
                    "result": task.get("result"),
                    "created_at": task.get("created_at", ""),
                    "updated_at": task.get("updated_at", ""),
                }
            )

        tasks.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return {
            "agent": {
                "id": agent_name,
                "type": agent.__class__.__name__,
                "module": agent.__class__.__module__,
            },
            "stats": stats,
            "recent_tasks": tasks[:safe_limit],
        }

    def get_task(self, task_id: str) -> dict:
        return self.tasks.get(task_id, {})

    async def submit(self, agent_name: str, query: str, context=None) -> str:
        """Create a new task and schedule LangGraph execution."""
        if agent_name not in self.agents:
            raise ValueError(f"Agent '{agent_name}' not found")
        task_id = str(uuid.uuid4())
        now = self._utcnow_iso()
        self.tasks[task_id] = {
            "status": "pending",
            "agent": agent_name,
            "query": query,
            "result": None,
            "retries": 0,
            "created_at": now,
            "updated_at": now,
        }
        asyncio.create_task(self._run_task(task_id, agent_name, query, context))
        return task_id

    async def _run_task(self, task_id, agent_name, query, context):
        """Execute full LangGraph flow and write terminal task state."""
        orch_cfg = self._task_cfg()
        initial: OrchestratorState = {
            "task_id": task_id,
            "agent": agent_name,
            "query": query,
            "context": context or {},
            "status": "pending",
            "retries": 0,
            "max_retries": int(orch_cfg.get("max_retries", 1)),
            "fallback_agent": str(orch_cfg.get("fallback_agent", "echo")),
        }
        try:
            log(
                "core",
                "info",
                f"Starting task {task_id} for agent={agent_name}",
                tag="orchestrator",
            )
            final_state = await self.graph.ainvoke(initial)
            if final_state.get("status") == "done":
                self._set_task(
                    task_id,
                    status="done",
                    result=final_state.get("result"),
                    retries=int(final_state.get("retries", self.tasks[task_id].get("retries", 0))),
                )
            elif final_state.get("status") == "error":
                self._set_task(
                    task_id,
                    status="error",
                    result={"error": str(final_state.get("error", "unknown error"))},
                    retries=int(final_state.get("retries", self.tasks[task_id].get("retries", 0))),
                )
        except Exception as e:
            log(
                "core",
                "error",
                f"Task runner crashed for task {task_id} agent={agent_name}: {e}\n{traceback.format_exc()}",
                tag="orchestrator",
            )
            self._set_task(task_id, status="error", result={"error": str(e)})
