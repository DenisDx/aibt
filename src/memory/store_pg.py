"""PostgreSQL-backed registry for memory and RAG indexes."""

from __future__ import annotations

from contextlib import contextmanager
import json
from typing import Any, Iterator

from memory.rag.embeddings import (
    DEFAULT_EMBEDDING_DIM,
    cosine_similarity,
    embedding_to_vector_literal,
    text_to_embedding,
)

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Json
except Exception:  # pragma: no cover - runtime dependency guard
    psycopg = None
    dict_row = None
    Json = None


class MemoryStore:
    """Database registry manager.

    Input: root directory and app config.
    Output: schema management and CRUD methods for memory/RAG data.
    """

    def __init__(self, root_dir: str, config: dict[str, Any]):
        if psycopg is None:
            raise RuntimeError("psycopg is required for memory store")
        self.root_dir = root_dir
        self.config = config
        self.embedding_dim = int(self.config.get("memory", {}).get("rag", {}).get("embedding_dim", DEFAULT_EMBEDDING_DIM))
        self._vector_enabled = False

    def _db_cfg(self) -> dict[str, Any]:
        """Return normalized database config.

        Input: app config.
        Output: psycopg connection kwargs.
        """

        db = self.config.get("database", {})
        return {
            "host": db.get("host", "127.0.0.1"),
            "port": int(db.get("port", 5432)),
            "user": db.get("user", "aibt"),
            "password": db.get("password", ""),
            "dbname": db.get("db", "aibt"),
        }

    @contextmanager
    def _conn(self) -> Iterator[Any]:
        """Open short-lived DB connection.

        Input: none.
        Output: context with psycopg connection.
        """

        conn = psycopg.connect(**self._db_cfg(), autocommit=True, row_factory=dict_row)
        try:
            yield conn
        finally:
            conn.close()

    def ensure_schema(self) -> None:
        """Create required tables and indexes.

        Input: none.
        Output: guaranteed schema for memory/RAG operations.
        """

        with self._conn() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                    self._vector_enabled = True
                except Exception:
                    self._vector_enabled = False

                embedding_col = f"embedding vector({self.embedding_dim})" if self._vector_enabled else "embedding double precision[]"

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS corpora (
                      corpus_id TEXT PRIMARY KEY,
                      title TEXT,
                      metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                      updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS documents (
                      doc_id TEXT PRIMARY KEY,
                      corpus_id TEXT NOT NULL REFERENCES corpora(corpus_id),
                      title TEXT,
                      source JSONB NOT NULL,
                      checksum TEXT,
                      tags JSONB NOT NULL DEFAULT '[]'::jsonb,
                      metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                      deleted BOOLEAN NOT NULL DEFAULT FALSE,
                      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                      updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS document_versions (
                      id BIGSERIAL PRIMARY KEY,
                      doc_id TEXT NOT NULL REFERENCES documents(doc_id),
                      version INTEGER NOT NULL,
                      content_path TEXT,
                      content_text TEXT NOT NULL,
                      content_summary TEXT,
                      metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                      UNIQUE(doc_id, version)
                    )
                    """
                )
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS chunks (
                      id BIGSERIAL PRIMARY KEY,
                      doc_id TEXT NOT NULL REFERENCES documents(doc_id),
                      version INTEGER NOT NULL,
                      chunk_index INTEGER NOT NULL,
                      content TEXT NOT NULL,
                      {embedding_col},
                      fts TSVECTOR GENERATED ALWAYS AS (to_tsvector('simple', coalesce(content, ''))) STORED,
                      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                      UNIQUE(doc_id, version, chunk_index)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS chunk_links (
                      id BIGSERIAL PRIMARY KEY,
                      doc_id TEXT NOT NULL,
                      version INTEGER NOT NULL,
                      chunk_index INTEGER NOT NULL,
                      linked_doc_id TEXT NOT NULL,
                      linked_version INTEGER NOT NULL,
                      linked_chunk_index INTEGER NOT NULL,
                      relation TEXT NOT NULL,
                      score DOUBLE PRECISION NOT NULL DEFAULT 1.0,
                      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                      UNIQUE(doc_id, version, chunk_index, linked_doc_id, linked_version, linked_chunk_index, relation)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ingest_jobs (
                      job_id TEXT PRIMARY KEY,
                      corpus_id TEXT NOT NULL,
                      source JSONB NOT NULL,
                      title TEXT,
                      tags JSONB NOT NULL DEFAULT '[]'::jsonb,
                      requested_by TEXT,
                      status TEXT NOT NULL,
                      attempts INTEGER NOT NULL DEFAULT 0,
                      error TEXT,
                      document_id TEXT,
                      version INTEGER,
                      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                      updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS agent_memory_items (
                      namespace TEXT[] NOT NULL,
                      key TEXT NOT NULL,
                      value JSONB NOT NULL,
                      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                      PRIMARY KEY(namespace, key)
                    )
                    """
                )
                cur.execute("CREATE INDEX IF NOT EXISTS idx_chunks_fts ON chunks USING GIN (fts)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_chunk_links_doc ON chunk_links(doc_id, version, chunk_index)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_chunk_links_target ON chunk_links(linked_doc_id, linked_version, linked_chunk_index)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_ingest_jobs_status ON ingest_jobs(status, created_at)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_agent_memory_namespace ON agent_memory_items USING GIN (namespace)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_agent_memory_updated ON agent_memory_items(updated_at DESC)")

    def ensure_corpus(self, corpus_id: str, title: str | None = None) -> None:
        """Create or update corpus metadata.

        Input: corpus id and optional title.
        Output: persisted corpus record.
        """

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO corpora(corpus_id, title)
                    VALUES (%s, %s)
                    ON CONFLICT(corpus_id) DO UPDATE
                      SET title = COALESCE(EXCLUDED.title, corpora.title),
                          updated_at = now()
                    """,
                    (corpus_id, title),
                )

    def create_ingest_job(
        self,
        job_id: str,
        corpus_id: str,
        source: dict[str, Any],
        title: str | None,
        tags: list[str],
        requested_by: str | None,
    ) -> None:
        """Insert new ingest job.

        Input: job fields.
        Output: new pending record.
        """

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ingest_jobs(job_id, corpus_id, source, title, tags, requested_by, status)
                    VALUES (%s, %s, %s, %s, %s, %s, 'pending')
                    """,
                    (job_id, corpus_id, Json(source), title, Json(tags), requested_by),
                )

    def fetch_pending_jobs(self, limit: int) -> list[dict[str, Any]]:
        """Fetch pending jobs in FIFO order.

        Input: max number of jobs.
        Output: list of job dicts.
        """

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT job_id, corpus_id, source, title, tags, requested_by, attempts
                    FROM ingest_jobs
                    WHERE status = 'pending'
                    ORDER BY created_at ASC
                    LIMIT %s
                    """,
                    (max(1, int(limit)),),
                )
                return list(cur.fetchall())

    def mark_job_running(self, job_id: str) -> None:
        """Set job status to running.

        Input: job id.
        Output: status update.
        """

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE ingest_jobs SET status='running', attempts=attempts+1, updated_at=now() WHERE job_id=%s",
                    (job_id,),
                )

    def mark_job_done(self, job_id: str, doc_id: str, version: int) -> None:
        """Set job status to done.

        Input: job id, document id, version.
        Output: final status update.
        """

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE ingest_jobs
                    SET status='done', document_id=%s, version=%s, error=NULL, updated_at=now()
                    WHERE job_id=%s
                    """,
                    (doc_id, version, job_id),
                )

    def mark_job_error(self, job_id: str, error: str) -> None:
        """Set job status to error.

        Input: job id and error message.
        Output: failed status update.
        """

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE ingest_jobs SET status='error', error=%s, updated_at=now() WHERE job_id=%s",
                    (error[:4000], job_id),
                )

    def get_latest_version_info(self, doc_id: str) -> dict[str, Any] | None:
        """Get latest known version and checksum.

        Input: document id.
        Output: dict with version/checksum or None.
        """

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT d.checksum, dv.version
                    FROM documents d
                    LEFT JOIN LATERAL (
                      SELECT version FROM document_versions
                      WHERE doc_id = d.doc_id
                      ORDER BY version DESC
                      LIMIT 1
                    ) dv ON TRUE
                    WHERE d.doc_id=%s
                    """,
                    (doc_id,),
                )
                row = cur.fetchone()
                return dict(row) if row else None

    def upsert_document(
        self,
        doc_id: str,
        corpus_id: str,
        title: str | None,
        source: dict[str, Any],
        tags: list[str],
        checksum: str,
        metadata: dict[str, Any],
    ) -> None:
        """Create or update document row.

        Input: document fields.
        Output: upserted document.
        """

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO documents(doc_id, corpus_id, title, source, checksum, tags, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT(doc_id) DO UPDATE
                      SET corpus_id=EXCLUDED.corpus_id,
                          title=COALESCE(EXCLUDED.title, documents.title),
                          source=EXCLUDED.source,
                          checksum=EXCLUDED.checksum,
                          tags=EXCLUDED.tags,
                          metadata=EXCLUDED.metadata,
                          deleted=FALSE,
                          updated_at=now()
                    """,
                    (doc_id, corpus_id, title, Json(source), checksum, Json(tags), Json(metadata)),
                )

    def insert_document_version(
        self,
        doc_id: str,
        content_path: str | None,
        content_text: str,
        content_summary: str,
        metadata: dict[str, Any],
    ) -> int:
        """Append new document version.

        Input: version content payload.
        Output: created version number.
        """

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 AS next_version FROM document_versions WHERE doc_id=%s",
                    (doc_id,),
                )
                next_version = int(cur.fetchone()["next_version"])
                cur.execute(
                    """
                    INSERT INTO document_versions(doc_id, version, content_path, content_text, content_summary, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (doc_id, next_version, content_path, content_text, content_summary, Json(metadata)),
                )
                return next_version

    def replace_chunks(self, doc_id: str, version: int, chunks: list[str]) -> None:
        """Replace chunk rows for a document version.

        Input: document id, version, and chunk list.
        Output: rewritten chunk set with embeddings.
        """

        embeddings = [text_to_embedding(chunk, self.embedding_dim) for chunk in chunks]

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM chunks WHERE doc_id=%s AND version=%s", (doc_id, version))
                cur.execute("DELETE FROM chunk_links WHERE doc_id=%s AND version=%s", (doc_id, version))
                for idx, chunk in enumerate(chunks):
                    embedding = embeddings[idx] if idx < len(embeddings) else text_to_embedding(chunk, self.embedding_dim)
                    cur.execute(
                        """
                        INSERT INTO chunks(doc_id, version, chunk_index, content, embedding)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            doc_id,
                            version,
                            idx,
                            chunk,
                            embedding_to_vector_literal(embedding) if self._vector_enabled else embedding,
                        ),
                    )

                for idx in range(len(chunks) - 1):
                    cur.execute(
                        """
                        INSERT INTO chunk_links(doc_id, version, chunk_index, linked_doc_id, linked_version, linked_chunk_index, relation, score)
                        VALUES (%s, %s, %s, %s, %s, %s, 'next', 1.0)
                        ON CONFLICT DO NOTHING
                        """,
                        (doc_id, version, idx, doc_id, version, idx + 1),
                    )
                    cur.execute(
                        """
                        INSERT INTO chunk_links(doc_id, version, chunk_index, linked_doc_id, linked_version, linked_chunk_index, relation, score)
                        VALUES (%s, %s, %s, %s, %s, %s, 'prev', 1.0)
                        ON CONFLICT DO NOTHING
                        """,
                        (doc_id, version, idx + 1, doc_id, version, idx),
                    )

    def _latest_chunk_rows(self, corpora: list[str] | None = None, with_embedding: bool = False) -> list[dict[str, Any]]:
        """Fetch latest chunk rows for retrieval.

        Input: optional corpus filter and embedding flag.
        Output: chunk rows.
        """

        select_embedding = ", c.embedding" if with_embedding else ""
        where_clause = "WHERE NOT d.deleted"
        params: list[Any] = []
        if corpora:
            where_clause += " AND d.corpus_id = ANY(%s)"
            params.append(corpora)

        sql = f"""
            WITH latest AS (
              SELECT doc_id, MAX(version) AS version
              FROM document_versions
              GROUP BY doc_id
            )
            SELECT d.doc_id,
                   d.corpus_id,
                   d.title,
                   c.version,
                   c.chunk_index,
                   c.content,
                   LEFT(c.content, 280) AS snippet,
                   d.updated_at{select_embedding}
            FROM chunks c
            JOIN latest l ON c.doc_id=l.doc_id AND c.version=l.version
            JOIN documents d ON d.doc_id=c.doc_id
            {where_clause}
        """

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                return list(cur.fetchall())

    def search_chunks_lexical(self, query: str, corpora: list[str] | None, limit: int) -> list[dict[str, Any]]:
        """Run lexical retrieval over latest chunks.

        Input: query text, optional corpora filter, and limit.
        Output: ranked lexical retrieval rows.
        """

        if not query.strip():
            return []

        where_clause = "WHERE NOT d.deleted AND c.fts @@ plainto_tsquery('simple', %s)"
        if corpora:
            where_clause += " AND d.corpus_id = ANY(%s)"
            params: list[Any] = [query, query, corpora, max(1, int(limit))]
        else:
            params = [query, query, max(1, int(limit))]

        sql = f"""
            WITH latest AS (
              SELECT doc_id, MAX(version) AS version
              FROM document_versions
              GROUP BY doc_id
            )
            SELECT d.doc_id,
                   d.corpus_id,
                   d.title,
                   c.version,
                   c.chunk_index,
                   LEFT(c.content, 280) AS snippet,
                   ts_rank(c.fts, plainto_tsquery('simple', %s)) AS score
            FROM chunks c
            JOIN latest l ON c.doc_id=l.doc_id AND c.version=l.version
            JOIN documents d ON d.doc_id=c.doc_id
            {where_clause}
            ORDER BY score DESC, d.updated_at DESC
            LIMIT %s
        """

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                return list(cur.fetchall())

    def search_chunks_dense(self, query: str, corpora: list[str] | None, limit: int) -> list[dict[str, Any]]:
        """Run dense retrieval over latest chunks.

        Input: query text, optional corpora filter, and limit.
        Output: ranked dense retrieval rows.
        """

        if not query.strip():
            return []

        query_embedding = text_to_embedding(query, self.embedding_dim)
        safe_limit = max(1, int(limit))

        if self._vector_enabled:
            where_clause = "WHERE NOT d.deleted"
            params: list[Any] = [embedding_to_vector_literal(query_embedding), self.embedding_dim]
            if corpora:
                where_clause += " AND d.corpus_id = ANY(%s)"
                params.append(corpora)

            sql = f"""
                WITH latest AS (
                  SELECT doc_id, MAX(version) AS version
                  FROM document_versions
                  GROUP BY doc_id
                )
                SELECT d.doc_id,
                       d.corpus_id,
                       d.title,
                       c.version,
                       c.chunk_index,
                       LEFT(c.content, 280) AS snippet,
                       1.0 - (c.embedding <=> %s::vector(%s)) AS score
                FROM chunks c
                JOIN latest l ON c.doc_id=l.doc_id AND c.version=l.version
                JOIN documents d ON d.doc_id=c.doc_id
                {where_clause}
                ORDER BY c.embedding <=> %s::vector(%s)
                LIMIT %s
            """
            params.extend([embedding_to_vector_literal(query_embedding), self.embedding_dim, safe_limit])
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, tuple(params))
                    return list(cur.fetchall())

        rows = self._latest_chunk_rows(corpora=corpora, with_embedding=True)
        scored: list[dict[str, Any]] = []
        for row in rows:
            embedding = row.get("embedding") or []
            if isinstance(embedding, str):
                try:
                    embedding = json.loads(embedding)
                except Exception:
                    embedding = []
            score = cosine_similarity(query_embedding, [float(v) for v in embedding])
            scored.append({**row, "score": score})
        scored.sort(key=lambda item: item.get("score", 0.0), reverse=True)
        return scored[:safe_limit]

    @staticmethod
    def _rrf_merge(
        lexical_rows: list[dict[str, Any]],
        dense_rows: list[dict[str, Any]],
        limit: int,
        lexical_weight: float,
        dense_weight: float,
    ) -> list[dict[str, Any]]:
        """Merge lexical and dense rows using reciprocal rank fusion.

        Input: ranked lexical and dense rows.
        Output: combined ranking.
        """

        k = 60.0
        merged: dict[tuple[str, int, int], dict[str, Any]] = {}

        def key(row: dict[str, Any]) -> tuple[str, int, int]:
            return (str(row.get("doc_id", "")), int(row.get("version") or 0), int(row.get("chunk_index") or 0))

        def add(rows: list[dict[str, Any]], weight: float, source: str) -> None:
            for rank, row in enumerate(rows, start=1):
                item_key = key(row)
                item = merged.setdefault(item_key, {**row, "lexical_score": 0.0, "dense_score": 0.0})
                item[f"{source}_score"] = float(row.get("score") or 0.0)
                item["score"] = float(item.get("score", 0.0)) + weight / (k + rank)

        add(lexical_rows, lexical_weight, "lexical")
        add(dense_rows, dense_weight, "dense")

        out = list(merged.values())
        out.sort(
            key=lambda item: (
                float(item.get("score", 0.0)),
                float(item.get("lexical_score", 0.0)),
                float(item.get("dense_score", 0.0)),
            ),
            reverse=True,
        )
        return out[: max(1, int(limit))]

    def search_chunks(self, query: str, corpora: list[str] | None, limit: int) -> list[dict[str, Any]]:
        """Run hybrid retrieval over latest chunks.

        Input: query text, optional corpora filter, and limit.
        Output: ranked hybrid retrieval rows.
        """

        if not query.strip():
            return []

        retrieval_cfg = self.config.get("memory", {}).get("rag", {}).get("retrieval", {})
        lexical_weight = float(retrieval_cfg.get("lexical_weight", 0.45))
        dense_weight = float(retrieval_cfg.get("dense_weight", 0.55))
        lexical_top_k = max(1, int(retrieval_cfg.get("lexical_top_k", max(10, int(limit) * 4))))
        dense_top_k = max(1, int(retrieval_cfg.get("dense_top_k", max(10, int(limit) * 4))))

        lexical_rows = self.search_chunks_lexical(query, corpora, lexical_top_k)
        dense_rows = self.search_chunks_dense(query, corpora, dense_top_k)
        return self._rrf_merge(
            lexical_rows,
            dense_rows,
            limit=limit,
            lexical_weight=lexical_weight,
            dense_weight=dense_weight,
        )

    def list_corpora(self) -> list[dict[str, Any]]:
        """List available corpora.

        Input: none.
        Output: corpus list.
        """

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT corpus_id, title, metadata, created_at, updated_at
                    FROM corpora
                    ORDER BY updated_at DESC, corpus_id ASC
                    """
                )
                return list(cur.fetchall())

    def list_documents(
        self,
        corpus_id: str,
        limit: int = 50,
        offset: int = 0,
        query: str | None = None,
        tag: str | None = None,
        sort_by: str | None = None,
        sort_dir: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """List documents in one corpus with pagination and filters.

        Input: corpus id, page args, optional query and tag.
        Output: document rows and total count.
        """

        safe_limit = max(1, int(limit))
        safe_offset = max(0, int(offset))
        sort_key = str(sort_by or "updated_at").strip().lower()
        sort_order = str(sort_dir or "desc").strip().lower()
        if sort_order not in ("asc", "desc"):
            sort_order = "desc"

        where_parts = ["d.corpus_id = %s", "NOT d.deleted"]
        params: list[Any] = [corpus_id]
        if query and query.strip():
            where_parts.append("(d.doc_id ILIKE %s OR d.title ILIKE %s OR v.content_summary ILIKE %s)")
            q = f"%{query.strip()}%"
            params.extend([q, q, q])
        if tag and tag.strip():
            where_parts.append("d.tags @> %s::jsonb")
            params.append(json.dumps([tag.strip()]))

        where_sql = " AND ".join(where_parts)
        order_map = {
            "updated_at": "d.updated_at",
            "title": "d.title",
            "doc_id": "d.doc_id",
            "version": "v.version",
        }
        order_col = order_map.get(sort_key, "d.updated_at")

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT COUNT(*) AS total
                    FROM documents d
                    LEFT JOIN LATERAL (
                      SELECT version, content_summary
                      FROM document_versions
                      WHERE doc_id = d.doc_id
                      ORDER BY version DESC
                      LIMIT 1
                    ) v ON TRUE
                    WHERE {where_sql}
                    """,
                    tuple(params),
                )
                total = int(cur.fetchone()["total"])

                cur.execute(
                    f"""
                    SELECT d.doc_id, d.corpus_id, d.title, d.tags, d.updated_at,
                           v.version, v.content_summary
                    FROM documents d
                    LEFT JOIN LATERAL (
                      SELECT version, content_summary
                      FROM document_versions
                      WHERE doc_id = d.doc_id
                      ORDER BY version DESC
                      LIMIT 1
                    ) v ON TRUE
                    WHERE {where_sql}
                    ORDER BY {order_col} {sort_order}, d.updated_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    tuple([*params, safe_limit, safe_offset]),
                )
                return list(cur.fetchall()), total

    def get_document_latest(self, doc_id: str) -> dict[str, Any] | None:
        """Fetch latest document version payload.

        Input: document id.
        Output: row with source/content or None.
        """

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT d.doc_id, d.corpus_id, d.title, d.source, d.tags, d.metadata,
                           v.version, v.content_path, v.content_text, v.content_summary, v.created_at
                    FROM documents d
                    LEFT JOIN LATERAL (
                      SELECT version, content_path, content_text, content_summary, created_at
                      FROM document_versions
                      WHERE doc_id = d.doc_id
                      ORDER BY version DESC
                      LIMIT 1
                    ) v ON TRUE
                    WHERE d.doc_id=%s AND NOT d.deleted
                    """,
                    (doc_id,),
                )
                row = cur.fetchone()
                return dict(row) if row else None

    def get_document_version(self, doc_id: str, version: int) -> dict[str, Any] | None:
        """Fetch one specific document version payload.

        Input: document id and version number.
        Output: row with source/content or None.
        """

        safe_version = int(version)
        if safe_version <= 0:
            return None

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT d.doc_id, d.corpus_id, d.title, d.source, d.tags, d.metadata,
                           v.version, v.content_path, v.content_text, v.content_summary, v.created_at
                    FROM documents d
                    JOIN document_versions v ON v.doc_id = d.doc_id
                    WHERE d.doc_id=%s AND v.version=%s AND NOT d.deleted
                    """,
                    (doc_id, safe_version),
                )
                row = cur.fetchone()
                return dict(row) if row else None

    def soft_delete_document(self, doc_id: str) -> bool:
        """Soft-delete a document.

        Input: document id.
        Output: true if row was updated.
        """

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE documents SET deleted=TRUE, updated_at=now() WHERE doc_id=%s", (doc_id,))
                return cur.rowcount > 0

    @staticmethod
    def _normalize_namespace(namespace: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
        """Normalize a namespace path.

        Input: tuple/list namespace parts.
        Output: cleaned namespace tuple.
        """

        return tuple(str(part).strip() for part in (namespace or ()) if str(part).strip())

    @staticmethod
    def _normalize_value_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Normalize namespace rows for Python callers.

        Input: raw database rows.
        Output: rows with tuple namespace values.
        """

        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            namespace = item.get("namespace")
            if isinstance(namespace, list):
                item["namespace"] = tuple(namespace)
            out.append(item)
        return out

    def put_namespace_item(self, namespace: tuple[str, ...], key: str, value: dict[str, Any]) -> None:
        """Upsert one namespace item.

        Input: namespace, key, and JSON value.
        Output: persisted record.
        """

        ns = self._normalize_namespace(namespace)
        clean_key = str(key or "").strip()
        if not ns:
            raise ValueError("namespace is required")
        if not clean_key:
            raise ValueError("key is required")

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO agent_memory_items(namespace, key, value)
                    VALUES (%s, %s, %s)
                    ON CONFLICT(namespace, key) DO UPDATE
                      SET value = EXCLUDED.value,
                          updated_at = now()
                    """,
                    (list(ns), clean_key, Json(value)),
                )

    def get_namespace_item(self, namespace: tuple[str, ...], key: str) -> dict[str, Any] | None:
        """Load one namespace item.

        Input: namespace and key.
        Output: stored row or None.
        """

        ns = self._normalize_namespace(namespace)
        clean_key = str(key or "").strip()
        if not ns or not clean_key:
            return None

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT namespace, key, value, created_at, updated_at
                    FROM agent_memory_items
                    WHERE namespace=%s AND key=%s
                    """,
                    (list(ns), clean_key),
                )
                row = cur.fetchone()
                return dict(row) if row else None

    def delete_namespace_item(self, namespace: tuple[str, ...], key: str) -> bool:
        """Delete one namespace item.

        Input: namespace and key.
        Output: deletion flag.
        """

        ns = self._normalize_namespace(namespace)
        clean_key = str(key or "").strip()
        if not ns or not clean_key:
            return False

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM agent_memory_items WHERE namespace=%s AND key=%s",
                    (list(ns), clean_key),
                )
                return cur.rowcount > 0

    def list_namespace_items(
        self,
        namespace_prefix: tuple[str, ...],
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List items in namespaces under a prefix.

        Input: namespace prefix and paging.
        Output: ordered namespace rows.
        """

        prefix = self._normalize_namespace(namespace_prefix)
        safe_limit = max(1, int(limit))
        safe_offset = max(0, int(offset))

        with self._conn() as conn:
            with conn.cursor() as cur:
                if prefix:
                    cur.execute(
                        """
                        SELECT namespace, key, value, created_at, updated_at
                        FROM agent_memory_items
                        WHERE cardinality(namespace) >= %s
                          AND namespace[1:%s] = %s
                        ORDER BY updated_at DESC, created_at DESC, key ASC
                        LIMIT %s OFFSET %s
                        """,
                        (len(prefix), len(prefix), list(prefix), safe_limit, safe_offset),
                    )
                else:
                    cur.execute(
                        """
                        SELECT namespace, key, value, created_at, updated_at
                        FROM agent_memory_items
                        ORDER BY updated_at DESC, created_at DESC, key ASC
                        LIMIT %s OFFSET %s
                        """,
                        (safe_limit, safe_offset),
                    )
                return self._normalize_value_rows(list(cur.fetchall()))

    def count_namespace_items(self, namespace_prefix: tuple[str, ...]) -> int:
        """Count items under one namespace prefix.

        Input: namespace prefix.
        Output: integer count.
        """

        prefix = self._normalize_namespace(namespace_prefix)
        with self._conn() as conn:
            with conn.cursor() as cur:
                if prefix:
                    cur.execute(
                        """
                        SELECT COUNT(*) AS total
                        FROM agent_memory_items
                        WHERE cardinality(namespace) >= %s
                          AND namespace[1:%s] = %s
                        """,
                        (len(prefix), len(prefix), list(prefix)),
                    )
                else:
                    cur.execute("SELECT COUNT(*) AS total FROM agent_memory_items")
                row = cur.fetchone()
                return int(row["total"] if row else 0)

    def list_namespace_paths(self, prefix: tuple[str, ...] | None = None) -> list[tuple[str, ...]]:
        """List distinct stored namespace paths.

        Input: optional namespace prefix filter.
        Output: sorted namespace tuples.
        """

        clean_prefix = self._normalize_namespace(prefix)
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT namespace FROM agent_memory_items ORDER BY namespace")
                rows = cur.fetchall()

        namespaces = []
        for row in rows:
            namespace = row.get("namespace") if isinstance(row, dict) else None
            if isinstance(namespace, list):
                ns = tuple(namespace)
            elif isinstance(namespace, tuple):
                ns = namespace
            else:
                continue
            if clean_prefix and ns[: len(clean_prefix)] != clean_prefix:
                continue
            namespaces.append(ns)
        return namespaces

    def search_namespace_items(
        self,
        namespace_prefix: tuple[str, ...],
        query: str | None = None,
        filter: dict[str, Any] | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Search namespace values with lexical matching.

        Input: namespace prefix, optional query/filter, paging.
        Output: matching namespace rows.
        """

        prefix = self._normalize_namespace(namespace_prefix)
        query_text = str(query or "").strip().lower()
        safe_limit = max(1, int(limit))
        safe_offset = max(0, int(offset))

        rows = self.list_namespace_items(prefix, limit=10000, offset=0)
        results: list[dict[str, Any]] = []
        for row in rows:
            value = row.get("value")
            if filter:
                if not isinstance(value, dict) or not all(value.get(k) == v for k, v in filter.items()):
                    continue
            if query_text:
                haystack = json.dumps(value, ensure_ascii=False, default=str).lower() if isinstance(value, (dict, list)) else str(value).lower()
                if query_text not in haystack:
                    continue
            results.append(row)
        return results[safe_offset : safe_offset + safe_limit]
