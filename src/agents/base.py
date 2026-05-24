"""AgentBase: abstract chain-based agent on top of LangChain."""
from __future__ import annotations

from abc import ABC, abstractmethod
import os
from typing import Any

from core.envid_runtime import assemble_component_config
from core.llm_wiretap import pop_llm_log_context
from core.llm_wiretap import push_llm_log_context
from core.logging_utils import log
from memory.tools import MemoryTools


class AgentBase(ABC):
    """Base class for agents that execute a LangChain runnable chain."""

    def __init__(self, app_config: dict[str, Any], agent_config: dict[str, Any] | None = None):
        self.app_config = app_config or {}
        self.agent_config = agent_config or {}
        self.name = self.agent_config.get("name", self.__class__.__name__.lower())
        root_dir = str(self.app_config.get("root") or os.getcwd())
        self.memory_tools = MemoryTools(root_dir, self.app_config)
        self.chain = self.build_chain()

    @classmethod
    def assemble_runtime_config(
        cls,
        root_config: dict[str, Any],
        agent_id: str,
        envid: str | None = None,
        local_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Assemble final agent config via shared core overlay rules.

        Input: root config, agent id, optional envid and local layer.
        Output: final `agents.items.<agent_id>` config section.
        """

        return assemble_component_config(
            component_type="agent",
            component_id=agent_id,
            envid=envid,
            root_config=root_config,
            local_config=local_config,
        )

    @abstractmethod
    def build_chain(self):
        """Build and return LangChain runnable chain."""

    async def handle(self, query: str, context=None) -> dict:
        """Execute LangChain chain and return unified result payload."""
        ctx = context or {}
        envid = str(ctx.get("envid", "")).strip() or None
        profile_id = str(ctx.get("chat_id") or ctx.get("user_id") or "").strip() or None
        memory_context = ""
        memory_meta: dict[str, Any] = {"semantic_hits": 0, "doc_hits": 0, "summary_hit": False, "procedural_items": 0}

        try:
            semantic_hits = self.memory_tools.recall_memory(
                agent_id=self.name,
                query=query,
                limit=5,
                envid=envid,
            )
            doc_hits = self.memory_tools.search_docs(
                query=query,
                corpora=self._allowed_corpora(),
                limit=6,
            )
            procedural_memory = self.memory_tools.get_procedural_memory(agent_id=self.name, limit=3, envid=envid)
            session_summary: dict[str, Any] = {}
            task_id = str(ctx.get("task_id", "") or "").strip()
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

        payload = {
            "query": query,
            "context": ctx,
            "memory_context": memory_context,
        }
        if "messages" in ctx:
            payload["messages"] = ctx.get("messages")
        invoke_config = None
        log_token = None
        if self._llm_logging_enabled():
            payload_preview = {
                "query": query,
                "context": ctx,
                "memory_context": memory_context,
            }
            log_token = push_llm_log_context(
                agent_id=self.name,
                envid=envid,
                log_path=self._llm_log_path(),
                payload=payload_preview,
            )
        try:
            out = await self.chain.ainvoke(payload, config=invoke_config)
        finally:
            if log_token is not None:
                pop_llm_log_context(log_token)
        content = getattr(out, "content", out)

        try:
            self.memory_tools.record_episode(
                agent_id=self.name,
                text=f"query={query[:500]} result={str(content)[:1000]}",
                task_id=str(ctx.get("task_id", "")) or None,
                outcome="ok",
                envid=envid,
            )
            if profile_id:
                self.memory_tools.remember_profile_fact(
                    agent_id=self.name,
                    profile_id=profile_id,
                    text=f"recent_query={query[:280]} recent_result={str(content)[:280]}",
                    scope="dialogue",
                    importance=0.35,
                    envid=envid,
                )
        except Exception as e:
            log("agents", "warning", f"Failed to record episode for agent={self.name}: {e}")

        return {"result": content, "memory": memory_meta}

    def _llm_logging_enabled(self) -> bool:
        """Return true when JSONL logging of LLM exchanges is enabled for this agent."""

        logging_cfg = self.agent_config.get("logging", {}) if isinstance(self.agent_config, dict) else {}
        if not isinstance(logging_cfg, dict):
            return False
        return bool(logging_cfg.get("log_llm", False))

    def _llm_log_path(self) -> str:
        """Resolve JSONL path for LLM exchange logging."""

        root_dir = str(self.app_config.get("root") or os.getcwd())
        logs_dir = os.path.join(root_dir, "logs")
        os.makedirs(logs_dir, exist_ok=True)
        return os.path.join(logs_dir, f"{self.name}_llm.jsonl")

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
