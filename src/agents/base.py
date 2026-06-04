"""AgentBase: abstract chain-based agent on top of LangChain."""
from __future__ import annotations

from abc import ABC, abstractmethod
import os
from typing import Any

from core.envid_runtime import assemble_component_config
from core.envid_runtime import build_effective_config
from core.llm_wiretap import pop_llm_log_context
from core.llm_wiretap import push_llm_log_context
from core.logging_utils import log
from memory.tools import MemoryTools
from memoryd import get_memoryd_service
from memoryd.schemas import normalize_memoryd_type_specs, split_memoryd_type_spec


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

    async def on_init(self, runtime: dict[str, Any]) -> None:
        """Lifecycle hook called once after agent instantiation."""

        return None

    async def on_cron_tick(self, runtime: dict[str, Any]) -> None:
        """Lifecycle hook called on each cron tick for active agent instances."""

        return None

    async def on_shutdown(self, runtime: dict[str, Any]) -> None:
        """Lifecycle hook called during graceful application shutdown."""

        return None

    async def _postprocess_result(self, result: Any, *, query: str, context: dict[str, Any], envid: str | None) -> Any:
        """Optional protected hook for agent-specific result normalization.

        Input: raw model/chain result and request context.
        Output: normalized result payload to be persisted and returned.
        """

        return result

    def _memoryd_caller_tag(self, ctx: dict[str, Any]) -> str | None:
        """Resolve stable caller_tag for MemoryD enqueue dedup/replacement.

        Input: request context.
        Output: caller_tag string or None.
        """

        explicit = str(ctx.get("caller_tag") or "").strip()
        if explicit:
            return explicit

        adapter_name = str(ctx.get("adapter") or "").strip().lower()
        chat_type = str(ctx.get("chat_type") or "").strip().lower()
        if adapter_name == "telegram" and chat_type in ("group", "supergroup"):
            chat_id = str(ctx.get("chat_id") or "").strip()
            if chat_id:
                return chat_id

        fallback = str(ctx.get("task_id") or self.name or "").strip()
        return fallback or None

    async def handle(self, query: str, context=None) -> dict:
        """Execute LangChain chain and return unified result payload."""
        ctx = context or {}
        envid = str(ctx.get("envid", "")).strip() or None
        effective_config = build_effective_config(self.app_config, envid)
        memory_cfg = effective_config.get("memory", {}) if isinstance(effective_config, dict) else {}
        memory_enabled = bool(memory_cfg.get("enabled", True)) if isinstance(memory_cfg, dict) else True
        profile_id = str(ctx.get("chat_id") or ctx.get("user_id") or "").strip() or None
        memory_context = ""
        memoryd_context = ""
        memory_meta: dict[str, Any] = {"semantic_hits": 0, "doc_hits": 0, "summary_hit": False, "procedural_items": 0}
        memoryd_meta: dict[str, Any] = {"enabled": False, "selected_records_count": 0, "task_id": "", "types": []}

        if memory_enabled:
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
        else:
            log("agents", "info", f"Memory enrichment disabled for agent={self.name} envid={envid or 'global'}")

        try:
            memoryd_cfg = effective_config.get("memoryd", {}) if isinstance(effective_config, dict) else {}
            if isinstance(memoryd_cfg, dict) and bool(memoryd_cfg.get("enabled", False)):
                memoryd_service = get_memoryd_service(str(self.app_config.get("root") or os.getcwd()), effective_config, current_envid=envid)
                memoryd_service.initialize()
                muid = str(ctx.get("muid") or ctx.get("chat_id") or ctx.get("user_id") or memoryd_cfg.get("muid") or "").strip() or None
                context_types = self._resolve_memoryd_context_types(effective_config)
                if context_types:
                    memoryd_result = memoryd_service.get_context(muid, types=context_types, render="markdown")
                    memoryd_context = str(memoryd_result.get("text") or "").strip()
                    memoryd_meta = {
                        "enabled": True,
                        "selected_records_count": int((memoryd_result.get("metadata") or {}).get("selected_records_count", 0) or 0),
                        "task_id": "",
                        "types": list((memoryd_result.get("metadata") or {}).get("types", []) or []),
                    }
                else:
                    memoryd_meta = {
                        "enabled": True,
                        "selected_records_count": 0,
                        "task_id": "",
                        "types": [],
                    }
                    log("agents", "info", f"Memoryd context attach disabled by policy for agent={self.name} envid={envid or 'global'}")
        except Exception as e:
            log("agents", "warning", f"Memoryd enrichment skipped for agent={self.name}: {e}")

        if memoryd_context:
            memory_context = "\n\n".join(part for part in (memory_context, "Memoryd context:\n" + memoryd_context) if part)

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
        content = await self._postprocess_result(content, query=query, context=ctx, envid=envid)

        if memory_enabled:
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
        else:
            log("agents", "info", f"Memory writeback disabled for agent={self.name} envid={envid or 'global'}")

        try:
            effective_config = build_effective_config(self.app_config, envid)
            memoryd_cfg = effective_config.get("memoryd", {}) if isinstance(effective_config, dict) else {}
            if isinstance(memoryd_cfg, dict) and bool(memoryd_cfg.get("enabled", False)):
                memoryd_service = get_memoryd_service(str(self.app_config.get("root") or os.getcwd()), effective_config, current_envid=envid)
                memoryd_service.initialize()
                muid = str(ctx.get("muid") or ctx.get("chat_id") or ctx.get("user_id") or memoryd_cfg.get("muid") or "").strip() or None
                update_types = self._resolve_memoryd_update_types(effective_config)
                if update_types:
                    enqueue_result = memoryd_service.enqueue_update(
                        source_context={k: v for k, v in ctx.items() if k not in {"memory_context"}},
                        final_response=str(content),
                        muid=muid,
                        caller_tag=self._memoryd_caller_tag(ctx),
                        types=update_types,
                    )
                    memoryd_meta.update({
                        "task_id": str(enqueue_result.get("task_id") or ""),
                        "types": list(enqueue_result.get("types") or memoryd_meta.get("types") or []),
                    })
                else:
                    log("agents", "info", f"Memoryd update enqueue disabled by policy for agent={self.name} envid={envid or 'global'}")
        except Exception as e:
            log("agents", "warning", f"Memoryd enqueue skipped for agent={self.name}: {e}")

        return {"result": content, "memory": memory_meta, "memoryd": memoryd_meta}

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

    def _llm_tools_value(self) -> tuple[bool, Any]:
        """Return explicit tools setting from agent config.

        Output: (is_defined, value).
        If key is not present, is_defined is False.
        If key is present, value is forwarded as-is (None, [], list, etc).
        """

        cfg = self.agent_config if isinstance(self.agent_config, dict) else {}
        if not isinstance(cfg, dict) or "tools" not in cfg:
            return (False, None)
        return (True, cfg.get("tools"))

    def _agent_memoryd_cfg(self) -> dict[str, Any]:
        """Return per-agent memoryd policy block from agent config."""

        cfg = self.agent_config if isinstance(self.agent_config, dict) else {}
        md = cfg.get("memoryd", {}) if isinstance(cfg, dict) else {}
        return md if isinstance(md, dict) else {}

    @staticmethod
    def _memoryd_enabled_types(effective_config: dict[str, Any]) -> list[str]:
        """Return enabled memoryd item types from effective config."""

        memoryd_cfg = effective_config.get("memoryd", {}) if isinstance(effective_config, dict) else {}
        items = memoryd_cfg.get("items", {}) if isinstance(memoryd_cfg, dict) else {}
        out: list[str] = []
        if not isinstance(items, dict):
            return out
        for type_name, item_cfg in items.items():
            if isinstance(item_cfg, dict) and bool(item_cfg.get("enabled", False)):
                name = str(type_name).strip().lower()
                if name:
                    out.append(name)
        return sorted(set(out))

    @staticmethod
    def _memoryd_auto_writable_types(effective_config: dict[str, Any]) -> list[str]:
        """Return enabled memoryd item types allowed for auto update enqueue."""

        memoryd_cfg = effective_config.get("memoryd", {}) if isinstance(effective_config, dict) else {}
        items = memoryd_cfg.get("items", {}) if isinstance(memoryd_cfg, dict) else {}
        out: list[str] = []
        if not isinstance(items, dict):
            return out
        for type_name, item_cfg in items.items():
            if not isinstance(item_cfg, dict):
                continue
            if not bool(item_cfg.get("enabled", False)):
                continue
            if bool(item_cfg.get("manual_only", False)) or bool(item_cfg.get("external_writer", False)):
                continue
            name = str(type_name).strip().lower()
            if name:
                out.append(name)
        return sorted(set(out))

    def _resolve_memoryd_policy_types(self, key: str, *, default_types: list[str], allowed_types: list[str]) -> list[str]:
        """Resolve one per-agent memoryd type policy list with filtering."""

        policy = self._agent_memoryd_cfg()
        raw = policy.get(key) if isinstance(policy, dict) else None
        if raw is None:
            requested = normalize_memoryd_type_specs(default_types)
        elif isinstance(raw, list):
            requested = normalize_memoryd_type_specs(raw)
        else:
            log("agents", "warning", f"Invalid memoryd policy for agent={self.name}: {key} must be list; fallback to defaults")
            requested = normalize_memoryd_type_specs(default_types)

        allowed = set(str(x).strip().lower() for x in allowed_types if str(x).strip())
        out: list[str] = []
        dropped: list[str] = []
        for item in requested:
            _, type_name = split_memoryd_type_spec(item)
            if type_name in allowed:
                if item not in out:
                    out.append(item)
            elif item not in dropped:
                dropped.append(item)
        if dropped:
            log(
                "agents",
                "warning",
                "Memoryd policy for "
                f"agent={self.name} ignored unsupported {key}: {dropped}; allowed={sorted(allowed)}",
            )
        return out

    def _resolve_memoryd_context_types(self, effective_config: dict[str, Any]) -> list[str]:
        """Resolve final memoryd types for context attachment stage."""

        enabled = self._memoryd_enabled_types(effective_config)
        return self._resolve_memoryd_policy_types(
            "context_types",
            default_types=enabled,
            allowed_types=enabled,
        )

    def _resolve_memoryd_update_types(self, effective_config: dict[str, Any]) -> list[str]:
        """Resolve final memoryd types for async update enqueue stage."""

        auto_writable = self._memoryd_auto_writable_types(effective_config)
        return self._resolve_memoryd_policy_types(
            "update_types",
            default_types=auto_writable,
            allowed_types=auto_writable,
        )

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
