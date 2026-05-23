"""Project-local LangGraph checkpointer and store adapters."""

from __future__ import annotations

import json
import os
import pickle
from collections import defaultdict
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import MemorySaver

from memory.store_pg import MemoryStore


def _safe_name(value: str) -> str:
    """Normalize a filesystem-safe name.

    Input: arbitrary string.
    Output: sanitized path segment.
    """

    return "".join(ch for ch in str(value) if ch.isalnum() or ch in ("-", "_")) or "item"


def _namespace_path(root: str, namespace: tuple[str, ...]) -> Path:
    """Resolve namespace path under the runtime folder.

    Input: root path and namespace tuple.
    Output: namespace directory path.
    """

    parts = [_safe_name(part) for part in namespace if str(part).strip()]
    path = Path(root)
    for part in parts:
        path /= part
    return path


class ProjectCheckpointer(MemorySaver):
    """Filesystem-backed LangGraph checkpoint saver.

    Input: runtime root and app config.
    Output: durable checkpoint state for one graph instance.
    """

    def __init__(self, root_dir: str, config: dict[str, Any], graph_name: str):
        super().__init__()
        self.root_dir = root_dir
        self.config = config
        memory_path = str(config.get("memory", {}).get("path", "memory/runtime"))
        self.runtime_dir = os.path.join(root_dir, memory_path, "langgraph", "checkpoints")
        self.graph_name = _safe_name(graph_name)
        self.state_path = os.path.join(self.runtime_dir, f"{self.graph_name}.pkl")
        self._load()

    def _load(self) -> None:
        """Load persisted checkpoint state.

        Input: none.
        Output: in-memory dict state.
        """

        os.makedirs(self.runtime_dir, exist_ok=True)
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "rb") as handle:
                payload = pickle.load(handle)
            self.storage = self._restore_storage(payload.get("storage", {}))
            self.writes = self._to_defaultdict(payload.get("writes", {}))
            self.blobs = self._to_defaultdict(payload.get("blobs", {}))
            self.stack = self._to_defaultdict(payload.get("stack", {}))
        except Exception:
            return

    @classmethod
    def _restore_storage(cls, value: Any) -> defaultdict:
        """Restore MemorySaver storage with nested default dicts.

        Input: plain serialized storage payload.
        Output: defaultdict(thread_id -> defaultdict(checkpoint_ns -> data)).
        """

        restored: dict[Any, defaultdict] = {}
        if not isinstance(value, dict):
            return defaultdict(lambda: defaultdict(dict))

        for thread_id, namespace_map in value.items():
            if not isinstance(namespace_map, dict):
                restored[thread_id] = defaultdict(dict)
                continue
            restored[thread_id] = defaultdict(
                dict,
                {checkpoint_ns: cls._to_defaultdict(data) for checkpoint_ns, data in namespace_map.items()},
            )
        return defaultdict(lambda: defaultdict(dict), restored)

    def _save(self) -> None:
        """Persist checkpoint state to disk.

        Input: none.
        Output: checkpoint file refreshed.
        """

        os.makedirs(self.runtime_dir, exist_ok=True)
        payload = {
            "storage": self._to_plain(self.storage),
            "writes": self._to_plain(self.writes),
            "blobs": self._to_plain(self.blobs),
            "stack": self._to_plain(self.stack),
        }
        tmp_path = f"{self.state_path}.tmp"
        with open(tmp_path, "wb") as handle:
            pickle.dump(payload, handle)
        os.replace(tmp_path, self.state_path)

    @classmethod
    def _to_plain(cls, value: Any) -> Any:
        """Convert nested default dict structures to plain Python containers.

        Input: nested LangGraph checkpoint state.
        Output: pickle-friendly plain value.
        """

        if isinstance(value, defaultdict):
            return {key: cls._to_plain(child) for key, child in value.items()}
        if isinstance(value, dict):
            return {key: cls._to_plain(child) for key, child in value.items()}
        if isinstance(value, list):
            return [cls._to_plain(child) for child in value]
        if isinstance(value, tuple):
            return tuple(cls._to_plain(child) for child in value)
        return value

    @classmethod
    def _to_defaultdict(cls, value: Any) -> Any:
        """Rebuild nested default dict structures from plain containers.

        Input: plain checkpoint state.
        Output: defaultdict tree compatible with MemorySaver.
        """

        if isinstance(value, dict):
            return defaultdict(dict, {key: cls._to_defaultdict(child) for key, child in value.items()})
        if isinstance(value, list):
            return [cls._to_defaultdict(child) for child in value]
        if isinstance(value, tuple):
            return tuple(cls._to_defaultdict(child) for child in value)
        return value

    def put(self, *args, **kwargs):
        """Store one checkpoint and persist the saver.

        Input: LangGraph checkpoint arguments.
        Output: parent runnable config.
        """

        result = super().put(*args, **kwargs)
        self._save()
        return result

    def put_writes(self, *args, **kwargs):
        """Store one checkpoint write batch and persist the saver.

        Input: LangGraph write arguments.
        Output: none.
        """

        result = super().put_writes(*args, **kwargs)
        self._save()
        return result

    def delete_thread(self, *args, **kwargs):
        """Delete one thread and persist the saver.

        Input: LangGraph thread arguments.
        Output: deletion result.
        """

        result = super().delete_thread(*args, **kwargs)
        self._save()
        return result

    def delete_for_runs(self, *args, **kwargs):
        """Delete run-linked checkpoints and persist the saver.

        Input: LangGraph run arguments.
        Output: deletion result.
        """

        result = super().delete_for_runs(*args, **kwargs)
        self._save()
        return result

    def prune(self, *args, **kwargs):
        """Prune checkpoints and persist the saver.

        Input: prune arguments.
        Output: prune result.
        """

        result = super().prune(*args, **kwargs)
        self._save()
        return result

    async def aput(self, *args, **kwargs):
        """Async checkpoint put."""

        result = await super().aput(*args, **kwargs)
        self._save()
        return result

    async def aput_writes(self, *args, **kwargs):
        """Async checkpoint writes put."""

        result = await super().aput_writes(*args, **kwargs)
        self._save()
        return result

    async def adelete_thread(self, *args, **kwargs):
        """Async thread deletion."""

        result = await super().adelete_thread(*args, **kwargs)
        self._save()
        return result

    async def adelete_for_runs(self, *args, **kwargs):
        """Async run deletion."""

        result = await super().adelete_for_runs(*args, **kwargs)
        self._save()
        return result

    async def aprune(self, *args, **kwargs):
        """Async prune."""

        result = await super().aprune(*args, **kwargs)
        self._save()
        return result


class ProjectStore:
    """PostgreSQL-backed durable namespace store.

    Input: runtime root and app config.
    Output: LangGraph-compatible namespace CRUD/search adapter.
    """

    def __init__(self, root_dir: str, config: dict[str, Any], graph_name: str):
        self.root_dir = root_dir
        self.config = config
        self.graph_name = _safe_name(graph_name)
        self.runtime_store = MemoryStore(root_dir, config)
        self.runtime_store.ensure_schema()

    def get(self, namespace: tuple[str, ...], key: str, *, refresh_ttl: bool | None = None):
        """Load one stored item.

        Input: namespace and key.
        Output: stored item or None.
        """

        del refresh_ttl
        return self.runtime_store.get_namespace_item(namespace, key)

    def put(self, namespace: tuple[str, ...], key: str, value: dict[str, Any], index=None, *, ttl=None):
        """Store one item in a namespace.

        Input: namespace, key, and JSON-like value.
        Output: none.
        """

        del index, ttl
        self.runtime_store.put_namespace_item(namespace, key, value)

    def delete(self, namespace: tuple[str, ...], key: str) -> None:
        """Delete one stored item.

        Input: namespace and key.
        Output: none.
        """

        self.runtime_store.delete_namespace_item(namespace, key)

    def list_namespaces(self, *, prefix=None, suffix=None, max_depth=None, limit: int = 100, offset: int = 0):
        """List known namespaces.

        Input: optional namespace filters and paging.
        Output: namespace tuples.
        """

        del suffix, max_depth
        namespaces = self.runtime_store.list_namespace_paths(prefix=tuple(prefix or ()))
        return namespaces[offset : offset + max(1, int(limit))]

    def search(self, namespace_prefix: tuple[str, ...], /, *, query: str | None = None, filter: dict[str, Any] | None = None, limit: int = 10, offset: int = 0, refresh_ttl: bool | None = None):
        """Search items in namespaces.

        Input: namespace prefix, optional query/filter and paging.
        Output: list of matching items.
        """

        del refresh_ttl
        return self.runtime_store.search_namespace_items(
            namespace_prefix=tuple(namespace_prefix),
            query=query,
            filter=filter,
            limit=limit,
            offset=offset,
        )


def build_langgraph_runtime(root_dir: str, config: dict[str, Any], graph_name: str) -> tuple[ProjectCheckpointer, ProjectStore]:
    """Build durable LangGraph runtime adapters.

    Input: root directory, app config, and graph name.
    Output: checkpointer/store pair.
    """

    return ProjectCheckpointer(root_dir, config, graph_name), ProjectStore(root_dir, config, graph_name)
