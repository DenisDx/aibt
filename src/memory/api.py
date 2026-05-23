"""Public memory facade for agents, adapters, and cron."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import traceback
from typing import Any
from uuid import uuid4

from core.logging_utils import log
from memory.rag.ingest import process_ingest_job
from memory.rag.retrieve import normalize_search_hits
from memory.store_pg import MemoryStore


class MemoryService:
    """Main memory facade.

    Input: project root and app config.
    Output: unified API for document and agent memory operations.
    """

    def __init__(self, root_dir: str, config: dict[str, Any]):
        self.root_dir = root_dir
        self.config = config
        self.memory_cfg = config.get("memory", {})
        self.enabled = bool(self.memory_cfg.get("enabled", True))
        self.data_root = os.path.join(root_dir, str(self.memory_cfg.get("path", "memory/runtime")))
        self.store = MemoryStore(root_dir, config)
        self._initialized = False
        self.corpus_card_root = os.path.join(self.data_root, "corpora")

    def initialize(self) -> None:
        """Prepare directories and database schema.

        Input: none.
        Output: initialized memory service.
        """

        if self._initialized:
            return
        os.makedirs(self.data_root, exist_ok=True)
        os.makedirs(os.path.join(self.data_root, "agent"), exist_ok=True)
        os.makedirs(self.corpus_card_root, exist_ok=True)
        self.store.ensure_schema()
        self._initialized = True

    def ingest_document(
        self,
        source: dict[str, Any],
        corpus_id: str,
        title: str | None = None,
        tags: list[str] | None = None,
        requested_by: str | None = None,
    ) -> dict[str, Any]:
        """Enqueue document for delayed indexing.

        Input: source descriptor, corpus id, and optional metadata.
        Output: ingest job metadata.
        """

        self.initialize()
        job_id = str(uuid4())
        clean_corpus = str(corpus_id or "").strip()
        if not clean_corpus:
            raise ValueError("corpus_id is required")

        self.store.ensure_corpus(clean_corpus)
        self.store.create_ingest_job(
            job_id=job_id,
            corpus_id=clean_corpus,
            source=source,
            title=title,
            tags=list(tags or []),
            requested_by=requested_by,
        )
        return {"job_id": job_id, "status": "pending", "corpus_id": clean_corpus}

    def run_ingest_batch(self, limit: int) -> dict[str, Any]:
        """Run limited amount of pending ingest work.

        Input: max jobs per run.
        Output: summary with processed/failed counters.
        """

        self.initialize()
        jobs = self.store.fetch_pending_jobs(limit=max(1, int(limit)))
        processed = 0
        failed = 0
        errors: list[dict[str, Any]] = []
        touched_corpora: set[str] = set()

        for job in jobs:
            job_id = str(job.get("job_id"))
            self.store.mark_job_running(job_id)
            try:
                doc_id, version = process_ingest_job(self.root_dir, self.config, self.store, job)
                self.store.mark_job_done(job_id, doc_id=doc_id, version=version)
                processed += 1
                touched_corpora.add(str(job.get("corpus_id") or ""))
            except Exception as e:
                failed += 1
                err = str(e)
                self.store.mark_job_error(job_id, err)
                errors.append({"job_id": job_id, "error": err})
                log(
                    "memory",
                    "error",
                    f"ingest job failed id={job_id}: {e}\n{traceback.format_exc()}",
                )

        for corpus_id in sorted(corpus for corpus in touched_corpora if corpus):
            self.refresh_corpus_card(corpus_id)

        return {"processed": processed, "failed": failed, "errors": errors}

    def list_corpora(self) -> list[dict[str, Any]]:
        """List available corpora.

        Input: none.
        Output: corpus list.
        """

        self.initialize()
        return self.store.list_corpora()

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
        """List documents in one corpus with pagination and filters.

        Input: corpus id, page args, optional query and tag.
        Output: dict with items and paging metadata.
        """

        self.initialize()
        items, total = self.store.list_documents(
            corpus_id=corpus_id,
            limit=max(1, int(limit)),
            offset=max(0, int(offset)),
            query=query,
            tag=tag,
            sort_by=sort_by,
            sort_dir=sort_dir,
        )
        return {
            "items": items,
            "total": total,
            "limit": max(1, int(limit)),
            "offset": max(0, int(offset)),
            "sort_by": str(sort_by or "updated_at"),
            "sort_dir": str(sort_dir or "desc"),
        }

    def search_docs(
        self,
        query: str,
        corpora: list[str] | None = None,
        filters: dict[str, Any] | None = None,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Search indexed document chunks.

        Input: query, optional corpora filter, optional metadata filters, and limit.
        Output: ranked hit list.
        """

        del filters  # reserved for phase B
        self.initialize()
        rows = self.store.search_chunks(query=query, corpora=corpora, limit=max(1, int(limit)))
        return normalize_search_hits(rows)

    def get_document(self, doc_id: str, version: int | None = None, mode: str = "source") -> dict[str, Any]:
        """Get document payload.

        Input: doc id, optional version, mode source|text|summary.
        Output: selected document payload.
        """

        self.initialize()
        row = self.store.get_document_version(doc_id, version) if version is not None else self.store.get_document_latest(doc_id)
        if not row:
            raise ValueError(f"document not found: {doc_id}")

        view = mode.strip().lower()
        if view == "summary":
            return {
                "doc_id": row.get("doc_id"),
                "corpus_id": row.get("corpus_id"),
                "version": row.get("version"),
                "title": row.get("title"),
                "summary": row.get("content_summary") or "",
            }
        if view == "text":
            return {
                "doc_id": row.get("doc_id"),
                "corpus_id": row.get("corpus_id"),
                "version": row.get("version"),
                "title": row.get("title"),
                "text": row.get("content_text") or "",
            }
        return {
            "doc_id": row.get("doc_id"),
            "corpus_id": row.get("corpus_id"),
            "version": row.get("version"),
            "title": row.get("title"),
            "source": row.get("source") or {},
            "content_path": row.get("content_path"),
        }

    def list_namespace_items(self, namespace: tuple[str, ...], limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        """List items in one durable namespace.

        Input: namespace tuple and paging.
        Output: namespace item list.
        """

        self.initialize()
        rows = self.store.list_namespace_items(namespace, limit=limit, offset=offset)
        return [dict(row) for row in rows]

    def get_agent_namespace_items(self, agent_id: str, namespace: str, limit: int = 100, profile_id: str | None = None) -> list[dict[str, Any]]:
        """Read agent namespace records.

        Input: agent id, namespace name, limit, and optional profile id.
        Output: namespace payload list.
        """

        clean_agent = self._safe_name(agent_id)
        clean_ns = self._safe_name(namespace)
        if clean_ns == "profiles" and profile_id:
            return self.get_profile_memory(clean_agent, profile_id, limit=limit)
        return self.list_namespace_items(("agent", clean_agent, clean_ns), limit=limit)

    def refresh_corpus_card(self, corpus_id: str) -> dict[str, Any]:
        """Refresh a lightweight corpus summary card.

        Input: corpus id.
        Output: serialized card payload.
        """

        self.initialize()
        clean_corpus = str(corpus_id or "").strip()
        if not clean_corpus:
            raise ValueError("corpus_id is required")

        docs = self.list_documents(clean_corpus, limit=5, offset=0)
        card = {
            "corpus_id": clean_corpus,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_documents": int(docs.get("total") or 0),
            "recent_documents": docs.get("items") or [],
            "retrieval": dict(self.memory_cfg.get("rag", {}).get("retrieval", {})),
        }
        path = self._corpus_card_path(clean_corpus)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(card, handle, ensure_ascii=False, indent=2, default=str)
        return card

    def get_corpus_card(self, corpus_id: str) -> dict[str, Any]:
        """Load a corpus summary card.

        Input: corpus id.
        Output: card payload, generating it when missing.
        """

        self.initialize()
        clean_corpus = str(corpus_id or "").strip()
        if not clean_corpus:
            raise ValueError("corpus_id is required")

        path = self._corpus_card_path(clean_corpus)
        if not os.path.exists(path):
            return self.refresh_corpus_card(clean_corpus)
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def delete_document(self, doc_id: str) -> bool:
        """Soft-delete a document from retrieval.

        Input: document id.
        Output: true when document state changed.
        """

        self.initialize()
        return self.store.soft_delete_document(doc_id)

    def _namespace_tuple(self, agent_id: str, namespace: str, *parts: str) -> tuple[str, ...]:
        """Build a durable namespace path.

        Input: agent id, namespace name, optional tail parts.
        Output: namespace tuple used by the runtime store.
        """

        clean_agent = self._safe_name(agent_id)
        clean_ns = self._safe_name(namespace)
        clean_parts = [self._safe_name(part) for part in parts if str(part).strip()]
        return ("agent", clean_agent, clean_ns, *clean_parts)

    def put_namespace_item(
        self,
        namespace: tuple[str, ...],
        payload: dict[str, Any],
        key: str | None = None,
        mirror_jsonl: bool = True,
    ) -> str:
        """Store one payload in a durable namespace.

        Input: namespace tuple, payload, optional key.
        Output: stored item key.
        """

        self.initialize()
        item_key = str(key or payload.get("ts") or f"{datetime.now(timezone.utc).isoformat()}-{uuid4().hex}")
        self.store.put_namespace_item(namespace, item_key, payload)
        if mirror_jsonl and len(namespace) >= 3 and namespace[0] == "agent":
            agent_id = str(namespace[1])
            namespace_name = str(namespace[2])
            if len(namespace) == 4 and namespace_name == "profiles":
                namespace_name = f"profiles_{namespace[3]}"
            self._append_namespace_item(agent_id, namespace_name, payload)
        return item_key

    def read_namespace_items(self, namespace: tuple[str, ...], limit: int = 100) -> list[dict[str, Any]]:
        """Read recent namespace payloads.

        Input: namespace tuple and limit.
        Output: payload list ordered by recency.
        """

        self.initialize()
        rows = self.store.list_namespace_items(namespace, limit=limit, offset=0)
        items = [dict(row.get("value") or {}) for row in rows if isinstance(row, dict)]
        if items:
            return items
        return self._read_namespace_items_legacy(namespace, limit=limit)

    def count_namespace_items(self, namespace: tuple[str, ...]) -> int:
        """Count records in one namespace.

        Input: namespace tuple.
        Output: integer count.
        """

        self.initialize()
        try:
            return self.store.count_namespace_items(namespace)
        except Exception:
            return len(self._read_namespace_items_legacy(namespace, limit=10_000))

    def list_agent_ids(self) -> list[str]:
        """List known agent ids from namespace storage.

        Input: none.
        Output: sorted agent id list.
        """

        self.initialize()
        ids: set[str] = set()
        try:
            for namespace in self.store.list_namespace_paths(prefix=("agent",)):
                if len(namespace) >= 2:
                    ids.add(str(namespace[1]))
        except Exception:
            pass
        if os.path.isdir(self.agent_root):
            for entry in os.listdir(self.agent_root):
                if os.path.isdir(os.path.join(self.agent_root, entry)):
                    ids.add(entry)
        return sorted(ids)

    def record_episode(self, agent_id: str, text: str, task_id: str | None = None, outcome: str | None = None) -> dict[str, Any]:
        """Append episodic event for an agent.

        Input: agent id, event text, optional task/outcome.
        Output: created event metadata.
        """

        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "text": text,
            "task_id": task_id,
            "outcome": outcome,
        }
        namespace = self._namespace_tuple(agent_id, "episodic")
        key = self.put_namespace_item(namespace, payload)
        return {"status": "ok", "namespace": namespace, "key": key, "path": "/".join((*namespace, key))}

    def remember_fact(self, agent_id: str, text: str, scope: str | None = None, importance: float = 0.5) -> dict[str, Any]:
        """Store semantic memory fact.

        Input: agent id, fact text, optional scope, importance score.
        Output: created fact metadata.
        """

        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "text": text,
            "scope": scope,
            "importance": float(importance),
        }
        namespace = self._namespace_tuple(agent_id, "semantic")
        key = self.put_namespace_item(namespace, payload)
        return {"status": "ok", "namespace": namespace, "key": key, "path": "/".join((*namespace, key))}

    def update_procedural_memory(self, agent_id: str, text: str, reason: str | None = None) -> dict[str, Any]:
        """Store procedural memory update.

        Input: agent id, instruction text, optional reason.
        Output: created record metadata.
        """

        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "text": text,
            "reason": reason,
        }
        namespace = self._namespace_tuple(agent_id, "procedural")
        key = self.put_namespace_item(namespace, payload)
        return {"status": "ok", "namespace": namespace, "key": key, "path": "/".join((*namespace, key))}

    def get_procedural_memory(self, agent_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """Read latest procedural memory records.

        Input: agent id and limit.
        Output: list of procedural records.
        """

        return self.read_namespace_items(self._namespace_tuple(agent_id, "procedural"), limit=limit)

    def remember_profile_fact(
        self,
        agent_id: str,
        profile_id: str,
        text: str,
        scope: str | None = None,
        importance: float = 0.5,
    ) -> dict[str, Any]:
        """Store a user/channel profile fact.

        Input: agent id, profile id, text, optional scope, importance.
        Output: write metadata.
        """

        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "text": text,
            "scope": scope,
            "importance": float(importance),
            "profile_id": profile_id,
        }
        namespace = self._namespace_tuple(agent_id, "profiles", profile_id)
        key = self.put_namespace_item(namespace, payload, mirror_jsonl=False)
        return {"status": "ok", "namespace": namespace, "key": key, "path": "/".join((*namespace, key))}

    def get_profile_memory(self, agent_id: str, profile_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """Read profile facts for one user/channel.

        Input: agent id, profile id, and limit.
        Output: list of profile facts.
        """

        return self.read_namespace_items(self._namespace_tuple(agent_id, "profiles", profile_id), limit=limit)

    def recall_memory(self, agent_id: str, query: str, scope: str | None = None, limit: int = 8) -> list[dict[str, Any]]:
        """Simple lexical recall from semantic and episodic namespaces.

        Input: agent id, query, optional scope, limit.
        Output: ranked list of memory items.
        """

        q = query.strip().lower()
        if not q:
            return []

        candidates = self.read_namespace_items(self._namespace_tuple(agent_id, "semantic"), limit=300)
        candidates += self.read_namespace_items(self._namespace_tuple(agent_id, "episodic"), limit=300)

        if scope:
            candidates = [x for x in candidates if str(x.get("scope", "")).strip() == scope]

        scored: list[dict[str, Any]] = []
        for item in candidates:
            txt = str(item.get("text", ""))
            if not txt:
                continue
            score = txt.lower().count(q)
            if score <= 0:
                continue
            scored.append({"score": score, **item})

        scored.sort(key=lambda x: x.get("score", 0), reverse=True)
        return scored[: max(1, int(limit))]

    def get_session_summary(self, agent_id: str, thread_id: str) -> dict[str, Any]:
        """Return one session summary record by thread id.

        Input: agent id and thread id.
        Output: matching summary or empty result.
        """

        items = self.read_namespace_items(self._namespace_tuple(agent_id, "summaries"), limit=500)
        for item in reversed(items):
            if str(item.get("thread_id", "")) == thread_id:
                return item
        return {}

    def get_agent_profile(self, agent_id: str) -> dict[str, Any]:
        """Return aggregate agent memory profile.

        Input: agent id.
        Output: counters and latest timestamps per namespace.
        """

        profile: dict[str, Any] = {"agent_id": agent_id, "namespaces": {}}
        for ns in ("semantic", "episodic", "procedural", "summaries"):
            items = self.read_namespace_items(self._namespace_tuple(agent_id, ns), limit=1000)
            profile["namespaces"][ns] = {
                "count": len(items),
                "latest_ts": items[-1].get("ts") if items else None,
            }
        profiles_root = self._namespace_tuple(agent_id, "profiles")
        profile["namespaces"]["profiles"] = {
            "count": self.count_namespace_items(profiles_root),
            "latest_ts": None,
        }
        return profile

    def _namespace_file(self, agent_id: str, namespace: str) -> str:
        """Build namespace file path.

        Input: agent id and namespace.
        Output: absolute jsonl file path.
        """

        safe_agent = "".join(ch for ch in agent_id if ch.isalnum() or ch in ("-", "_")) or "agent"
        safe_ns = "".join(ch for ch in namespace if ch.isalnum() or ch in ("-", "_")) or "data"
        p = Path(self.data_root) / "agent" / safe_agent
        p.mkdir(parents=True, exist_ok=True)
        return str(p / f"{safe_ns}.jsonl")

    def _safe_name(self, value: str) -> str:
        """Normalize a filesystem-safe name.

        Input: arbitrary string.
        Output: sanitized path segment.
        """

        return "".join(ch for ch in str(value) if ch.isalnum() or ch in ("-", "_")) or "item"

    def _corpus_card_path(self, corpus_id: str) -> str:
        """Resolve corpus card path.

        Input: corpus id.
        Output: card JSON path.
        """

        return str(Path(self.corpus_card_root) / f"{self._safe_name(corpus_id)}.json")

    def _append_namespace_item(self, agent_id: str, namespace: str, payload: dict[str, Any]) -> str:
        """Append one JSON line record.

        Input: namespace identifiers and payload.
        Output: file path where item was written.
        """

        path = self._namespace_file(agent_id, namespace)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=True) + "\n")
        return path

    def _read_namespace_items_legacy(self, namespace: tuple[str, ...], limit: int) -> list[dict[str, Any]]:
        """Read latest JSONL items from legacy files.

        Input: namespace tuple and max item count.
        Output: list of parsed records.
        """

        if len(namespace) < 3 or namespace[0] != "agent":
            return []
        agent_id = str(namespace[1])
        namespace_name = str(namespace[2])
        if len(namespace) == 4 and namespace_name == "profiles":
            namespace_name = f"profiles_{namespace[3]}"

        path = self._namespace_file(agent_id, namespace_name)
        if not os.path.exists(path):
            return []

        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        out: list[dict[str, Any]] = []
        for line in lines[-max(1, int(limit)) :]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out


_SERVICES: dict[tuple[str, int], MemoryService] = {}


def get_memory_service(root_dir: str, config: dict[str, Any]) -> MemoryService:
    """Return cached memory service for root+config pair.

    Input: root directory and app config.
    Output: initialized MemoryService instance.
    """

    key = (root_dir, id(config))
    svc = _SERVICES.get(key)
    if svc is None:
        svc = MemoryService(root_dir, config)
        _SERVICES[key] = svc
    return svc
