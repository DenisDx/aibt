"""Agent-facing tool wrappers for memory facade."""

from __future__ import annotations

from typing import Any

from memory.api import get_memory_service


class MemoryTools:
    """Thin wrappers for memory and RAG operations.

    Input: root dir and app config.
    Output: callable methods for agents.
    """

    def __init__(self, root_dir: str, config: dict[str, Any]):
        self.root_dir = root_dir
        self.config = config

    def search_docs(
        self,
        query: str,
        corpora: list[str] | None = None,
        filters: dict[str, Any] | None = None,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Search indexed documents.

        Input: query and optional filters.
        Output: ranked hit list.
        """

        svc = get_memory_service(self.root_dir, self.config)
        return svc.search_docs(query=query, corpora=corpora, filters=filters, limit=limit)

    def get_document(self, doc_id: str, version: int | None = None, mode: str = "source") -> dict[str, Any]:
        """Get document payload by id.

        Input: doc id, optional version and mode.
        Output: source/text/summary payload.
        """

        svc = get_memory_service(self.root_dir, self.config)
        return svc.get_document(doc_id=doc_id, version=version, mode=mode)

    def list_corpora(self) -> list[dict[str, Any]]:
        """List all corpora.

        Input: none.
        Output: corpus list.
        """

        svc = get_memory_service(self.root_dir, self.config)
        return svc.list_corpora()

    def list_documents(
        self,
        corpus_id: str,
        limit: int = 50,
        offset: int = 0,
        query: str | None = None,
        tag: str | None = None,
        sort_by: str | None = None,
        sort_dir: str | None = None,
    ) -> dict[str, Any]:
        """List documents in corpus with filters and pagination.

        Input: corpus id, page args, optional query and tag.
        Output: dict with items and paging metadata.
        """

        svc = get_memory_service(self.root_dir, self.config)
        return svc.list_documents(
            corpus_id=corpus_id,
            limit=limit,
            offset=offset,
            query=query,
            tag=tag,
            sort_by=sort_by,
            sort_dir=sort_dir,
        )

    def ingest_document(
        self,
        source: dict[str, Any],
        corpus_id: str,
        title: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Queue document for ingest.

        Input: source descriptor and corpus metadata.
        Output: ingest job metadata.
        """

        svc = get_memory_service(self.root_dir, self.config)
        return svc.ingest_document(source=source, corpus_id=corpus_id, title=title, tags=tags)

    def delete_document(self, doc_id: str) -> bool:
        """Soft-delete document.

        Input: document id.
        Output: deletion flag.
        """

        svc = get_memory_service(self.root_dir, self.config)
        return svc.delete_document(doc_id)

    def recall_memory(
        self,
        agent_id: str,
        query: str,
        scope: str | None = None,
        limit: int = 8,
        envid: str | None = None,
    ) -> list[dict[str, Any]]:
        """Recall memory snippets.

        Input: agent id, query, optional scope.
        Output: ranked memory list.
        """

        svc = get_memory_service(self.root_dir, self.config)
        return svc.recall_memory(agent_id=agent_id, query=query, scope=scope, limit=limit, envid=envid)

    def remember_fact(
        self,
        agent_id: str,
        text: str,
        scope: str | None = None,
        importance: float = 0.5,
        envid: str | None = None,
    ) -> dict[str, Any]:
        """Store semantic fact.

        Input: agent id and fact text.
        Output: write metadata.
        """

        svc = get_memory_service(self.root_dir, self.config)
        return svc.remember_fact(agent_id=agent_id, text=text, scope=scope, importance=importance, envid=envid)

    def record_episode(
        self,
        agent_id: str,
        text: str,
        task_id: str | None = None,
        outcome: str | None = None,
        envid: str | None = None,
    ) -> dict[str, Any]:
        """Store episodic event.

        Input: agent id and event text.
        Output: write metadata.
        """

        svc = get_memory_service(self.root_dir, self.config)
        return svc.record_episode(agent_id=agent_id, text=text, task_id=task_id, outcome=outcome, envid=envid)

    def get_procedural_memory(self, agent_id: str, limit: int = 20, envid: str | None = None) -> list[dict[str, Any]]:
        """Read procedural memory.

        Input: agent id and limit.
        Output: procedural records.
        """

        svc = get_memory_service(self.root_dir, self.config)
        return svc.get_procedural_memory(agent_id=agent_id, limit=limit, envid=envid)

    def get_session_summary(self, agent_id: str, thread_id: str, envid: str | None = None) -> dict[str, Any]:
        """Read one session summary.

        Input: agent id and thread id.
        Output: matching summary record or empty dict.
        """

        svc = get_memory_service(self.root_dir, self.config)
        return svc.get_session_summary(agent_id=agent_id, thread_id=thread_id, envid=envid)

    def get_agent_profile(self, agent_id: str, envid: str | None = None) -> dict[str, Any]:
        """Read aggregated agent memory profile.

        Input: agent id.
        Output: namespace counters and timestamps.
        """

        svc = get_memory_service(self.root_dir, self.config)
        return svc.get_agent_profile(agent_id=agent_id, envid=envid)

    def update_procedural_memory(
        self,
        agent_id: str,
        text: str,
        reason: str | None = None,
        envid: str | None = None,
    ) -> dict[str, Any]:
        """Store procedural rule update.

        Input: agent id and text.
        Output: write metadata.
        """

        svc = get_memory_service(self.root_dir, self.config)
        return svc.update_procedural_memory(agent_id=agent_id, text=text, reason=reason, envid=envid)

    def remember_profile_fact(
        self,
        agent_id: str,
        profile_id: str,
        text: str,
        scope: str | None = None,
        importance: float = 0.5,
        envid: str | None = None,
    ) -> dict[str, Any]:
        """Store user/channel profile fact.

        Input: agent id, profile id, fact text, optional scope.
        Output: write metadata.
        """

        svc = get_memory_service(self.root_dir, self.config)
        return svc.remember_profile_fact(
            agent_id=agent_id,
            profile_id=profile_id,
            text=text,
            scope=scope,
            importance=importance,
            envid=envid,
        )

    def get_profile_memory(self, agent_id: str, profile_id: str, limit: int = 20, envid: str | None = None) -> list[dict[str, Any]]:
        """Read user/channel profile facts.

        Input: agent id, profile id, and limit.
        Output: profile fact list.
        """

        svc = get_memory_service(self.root_dir, self.config)
        return svc.get_profile_memory(agent_id=agent_id, profile_id=profile_id, limit=limit, envid=envid)
