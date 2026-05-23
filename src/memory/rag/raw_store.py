"""Immutable raw document storage for the memory RAG pipeline."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any


def _safe_name(value: str) -> str:
    """Normalize a filesystem-safe name.

    Input: arbitrary string.
    Output: sanitized path segment.
    """

    return "".join(ch for ch in str(value) if ch.isalnum() or ch in ("-", "_")) or "item"


def raw_document_dir(root_dir: str, corpus_id: str, doc_id: str, version: int) -> str:
    """Resolve raw document directory path.

    Input: project root, corpus id, doc id, and version.
    Output: absolute storage directory path.
    """

    return str(Path(root_dir) / "data" / "documents" / _safe_name(corpus_id) / _safe_name(doc_id) / str(int(version)))


def save_raw_document(
    root_dir: str,
    corpus_id: str,
    doc_id: str,
    version: int,
    text: str,
    source: dict[str, Any],
    source_path: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Persist immutable raw document artifacts.

    Input: document identifiers, extracted text, and source metadata.
    Output: saved artifact paths.
    """

    folder = Path(raw_document_dir(root_dir, corpus_id, doc_id, version))
    folder.mkdir(parents=True, exist_ok=True)

    text_path = folder / "content.txt"
    meta_path = folder / "source.json"
    with open(text_path, "w", encoding="utf-8") as handle:
        handle.write(text)

    payload = {
        "corpus_id": corpus_id,
        "doc_id": doc_id,
        "version": int(version),
        "source": source,
        "source_path": source_path,
        "metadata": metadata or {},
    }
    with open(meta_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)

    if source_path and os.path.exists(source_path) and os.path.isfile(source_path):
        copied_name = _safe_name(Path(source_path).name) or "source"
        shutil.copy2(source_path, folder / copied_name)

    return {"raw_dir": str(folder), "content_path": str(text_path), "source_path": str(meta_path)}
