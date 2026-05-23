"""Retrieval helpers for hybrid RAG surface."""

from __future__ import annotations

from typing import Any


def normalize_search_hits(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize retrieval rows into API payload.

    Input: rows from store.search_chunks.
    Output: list of serializable search hits.
    """

    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "doc_id": row.get("doc_id"),
                "corpus_id": row.get("corpus_id"),
                "version": int(row.get("version") or 1),
                "title": row.get("title"),
                "snippet": row.get("snippet") or "",
                "score": float(row.get("score") or 0.0),
                "lexical_score": float(row.get("lexical_score") or 0.0),
                "dense_score": float(row.get("dense_score") or 0.0),
                "chunk_index": int(row.get("chunk_index") or 0),
            }
        )
    return out
