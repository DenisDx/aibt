"""GraphEchoAgent: minimal LangGraph agent with one LLM node."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, TypedDict

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda
from langgraph.graph import END, StateGraph

from agents.base import AgentBase
from agents.llm_factory import build_llm
from core.logging_utils import log
from memory.langgraph_runtime import build_langgraph_runtime


class GraphEchoState(TypedDict, total=False):
    """Graph state for GraphEchoAgent execution."""

    query: str
    context: dict[str, Any]
    result: str
    error: str


class GraphEchoAgent(AgentBase):
    """LangGraph-backed agent that reuses shared AgentBase memory flow."""

    def build_chain(self):
        """Build LangGraph runtime and expose it as a runnable chain."""

        root_dir = str(self.app_config.get("root") or os.getcwd())
        self.checkpointer, self.store = build_langgraph_runtime(root_dir, self.app_config, self.name)
        self.prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are GraphEchoAgent. Reply briefly and clearly. "
                    "If user asks to repeat, repeat their message exactly. "
                    "Use memory context only as background; do not print it.\n{memory_context}",
                ),
                ("human", "{query}"),
            ]
        )
        has_tools, tools_value = self._llm_tools_value()
        if has_tools:
            self.llm = build_llm(self.app_config, tools=tools_value)
        else:
            self.llm = build_llm(self.app_config)
        self.graph = self._build_graph()
        return RunnableLambda(self._ainvoke_graph_chain)

    def _build_graph(self):
        """Build minimal one-node LangGraph pipeline."""
        graph = StateGraph(GraphEchoState)
        graph.add_node("answer", self._node_answer)
        graph.set_entry_point("answer")
        graph.add_edge("answer", END)
        return graph.compile(checkpointer=self.checkpointer, store=self.store)

    async def _ainvoke_graph_chain(self, payload: dict[str, Any]) -> str:
        """Run LangGraph and return the final result text for AgentBase."""

        query = str(payload.get("query", "") or "").strip()
        context = payload.get("context", {}) if isinstance(payload, dict) else {}
        ctx = context if isinstance(context, dict) else {}
        memory_context = str(payload.get("memory_context", "") or "")
        thread_id = str(ctx.get("task_id") or ctx.get("thread_id") or self.name)
        final = await self.graph.ainvoke(
            {
                "query": query,
                "context": {**ctx, "memory_context": memory_context},
            },
            config={"configurable": {"thread_id": thread_id}},
        )
        return str(final.get("result", ""))

    async def _node_answer(self, state: GraphEchoState) -> GraphEchoState:
        """Generate answer from configured LLM."""
        query = str(state.get("query", "")).strip()
        context = state.get("context", {}) or {}
        envid = str(context.get("envid", "")).strip() or "global"
        task_id = str(context.get("task_id") or context.get("thread_id") or self.name).strip() or self.name
        namespace = ("env", envid, "agents", self.name, "threads")
        thread_key = task_id
        memory_context = str(context.get("memory_context", ""))

        try:
            thread_state = self.store.get(namespace, thread_key) or {}
            value = thread_state.get("value") if isinstance(thread_state, dict) else {}
            turns = list((value or {}).get("turns") or [])
            recent_turns = turns[-3:]
            if recent_turns:
                lines = ["Thread memory:"]
                for idx, turn in enumerate(recent_turns, start=1):
                    turn_query = str(turn.get("query") or "")
                    turn_result = str(turn.get("result") or "")
                    lines.append(f"{idx}. Q: {turn_query[:140]} | A: {turn_result[:180]}")
                memory_context = "\n".join([part for part in (memory_context, "\n".join(lines)) if part])
        except Exception as e:
            log("agents", "warning", f"GraphEcho thread memory read skipped for agent={self.name}: {e}")

        chain = self.prompt | self.llm
        out = await chain.ainvoke({"query": query, "memory_context": memory_context})
        content = getattr(out, "content", out)

        try:
            thread_state = self.store.get(namespace, thread_key) or {}
            value = thread_state.get("value") if isinstance(thread_state, dict) else {}
            turns = list((value or {}).get("turns") or [])
            turns.append(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "query": query,
                    "result": str(content),
                }
            )
            self.store.put(
                namespace,
                thread_key,
                {
                    "agent_id": self.name,
                    "thread_id": thread_key,
                    "turns": turns[-20:],
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception as e:
            log("agents", "warning", f"GraphEcho thread memory write skipped for agent={self.name}: {e}")

        return {"result": str(content)}

