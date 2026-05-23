"""Typed schemas for memory and document operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class IngestJob:
    """Ingest job model.

    Input: database row fields.
    Output: normalized ingest job object.
    """

    job_id: str
    corpus_id: str
    source: dict[str, Any]
    title: str | None = None
    tags: list[str] = field(default_factory=list)
    requested_by: str | None = None
    status: str = "pending"


@dataclass
class SearchHit:
    """Search result item.

    Input: retrieval fields.
    Output: one ranked hit with snippet and score.
    """

    doc_id: str
    corpus_id: str
    version: int
    title: str | None
    snippet: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DocumentPayload:
    """Loaded source document content.

    Input: parsed source descriptor and config.
    Output: document text and source metadata.
    """

    text: str
    source_uri: str
    content_path: str | None = None
    title: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
