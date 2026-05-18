"""GraphEchoAgent: minimal LangGraph agent with one LLM node."""
from __future__ import annotations

from typing import Any, TypedDict

from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import END, StateGraph

from agents.llm_factory import build_llm


class GraphEchoState(TypedDict, total=False):
    """Graph state for GraphEchoAgent execution."""

    query: str
    context: dict[str, Any]
    result: str
    error: str


class GraphEchoAgent:
    """Minimal LangGraph-backed agent connected to configured LLM."""

    def __init__(self, app_config: dict[str, Any], agent_config: dict[str, Any] | None = None):
        """Create graph-backed agent using app and per-agent config."""
        self.app_config = app_config or {}
        self.agent_config = agent_config or {}
        self.name = self.agent_config.get("name", "graph_echo")
        self.prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are GraphEchoAgent. Reply briefly and clearly. "
                    "If user asks to repeat, repeat their message exactly.",
                ),
                ("human", "{query}"),
            ]
        )
        self.llm = build_llm(self.app_config)
        self.graph = self._build_graph()

    def _build_graph(self):
        """Build minimal one-node LangGraph pipeline."""
        graph = StateGraph(GraphEchoState)
        graph.add_node("answer", self._node_answer)
        graph.set_entry_point("answer")
        graph.add_edge("answer", END)
        return graph.compile()

    async def _node_answer(self, state: GraphEchoState) -> GraphEchoState:
        """Generate answer from configured LLM."""
        query = str(state.get("query", "")).strip()
        chain = self.prompt | self.llm
        out = await chain.ainvoke({"query": query})
        content = getattr(out, "content", out)
        return {"result": str(content)}

    async def handle(self, query: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run graph for one user query and return unified result payload."""
        final = await self.graph.ainvoke({"query": query, "context": context or {}})
        return {"result": str(final.get("result", ""))}
