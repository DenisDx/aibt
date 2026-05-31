"""GraphEchoAgent: minimal LangGraph agent with one LLM node."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, TypedDict

from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import END, StateGraph

from agents.llm_factory import build_llm
from core.envid_runtime import build_effective_config
from core.logging_utils import log
from memory.tools import MemoryTools
from memory.langgraph_runtime import build_langgraph_runtime
from memoryd import get_memoryd_service


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
        root_dir = str(self.app_config.get("root") or os.getcwd())
        self.memory_tools = MemoryTools(root_dir, self.app_config)
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

    def _llm_tools_value(self) -> tuple[bool, Any]:
        """Return explicit tools setting from agent config.

        Output: (is_defined, value). Key absence means no passthrough.
        """

        cfg = self.agent_config if isinstance(self.agent_config, dict) else {}
        if not isinstance(cfg, dict) or "tools" not in cfg:
            return (False, None)
        return (True, cfg.get("tools"))

    def _build_graph(self):
        """Build minimal one-node LangGraph pipeline."""
        graph = StateGraph(GraphEchoState)
        graph.add_node("answer", self._node_answer)
        graph.set_entry_point("answer")
        graph.add_edge("answer", END)
        return graph.compile(checkpointer=self.checkpointer, store=self.store)

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

    async def handle(self, query: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run graph for one user query and return unified result payload."""
        ctx = context or {}
        envid = str(ctx.get("envid", "")).strip() or None
        profile_id = str(ctx.get("chat_id") or ctx.get("user_id") or "").strip() or None
        memory_context = ""
        memory_meta: dict[str, Any] = {"semantic_hits": 0, "doc_hits": 0, "summary_hit": False, "procedural_items": 0}
        memoryd_meta: dict[str, Any] = {"enabled": False, "selected_records_count": 0, "task_id": "", "types": []}
        try:
            semantic_hits = self.memory_tools.recall_memory(self.name, query, limit=5, envid=envid)
            doc_hits = self.memory_tools.search_docs(query=query, corpora=self._allowed_corpora(), limit=6)
            procedural_memory = self.memory_tools.get_procedural_memory(agent_id=self.name, limit=3, envid=envid)
            task_id = str(ctx.get("task_id", "") or "").strip()
            session_summary: dict[str, Any] = {}
            if task_id:
                session_summary = self.memory_tools.get_session_summary(agent_id=self.name, thread_id=task_id, envid=envid)
            memory_meta["semantic_hits"] = len(semantic_hits)
            memory_meta["doc_hits"] = len(doc_hits)
            memory_meta["summary_hit"] = bool(session_summary)
            memory_meta["procedural_items"] = len(procedural_memory)
            if semantic_hits or doc_hits or procedural_memory or session_summary:
                memory_context = self._format_memory_context(semantic_hits, doc_hits, procedural_memory, session_summary)
        except Exception as e:
            log("agents", "warning", f"Memory enrichment skipped for agent={self.name}: {e}")

        try:
            effective_config = build_effective_config(self.app_config, envid)
            memoryd_cfg = effective_config.get("memoryd", {}) if isinstance(effective_config, dict) else {}
            if isinstance(memoryd_cfg, dict) and bool(memoryd_cfg.get("enabled", False)):
                memoryd_service = get_memoryd_service(str(self.app_config.get("root") or os.getcwd()), effective_config)
                memoryd_service.initialize()
                muid = str(ctx.get("muid") or ctx.get("chat_id") or ctx.get("user_id") or memoryd_cfg.get("muid") or "").strip() or None
                memoryd_result = memoryd_service.get_context(muid, types=None, render="markdown")
                memoryd_text = str(memoryd_result.get("text") or "").strip()
                if memoryd_text:
                    memory_context = "\n\n".join(part for part in (memory_context, "Memoryd context:\n" + memoryd_text) if part)
                memoryd_meta = {
                    "enabled": True,
                    "selected_records_count": int((memoryd_result.get("metadata") or {}).get("selected_records_count", 0) or 0),
                    "task_id": "",
                    "types": list((memoryd_result.get("metadata") or {}).get("types", []) or []),
                }
        except Exception as e:
            log("agents", "warning", f"Memoryd enrichment skipped for agent={self.name}: {e}")

        final = await self.graph.ainvoke(
            {
                "query": query,
                "context": {**ctx, "memory_context": memory_context},
            }
            , config={"configurable": {"thread_id": str(ctx.get("task_id") or ctx.get("thread_id") or self.name)}}
        )
        result_text = str(final.get("result", ""))

        try:
            self.memory_tools.record_episode(
                agent_id=self.name,
                text=f"query={query[:500]} result={result_text[:1000]}",
                task_id=str(ctx.get("task_id", "")) or None,
                outcome="ok",
                envid=envid,
            )
            if profile_id:
                self.memory_tools.remember_profile_fact(
                    agent_id=self.name,
                    profile_id=profile_id,
                    text=f"recent_query={query[:280]} recent_result={result_text[:280]}",
                    scope="dialogue",
                    importance=0.35,
                    envid=envid,
                )
        except Exception as e:
            log("agents", "warning", f"Failed to record episode for agent={self.name}: {e}")

        try:
            effective_config = build_effective_config(self.app_config, envid)
            memoryd_cfg = effective_config.get("memoryd", {}) if isinstance(effective_config, dict) else {}
            if isinstance(memoryd_cfg, dict) and bool(memoryd_cfg.get("enabled", False)):
                memoryd_service = get_memoryd_service(str(self.app_config.get("root") or os.getcwd()), effective_config)
                memoryd_service.initialize()
                muid = str(ctx.get("muid") or ctx.get("chat_id") or ctx.get("user_id") or memoryd_cfg.get("muid") or "").strip() or None
                enqueue_result = memoryd_service.enqueue_update(
                    source_context={k: v for k, v in ctx.items() if k not in {"memory_context"}},
                    final_response=str(result_text),
                    muid=muid,
                    caller_tag=str(ctx.get("caller_tag") or ctx.get("task_id") or self.name).strip() or None,
                    types=None,
                )
                memoryd_meta.update({
                    "task_id": str(enqueue_result.get("task_id") or ""),
                    "types": list(enqueue_result.get("types") or memoryd_meta.get("types") or []),
                })
        except Exception as e:
            log("agents", "warning", f"Memoryd enqueue skipped for agent={self.name}: {e}")

        return {"result": result_text, "memory": memory_meta, "memoryd": memoryd_meta}

    def _allowed_corpora(self) -> list[str] | None:
        """Resolve allowed corpora for this agent.

        Input: per-agent config.
        Output: allowlist or None when unrestricted.
        """

        rag = self.agent_config.get("rag", {}) if isinstance(self.agent_config, dict) else {}
        corpora = rag.get("corpora") if isinstance(rag, dict) else None
        if not isinstance(corpora, list):
            return None
        clean = [str(x).strip() for x in corpora if str(x).strip()]
        return clean or []

    @staticmethod
    def _format_memory_context(
        semantic_hits: list[dict[str, Any]],
        doc_hits: list[dict[str, Any]],
        procedural_memory: list[dict[str, Any]] | None = None,
        session_summary: dict[str, Any] | None = None,
    ) -> str:
        """Build compact textual memory context for prompts.

        Input: semantic and document hits.
        Output: formatted context block.
        """

        lines: list[str] = []
        if semantic_hits:
            lines.append("Semantic memory:")
            for idx, item in enumerate(semantic_hits[:5], start=1):
                lines.append(f"{idx}. {str(item.get('text', ''))[:220]}")
        if doc_hits:
            lines.append("Document hits:")
            for idx, item in enumerate(doc_hits[:5], start=1):
                title = str(item.get("title") or item.get("doc_id") or "document")
                snippet = str(item.get("snippet") or "")
                lines.append(f"{idx}. {title}: {snippet[:220]}")
        if procedural_memory:
            lines.append("Procedural memory:")
            for idx, item in enumerate(procedural_memory[:3], start=1):
                lines.append(f"{idx}. {str(item.get('text', ''))[:220]}")
        if session_summary:
            lines.append("Session summary:")
            lines.append(str(session_summary.get("text") or "")[:320])
        return "\n".join(lines)
