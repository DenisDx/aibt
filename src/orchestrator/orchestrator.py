"""AgentOrchestrator: LangGraph-based routing, retries and task tracking."""
from __future__ import annotations

import asyncio
import importlib
import inspect
import os
import traceback
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, TypedDict, Type

from langgraph.graph import END, StateGraph

from agents.base import AgentBase
from adapters.adapter import Adapter
from core.envid_runtime import assemble_component_config, load_environment_registry
from core.logging_utils import log
from memory.langgraph_runtime import build_langgraph_runtime


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
        self.root_dir = str(self.config.get("root") or self._default_root_dir())
        self.agent_classes: Dict[str, Type[Any]] = self._discover_agent_classes()
        self.adapter_classes: Dict[str, Type[Adapter]] = self._discover_adapter_classes()
        self.agents: Dict[str, Any] = {}
        self.adapters: Dict[str, Adapter] = {}
        self._agents_by_envid: Dict[str | None, Dict[str, Any]] = {}
        self.tasks: Dict[str, dict] = {}  # id -> {status, agent, query, result}
        self.checkpointer, self.store = build_langgraph_runtime(self.root_dir, self.config, "orchestrator")
        self._init_agents()
        self._init_adapters()
        self.graph = self._build_graph()

    @staticmethod
    def _default_root_dir() -> str:
        """Resolve project root when root is not present in runtime config."""

        return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    def _discover_agent_classes(self) -> Dict[str, Type[Any]]:
        """Discover agent classes from src/agents/*/agent.py modules."""

        return self._discover_component_classes(
            component_dir="agents",
            module_file="agent",
            class_suffix="Agent",
            component_label="agent",
            base_class=AgentBase,
        )

    def _discover_adapter_classes(self) -> Dict[str, Type[Adapter]]:
        """Discover adapter classes from src/adapters/*/app.py modules."""

        classes = self._discover_component_classes(
            component_dir="adapters",
            module_file="app",
            class_suffix="Adapter",
            component_label="adapter",
            base_class=Adapter,
        )
        return {k: v for k, v in classes.items() if k != "adapter"}

    def _discover_component_classes(
        self,
        component_dir: str,
        module_file: str,
        class_suffix: str,
        component_label: str,
        base_class: type,
    ) -> Dict[str, Type[Any]]:
        """Discover component classes from src/<component_dir>/*/<module_file>.py."""

        discovered: Dict[str, Type[Any]] = {}
        root = os.path.join(self.root_dir, "src", component_dir)
        if not os.path.isdir(root):
            log("core", "error", f"{component_label} discovery skipped: missing directory {root}", tag="orchestrator")
            return discovered

        for entry in sorted(os.listdir(root)):
            if entry.startswith("_"):
                continue
            entry_path = os.path.join(root, entry)
            module_path = os.path.join(entry_path, f"{module_file}.py")
            if not os.path.isdir(entry_path):
                continue
            if not os.path.isfile(module_path):
                log(
                    "core",
                    "error",
                    f"{component_label.title()} directory '{entry_path}' is missing required file '{module_file}.py'",
                    tag="orchestrator",
                )
                continue

            module_name = f"{component_dir}.{entry}.{module_file}"
            try:
                module = importlib.import_module(module_name)
            except Exception as e:
                log(
                    "core",
                    "error",
                    f"Failed to import {component_label} module {module_name}: {e}\n{traceback.format_exc()}",
                    tag="orchestrator",
                )
                continue

            cls = self._pick_component_class(module, class_suffix, base_class)
            if cls is None:
                log(
                    "core",
                    "error",
                    f"No valid {component_label} class found in module {module_name}",
                    tag="orchestrator",
                )
                continue

            discovered[entry] = cls

        log("core", "info", f"Discovered {len(discovered)} {component_label}(s): {', '.join(discovered.keys()) or '-'}", tag="orchestrator")
        return discovered

    @staticmethod
    def _pick_component_class(module: Any, suffix: str, base_class: type) -> Type[Any] | None:
        """Pick best component class from module members."""

        candidates: list[type] = []
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if obj.__module__ != module.__name__:
                continue
            if obj is base_class:
                continue
            candidates.append(obj)

        if not candidates:
            return None

        named = [obj for obj in candidates if obj.__name__.endswith(suffix)]

        for obj in named:
            if issubclass(obj, base_class):
                return obj

        for obj in named:
            handle = getattr(obj, "handle", None)
            if inspect.iscoroutinefunction(handle):
                return obj

        for obj in candidates:
            if issubclass(obj, base_class):
                return obj

        for obj in candidates:
            handle = getattr(obj, "handle", None)
            if inspect.iscoroutinefunction(handle):
                return obj

        return candidates[0]

    def _init_agents(self):
        """Initialize default agent registry and prewarm configured envid registries."""

        self.agents = self._build_agents_for_envid(None)
        self._agents_by_envid[None] = self.agents

        for envid in load_environment_registry(self.config).keys():
            self._agents_by_envid[envid] = self._build_agents_for_envid(envid)

    def _init_adapters(self) -> None:
        """Initialize adapter registry from discovered adapter classes."""

        self.adapters = self._build_adapters()

    def _build_adapters(self) -> Dict[str, Adapter]:
        """Instantiate discovered adapters with runtime configuration."""

        built: Dict[str, Adapter] = {}
        for adapter_id, adapter_cls in self.adapter_classes.items():
            try:
                cfg = assemble_component_config(
                    component_type="adapter",
                    component_id=adapter_id,
                    envid=None,
                    root_config=self.config,
                    local_config=None,
                )
                if cfg.get("enabled") is False:
                    log("core", "info", f"Adapter '{adapter_id}' is disabled by config", tag="orchestrator")
                    continue

                try:
                    instance = adapter_cls(self, self.config)
                except TypeError:
                    instance = adapter_cls(self)

                built[adapter_id] = instance
            except Exception as e:
                log(
                    "core",
                    "error",
                    f"Failed to initialize adapter '{adapter_id}': {e}\n{traceback.format_exc()}",
                    tag="orchestrator",
                )
        return built

    def _build_agents_for_envid(self, envid: str | None) -> Dict[str, Any]:
        """Build agent objects for one envid using shared core overlay assembly.

        Input: resolved envid or None.
        Output: agent id -> agent instance map.
        """

        built: Dict[str, Any] = {}
        for agent_id, agent_cls in self.agent_classes.items():
            cfg = AgentBase.assemble_runtime_config(self.config, agent_id, envid=envid)
            if cfg.get("enabled", True) is False:
                continue
            try:
                built[agent_id] = self._instantiate_agent(agent_id, agent_cls, cfg)
            except Exception as e:
                log(
                    "core",
                    "error",
                    f"Failed to initialize agent '{agent_id}' for envid={envid or '-'}: {e}\n{traceback.format_exc()}",
                    tag="orchestrator",
                )
        return built

    def _instantiate_agent(self, agent_id: str, agent_cls: Type[Any], cfg: dict[str, Any]) -> Any:
        """Instantiate one agent class with fallback constructor signatures."""

        agent_cfg = {"name": agent_id, **cfg}
        try:
            return agent_cls(self.config, agent_cfg)
        except TypeError:
            return agent_cls(self.config)

    def _agents_for_envid(self, envid: str | None) -> Dict[str, Any]:
        """Get cached agent map for envid, building it on first use."""

        if envid in self._agents_by_envid:
            return self._agents_by_envid[envid]
        self._agents_by_envid[envid] = self._build_agents_for_envid(envid)
        return self._agents_by_envid[envid]

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
        return graph.compile(checkpointer=self.checkpointer, store=self.store)

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

    @classmethod
    def _sanitize_state_value(cls, value: Any, depth: int = 0) -> Any:
        """Sanitize arbitrary values for durable graph state serialization."""

        if depth > 8:
            return str(value)
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for key, item in value.items():
                out[str(key)] = cls._sanitize_state_value(item, depth + 1)
            return out
        if isinstance(value, (list, tuple, set)):
            return [cls._sanitize_state_value(item, depth + 1) for item in value]
        return str(value)

    @classmethod
    def _sanitize_context(cls, context: dict[str, Any] | None) -> dict[str, Any]:
        """Return serialization-safe context payload for task state."""

        raw = context or {}
        if not isinstance(raw, dict):
            return {"value": cls._sanitize_state_value(raw)}
        return cls._sanitize_state_value(raw)

    @staticmethod
    def _error_text(exc: Exception | str | None) -> str:
        """Render stable non-empty error text.

        Input: exception or plain error value.
        Output: non-empty error string.
        """

        if exc is None:
            return "unknown error"
        if isinstance(exc, Exception):
            text = str(exc).strip()
            return text or exc.__class__.__name__
        text = str(exc).strip()
        return text or "unknown error"

    @classmethod
    def _classify_runtime_error(cls, exc: Exception | str | None) -> tuple[str, str, bool]:
        """Classify runtime errors for concise logging and user-facing status.

        Output: (error_code, public_message, expected_operational_error).
        """

        raw = cls._error_text(exc)
        lowered = raw.lower()

        if "upstream_timeout" in lowered or "upstream timeout" in lowered:
            return (
                "llm_upstream_timeout",
                "LLM upstream timeout: provider backend is overloaded; task dropped.",
                True,
            )

        if "upstream_error" in lowered:
            if "404" in lowered:
                return (
                    "llm_upstream_404",
                    "LLM upstream routing error (404): requested model/route is unavailable; task dropped.",
                    True,
                )
            return (
                "llm_upstream_error",
                "LLM upstream error: backend is unavailable or overloaded; task dropped.",
                True,
            )

        if "error code: 5" in lowered or "http 5" in lowered:
            return (
                "llm_upstream_5xx",
                "LLM provider 5xx error: backend is unavailable or overloaded; task dropped.",
                True,
            )

        if "timeout_error" in lowered or "request timed out" in lowered or "timed out" in lowered:
            return (
                "llm_timeout",
                "LLM timeout: provider is overloaded or too slow; try again later.",
                True,
            )

        if "apiconnectionerror" in lowered or "connection error" in lowered or "connection refused" in lowered:
            return (
                "llm_connection",
                "LLM connection error: provider is unavailable; try again later.",
                True,
            )

        if "rate limit" in lowered or "429" in lowered:
            return (
                "llm_rate_limit",
                "LLM rate limit: too many requests; try again later.",
                True,
            )

        return ("runtime_error", raw, False)

    async def _node_route(self, state: OrchestratorState) -> OrchestratorState:
        """Validate requested agent and mark task as running."""
        task_id = state["task_id"]
        agent_name = state.get("agent", "")
        envid = str((state.get("context", {}) or {}).get("envid", "")).strip() or None
        agents = self._agents_for_envid(envid)
        if agent_name not in agents:
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
        context = {**(state.get("context", {}) or {}), "task_id": task_id}
        envid = str(context.get("envid", "")).strip() or None
        agents = self._agents_for_envid(envid)

        try:
            result = await agents[agent_name].handle(query, context)
            if isinstance(result, dict) and result.get("error"):
                raise RuntimeError(str(result.get("error")))
            self._set_task(task_id, status="done", result=result)
            return {"status": "done", "result": result, "error": ""}
        except Exception as e:
            retries = int(state.get("retries", 0))
            max_retries = int(state.get("max_retries", 1))
            fallback_agent = str(state.get("fallback_agent", "")).strip()
            err_code, err_text, expected = self._classify_runtime_error(e)
            if expected:
                log(
                    "core",
                    "warning",
                    f"Task {task_id} failed in agent={agent_name} on retry={retries}: {err_code} ({err_text})",
                    tag="orchestrator",
                )
                self._set_task(task_id, status="error", result={"error": err_text, "error_code": err_code})
                return {"status": "error", "error": err_text}
            else:
                log(
                    "core",
                    "error",
                    f"Task {task_id} failed in agent={agent_name} on retry={retries}: {e}\n{traceback.format_exc()}",
                    tag="orchestrator",
                )

            self._set_task(
                task_id,
                status="retrying",
                result={"error": err_text, "error_code": err_code, "retries": retries},
            )

            if retries < max_retries:
                return {"status": "retry", "error": err_text}
            if fallback_agent and fallback_agent in self.agents and fallback_agent != agent_name:
                return {"status": "fallback", "error": err_text}

            self._set_task(task_id, status="error", result={"error": err_text, "error_code": err_code})
            return {"status": "error", "error": err_text}

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
        context = {**(state.get("context", {}) or {}), "task_id": task_id}
        envid = str(context.get("envid", "")).strip() or None
        agents = self._agents_for_envid(envid)
        if not fallback_agent or fallback_agent not in agents:
            err = "Fallback agent is not configured"
            self._set_task(task_id, status="error", result={"error": err})
            return {"status": "error", "error": err}

        query = state["query"]
        self._set_task(task_id, status="running", result={"fallback": fallback_agent})
        try:
            result = await agents[fallback_agent].handle(query, context)
            self._set_task(task_id, status="done", result={"via_fallback": fallback_agent, **result})
            return {"status": "done", "result": {"via_fallback": fallback_agent, **result}}
        except Exception as e:
            err_code, err_text, expected = self._classify_runtime_error(e)
            if expected:
                log(
                    "core",
                    "warning",
                    f"Task {task_id} fallback agent={fallback_agent} failed: {err_code} ({err_text})",
                    tag="orchestrator",
                )
            else:
                log(
                    "core",
                    "error",
                    f"Task {task_id} fallback agent={fallback_agent} failed: {e}\n{traceback.format_exc()}",
                    tag="orchestrator",
                )
            self._set_task(task_id, status="error", result={"error": err_text, "error_code": err_code})
            return {"status": "error", "error": err_text}

    def _after_fallback(self, state: OrchestratorState) -> str:
        """Choose final edge after fallback execution."""
        return "done" if state.get("status") == "done" else "error"

    def list_agents(self):
        return list(self._agents_for_envid(None).keys())

    def get_agent_info(self, agent_name: str, limit: int = 20) -> dict[str, Any]:
        """Return runtime metadata and recent task history for one agent."""
        agents = self._agents_for_envid(None)
        if agent_name not in agents:
            return {}

        safe_limit = max(1, min(100, int(limit)))
        agent = agents[agent_name]
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
        ctx = self._sanitize_context(context)
        envid = str(ctx.get("envid", "")).strip() or None
        agents = self._agents_for_envid(envid)
        if agent_name not in agents:
            raise ValueError(f"Agent '{agent_name}' not found")
        task_id = str(uuid.uuid4())
        now = self._utcnow_iso()
        self.tasks[task_id] = {
            "status": "pending",
            "agent": agent_name,
            "envid": envid,
            "query": query,
            "result": None,
            "retries": 0,
            "created_at": now,
            "updated_at": now,
        }
        asyncio.create_task(self._run_task(task_id, agent_name, query, ctx))
        return task_id

    async def _run_task(self, task_id, agent_name, query, context):
        """Execute full LangGraph flow and write terminal task state."""
        orch_cfg = self._task_cfg()
        safe_context = self._sanitize_context(context)
        initial: OrchestratorState = {
            "task_id": task_id,
            "agent": agent_name,
            "query": query,
            "context": safe_context,
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
            final_state = await self.graph.ainvoke(initial, config={"configurable": {"thread_id": task_id}})
            if final_state.get("status") == "done":
                self._set_task(
                    task_id,
                    status="done",
                    result=final_state.get("result"),
                    retries=int(final_state.get("retries", self.tasks[task_id].get("retries", 0))),
                )
            elif final_state.get("status") == "error":
                err_text = self._error_text(final_state.get("error", "unknown error"))
                self._set_task(
                    task_id,
                    status="error",
                    result={"error": err_text},
                    retries=int(final_state.get("retries", self.tasks[task_id].get("retries", 0))),
                )
        except Exception as e:
            err_text = self._error_text(e)
            log(
                "core",
                "error",
                f"Task runner crashed for task {task_id} agent={agent_name}: {e}\n{traceback.format_exc()}",
                tag="orchestrator",
            )
            self._set_task(task_id, status="error", result={"error": err_text})
