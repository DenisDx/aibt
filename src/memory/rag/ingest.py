"""Ingest pipeline for document indexing."""

from __future__ import annotations

import hashlib
from typing import Any

from memory.rag.parsers import load_document_source
from memory.rag.raw_store import save_raw_document
from memory.rag.summaries import make_summary


def chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Split text into overlapping chunks.

    Input: text, chunk size, overlap size.
    Output: ordered chunk list.
    """

    normalized = " ".join((text or "").split())
    if not normalized:
        return []

    size = max(200, int(chunk_size))
    ov = max(0, min(size // 2, int(overlap)))
    step = size - ov if size > ov else size

    chunks: list[str] = []
    start = 0
    while start < len(normalized):
        chunk = normalized[start : start + size].strip()
        if chunk:
            chunks.append(chunk)
        start += step
    return chunks


def _derive_doc_id(corpus_id: str, source_uri: str, text: str) -> str:
    """Derive deterministic document id.

    Input: corpus id, source URI, and source text.
    Output: stable document identifier.
    """

    text_hash = hashlib.sha1(text.encode("utf-8")).hexdigest()
    h = hashlib.sha1(f"{corpus_id}:{source_uri}:{text_hash}".encode("utf-8")).hexdigest()
    return h[:32]


def process_ingest_job(root_dir: str, config: dict[str, Any], store, job: dict[str, Any]) -> tuple[str, int]:
    """Process one pending ingest job.

    Input: root, config, store, job record.
    Output: tuple of (doc_id, version).
    """

    source = dict(job.get("source") or {})
    corpus_id = str(job.get("corpus_id", "")).strip()
    if not corpus_id:
        raise ValueError("job has empty corpus_id")

    payload = load_document_source(root_dir, config, source)
    checksum = hashlib.sha256(payload.text.encode("utf-8")).hexdigest()
    doc_id = str(source.get("doc_id") or _derive_doc_id(corpus_id, payload.source_uri, payload.text))
    title = str(job.get("title") or payload.title or source.get("title") or doc_id)
    tags = list(job.get("tags") or [])
    summary = make_summary(payload.text)

    store.ensure_corpus(corpus_id)
    latest = store.get_latest_version_info(doc_id)
    if latest and str(latest.get("checksum") or "") == checksum:
        return doc_id, int(latest.get("version") or 1)

    source_meta = {"source_uri": payload.source_uri, "type": source.get("type", "unknown")}
    source_meta.update(payload.metadata)

    raw_artifacts = save_raw_document(
        root_dir=root_dir,
        corpus_id=corpus_id,
        doc_id=doc_id,
        version=(int(latest.get("version") or 0) + 1) if latest else 1,
        text=payload.text,
        source=source_meta,
        source_path=payload.content_path,
        metadata=payload.metadata,
    )
    source_meta.update(raw_artifacts)

    store.upsert_document(
        doc_id=doc_id,
        corpus_id=corpus_id,
        title=title,
        source=source_meta,
        tags=tags,
        checksum=checksum,
        metadata={"ingest": "phase_a"},
    )

    version = store.insert_document_version(
        doc_id=doc_id,
        content_path=raw_artifacts["content_path"],
        content_text=payload.text,
        content_summary=summary,
        metadata={**payload.metadata, **raw_artifacts},
    )

    ingest_cfg = config.get("memory", {}).get("rag", {}).get("ingest", {})
    chunks = chunk_text(
        payload.text,
        chunk_size=int(ingest_cfg.get("chunk_size", 1200)),
        overlap=int(ingest_cfg.get("chunk_overlap", 120)),
    )
    store.replace_chunks(doc_id, version, chunks)
    return doc_id, version
