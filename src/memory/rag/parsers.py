"""Document source parsers for ingest pipeline."""

from __future__ import annotations

import os
from typing import Any

from memory.schemas import DocumentPayload


def _safe_join(root: str, rel_path: str) -> str:
    """Build normalized path under root.

    Input: storage root and user relative path.
    Output: absolute safe path or ValueError.
    """

    root_abs = os.path.abspath(root)
    target = os.path.abspath(os.path.join(root_abs, rel_path))
    if not target.startswith(root_abs + os.sep) and target != root_abs:
        raise ValueError("path escapes storage root")
    return target


def _read_text_file(path: str) -> str:
    """Read utf-8 text file with replacement.

    Input: absolute file path.
    Output: decoded text.
    """

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _storage_map(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return configured external document storages.

    Input: app config.
    Output: storage_id -> storage config mapping.
    """

    memory_cfg = config.get("memory", {})
    storages = memory_cfg.get("document_storages", {}).get("items", {})
    return storages if isinstance(storages, dict) else {}


def load_document_source(root_dir: str, config: dict[str, Any], source: dict[str, Any]) -> DocumentPayload:
    """Resolve source descriptor and return text payload.

    Input: root dir, app config, and source descriptor.
    Output: parsed document payload.
    """

    src_type = str(source.get("type", "")).strip().lower()
    title = source.get("title")
    metadata = dict(source.get("metadata", {}) or {})

    if src_type == "text":
        text = str(source.get("text", ""))
        if not text.strip():
            raise ValueError("empty text source")
        return DocumentPayload(text=text, source_uri="inline://text", title=title, metadata=metadata)

    if src_type == "path":
        raw_path = str(source.get("path", "")).strip()
        if not raw_path:
            raise ValueError("path source requires 'path'")
        abs_path = raw_path if os.path.isabs(raw_path) else os.path.abspath(os.path.join(root_dir, raw_path))
        if not os.path.exists(abs_path):
            raise ValueError(f"source file does not exist: {abs_path}")
        text = _read_text_file(abs_path)
        return DocumentPayload(
            text=text,
            source_uri=f"file://{abs_path}",
            content_path=abs_path,
            title=title,
            metadata=metadata,
        )

    if src_type == "storage_path":
        storage_id = str(source.get("storage_id", "")).strip()
        rel_path = str(source.get("path", "")).strip()
        if not storage_id or not rel_path:
            raise ValueError("storage_path requires storage_id and path")

        storages = _storage_map(config)
        storage = storages.get(storage_id)
        if not storage:
            raise ValueError(f"unknown storage_id: {storage_id}")
        if storage.get("type") != "filesystem":
            raise ValueError("only filesystem storage type is supported in phase A")

        storage_root = str(storage.get("root", "")).strip()
        if not storage_root:
            raise ValueError("storage root is empty")

        abs_path = _safe_join(storage_root, rel_path)
        if not os.path.exists(abs_path):
            raise ValueError(f"storage file does not exist: {abs_path}")

        text = _read_text_file(abs_path)
        return DocumentPayload(
            text=text,
            source_uri=f"storage://{storage_id}/{rel_path}",
            content_path=abs_path,
            title=title,
            metadata={**metadata, "storage_id": storage_id, "storage_path": rel_path},
        )

    raise ValueError(f"unsupported source type: {src_type!r}")
