"""PostgreSQL-backed registry for memoryd records and tasks.

Provides: schema management and CRUD/query helpers for memoryd state.
"""

from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
from typing import Any, Iterator

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Json
except Exception:  # pragma: no cover - runtime dependency guard
    psycopg = None
    dict_row = None
    Json = None

from memoryd.schemas import deserialize_json
from core.logging_utils import log


def _preview_data_block(value: Any, head: int = 100, tail: int = 100) -> str:
    """Build bounded preview string: up to 100 chars from start + 100 from end."""

    text = str(value or "")
    cap = max(0, int(head)) + max(0, int(tail))
    if len(text) <= cap:
        return text
    return text[: max(0, int(head))] + text[-max(0, int(tail)) :]


class MemorydStore:
    """Database registry manager for memoryd.

    Input: root directory and app config.
    Output: schema management and CRUD methods for memoryd data.
    """

    def __init__(self, root_dir: str, config: dict[str, Any]):
        if psycopg is None:
            raise RuntimeError("psycopg is required for memoryd store")
        self.root_dir = root_dir
        self.config = config

    def _db_cfg(self) -> dict[str, Any]:
        db = self.config.get("database", {}) if isinstance(self.config, dict) else {}
        return {
            "host": db.get("host", "127.0.0.1"),
            "port": int(db.get("port", 5432)),
            "user": db.get("user", "aibt"),
            "password": db.get("password", ""),
            "dbname": db.get("db", "aibt"),
        }

    @contextmanager
    def _conn(self) -> Iterator[Any]:
        conn = psycopg.connect(**self._db_cfg(), autocommit=True, row_factory=dict_row)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _tx_conn(self) -> Iterator[Any]:
        """Open explicit transaction connection for multi-step atomic operations."""

        conn = psycopg.connect(**self._db_cfg(), autocommit=False, row_factory=dict_row)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def ensure_schema(self) -> None:
        """Create required tables and indexes for memoryd.

        Input: none.
        Output: guaranteed schema for memoryd operations.
        """

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memoryd_records (
                      id BIGSERIAL PRIMARY KEY,
                      muid TEXT NOT NULL,
                      type TEXT NOT NULL,
                      title TEXT NOT NULL DEFAULT '',
                      body TEXT NOT NULL DEFAULT '',
                      importance SMALLINT NOT NULL DEFAULT 5,
                      tags JSONB NOT NULL DEFAULT '[]'::jsonb,
                      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                      updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memoryd_tasks (
                      task_id UUID PRIMARY KEY,
                      muid TEXT NOT NULL,
                      caller_tag TEXT,
                                            work_hash TEXT,
                                            request_text TEXT,
                                            provider TEXT,
                                            model TEXT,
                                            tools JSONB,
                                            context_types JSONB,
                      requested_types JSONB NOT NULL DEFAULT '[]'::jsonb,
                      source_context JSONB NOT NULL DEFAULT '{}'::jsonb,
                      final_response TEXT NOT NULL DEFAULT '',
                      status TEXT NOT NULL,
                      prio INT NOT NULL DEFAULT 0,
                      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                      started_at TIMESTAMPTZ,
                      finished_at TIMESTAMPTZ,
                      retry_count INT NOT NULL DEFAULT 0,
                      error TEXT
                    )
                    """
                )
                cur.execute("ALTER TABLE memoryd_tasks ADD COLUMN IF NOT EXISTS work_hash TEXT")
                cur.execute("ALTER TABLE memoryd_tasks ADD COLUMN IF NOT EXISTS provider TEXT")
                cur.execute("ALTER TABLE memoryd_tasks ADD COLUMN IF NOT EXISTS model TEXT")
                cur.execute("ALTER TABLE memoryd_tasks ADD COLUMN IF NOT EXISTS tools JSONB")
                cur.execute("ALTER TABLE memoryd_tasks ADD COLUMN IF NOT EXISTS context_types JSONB")
                cur.execute(
                    """
                    DO $$
                    BEGIN
                        IF EXISTS (
                            SELECT 1
                            FROM information_schema.columns
                            WHERE table_name = 'memoryd_tasks' AND column_name = 'codex_agent'
                        ) AND NOT EXISTS (
                            SELECT 1
                            FROM information_schema.columns
                            WHERE table_name = 'memoryd_tasks' AND column_name = 'request_text'
                        ) THEN
                            EXECUTE 'ALTER TABLE memoryd_tasks RENAME COLUMN codex_agent TO request_text';
                        END IF;
                    END $$;
                    """
                )
                cur.execute("ALTER TABLE memoryd_tasks ADD COLUMN IF NOT EXISTS request_text TEXT")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_memoryd_records_muid_type_updated ON memoryd_records(muid, type, updated_at DESC)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_memoryd_records_lookup ON memoryd_records(muid, type, title)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_memoryd_tasks_status_prio_created ON memoryd_tasks(status, prio DESC, created_at)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_memoryd_tasks_muid_caller_status ON memoryd_tasks(muid, caller_tag, status)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_memoryd_tasks_muid_workhash_status ON memoryd_tasks(muid, work_hash, status)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_memoryd_tasks_created ON memoryd_tasks(created_at ASC)")

    def _json_value(self, value: Any) -> Any:
        if Json is None:
            return value
        return Json(value)

    def upsert_record(self, record: dict[str, Any], conn: Any | None = None) -> dict[str, Any]:
        """Insert or update one memoryd record.

        Input: normalized record payload.
        Output: persisted record row.
        """

        muid = str(record["muid"]).strip().lower()
        type_name = str(record["type"]).strip().lower()
        title = str(record.get("title") or "").strip()
        body = str(record.get("body") or "").strip()
        importance = int(record.get("importance", 5))
        tags = record.get("tags") or []
        body_preview = _preview_data_block(body, head=100, tail=100)
        log(
            "memoryd",
            "info",
            (
                "save memory store params "
                f"muid={muid} type={type_name} title={title} "
                f"importance={importance} tags_count={len(tags) if isinstance(tags, list) else 0} "
                f"text_len={len(body)} text_preview={body_preview}"
            ),
        )

        def _execute(active_conn: Any) -> dict[str, Any]:
            with active_conn.cursor() as cur:
                record_id = record.get("id")
                if record_id:
                    cur.execute(
                        """
                        INSERT INTO memoryd_records(id, muid, type, title, body, importance, tags)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT(id) DO UPDATE
                          SET muid=EXCLUDED.muid,
                              type=EXCLUDED.type,
                              title=EXCLUDED.title,
                              body=EXCLUDED.body,
                              importance=EXCLUDED.importance,
                              tags=EXCLUDED.tags,
                              updated_at=now()
                        RETURNING *
                        """,
                        (record_id, muid, type_name, title, body, importance, self._json_value(tags)),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO memoryd_records(muid, type, title, body, importance, tags)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        RETURNING *
                        """,
                        (muid, type_name, title, body, importance, self._json_value(tags)),
                    )
                row = cur.fetchone()
                return dict(row) if row else {}

        if conn is not None:
            return _execute(conn)
        with self._conn() as own_conn:
            return _execute(own_conn)

    def delete_record_by_id(self, record_id: Any, conn: Any | None = None) -> None:
        """Hard-delete one memoryd record by id.

        Input: record id.
        Output: none.
        """

        if conn is not None:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM memoryd_records WHERE id=%s", (record_id,))
            return
        with self._conn() as own_conn:
            with own_conn.cursor() as cur:
                cur.execute("DELETE FROM memoryd_records WHERE id=%s", (record_id,))

    def find_records_by_title(self, muid: str, type_name: str, title: str, conn: Any | None = None) -> list[dict[str, Any]]:
        """Return records matching title exactly within one MUID and type.

        Input: muid, type, title.
        Output: matching records.
        """

        def _execute(active_conn: Any) -> list[dict[str, Any]]:
            with active_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT *
                    FROM memoryd_records
                    WHERE muid=%s AND type=%s AND title=%s
                    ORDER BY updated_at DESC, id DESC
                    """,
                    (muid, type_name, title),
                )
                return [dict(row) for row in cur.fetchall()]

        if conn is not None:
            return _execute(conn)
        with self._conn() as own_conn:
            return _execute(own_conn)

    def get_record_by_id(self, record_id: Any, conn: Any | None = None) -> dict[str, Any] | None:
        """Fetch one record by id.

        Input: record id.
        Output: record row or None.
        """

        def _execute(active_conn: Any) -> dict[str, Any] | None:
            with active_conn.cursor() as cur:
                cur.execute("SELECT * FROM memoryd_records WHERE id=%s", (record_id,))
                row = cur.fetchone()
                return dict(row) if row else None

        if conn is not None:
            return _execute(conn)
        with self._conn() as own_conn:
            return _execute(own_conn)

    def list_records(self, muid: str, types: list[str] | None = None, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        """List memoryd records with optional filters.

        Input: muid, optional type list, offset/limit.
        Output: ordered record rows.
        """

        params: list[Any] = [muid]
        where = ["muid=%s"]
        if types:
            where.append("type = ANY(%s)")
            params.append(types)
        params.extend([max(1, int(limit)), max(0, int(offset))])
        sql = f"""
            SELECT *
            FROM memoryd_records
            WHERE {' AND '.join(where)}
            ORDER BY type ASC, importance DESC, updated_at DESC, id DESC
            LIMIT %s OFFSET %s
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                return [dict(row) for row in cur.fetchall()]

    def list_muids(self, limit: int = 200) -> list[str]:
        """Return distinct MUID values stored in memoryd records.

        Input: result limit.
        Output: ordered MUID list.
        """

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT muid
                    FROM memoryd_records
                    ORDER BY muid ASC
                    LIMIT %s
                    """,
                    (max(1, int(limit)),),
                )
                return [str(row["muid"]) for row in cur.fetchall() if str(row.get("muid") or "").strip()]

    def count_pending_tasks(self) -> int:
        """Return pending task count.

        Input: none.
        Output: pending queue depth.
        """

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS count FROM memoryd_tasks WHERE status='pending'")
                row = cur.fetchone()
                return int((row or {}).get("count", 0) or 0)

    def find_pending_tasks_by_key(self, muid: str, caller_tag: str | None = None) -> list[dict[str, Any]]:
        """Find pending tasks for one (muid, caller_tag) key.

        Input: muid and optional caller tag.
        Output: matching pending task rows.
        """

        with self._conn() as conn:
            with conn.cursor() as cur:
                if caller_tag is None:
                    cur.execute(
                        """
                        SELECT *
                        FROM memoryd_tasks
                        WHERE muid=%s AND status='pending' AND caller_tag IS NULL
                        ORDER BY created_at ASC, task_id ASC
                        """,
                        (muid,),
                    )
                else:
                    cur.execute(
                        """
                        SELECT *
                        FROM memoryd_tasks
                        WHERE muid=%s AND status='pending' AND caller_tag=%s
                        ORDER BY created_at ASC, task_id ASC
                        """,
                        (muid, caller_tag),
                    )
                return [dict(row) for row in cur.fetchall()]

    def delete_task(self, task_id: str) -> None:
        """Delete one task row.

        Input: task id.
        Output: none.
        """

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM memoryd_tasks WHERE task_id=%s", (task_id,))

    def prune_records(self, muid: str, type_name: str, max_record_count: int, max_content_length: int, conn: Any | None = None) -> int:
        """Enforce per-MUID/type retention limits.

        Input: muid, type, and limits.
        Output: deleted row count.
        """

        def _select_rows(active_conn: Any) -> list[dict[str, Any]]:
            with active_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, title, body, importance, created_at
                    FROM memoryd_records
                    WHERE muid=%s AND type=%s
                    ORDER BY importance ASC, created_at ASC, id ASC
                    """,
                    (muid, type_name),
                )
                return [dict(row) for row in cur.fetchall()]

        if conn is not None:
            rows = _select_rows(conn)
        else:
            with self._conn() as own_conn:
                rows = _select_rows(own_conn)

        def _content_len(row: dict[str, Any]) -> int:
            return len(str(row.get("title") or "")) + len(str(row.get("body") or ""))

        to_delete: set[Any] = set()
        if len(rows) > max_record_count:
            for row in rows[: len(rows) - max_record_count]:
                to_delete.add(row["id"])

        remaining = [row for row in rows if row["id"] not in to_delete]
        total = sum(_content_len(row) for row in remaining)
        while remaining and total > max_content_length:
            row = remaining.pop(0)
            to_delete.add(row["id"])
            total -= _content_len(row)

        if not to_delete:
            return 0

        def _delete_rows(active_conn: Any) -> int:
            with active_conn.cursor() as cur:
                cur.execute("DELETE FROM memoryd_records WHERE id = ANY(%s)", (list(to_delete),))
                return int(cur.rowcount or 0)

        if conn is not None:
            return _delete_rows(conn)
        with self._conn() as own_conn:
            return _delete_rows(own_conn)

    def create_task(self, task: dict[str, Any]) -> None:
        """Insert a pending memoryd task.

        Input: task payload.
        Output: none.
        """

        source_context_text = str(task.get("source_context") or "")
        source_context_preview = _preview_data_block(source_context_text, head=100, tail=100)
        final_response = str(task.get("final_response") or "")
        final_response_preview = _preview_data_block(final_response, head=100, tail=100)
        request_text = str(task.get("request_text") or "")
        request_preview = _preview_data_block(request_text, head=100, tail=100)
        provider = str(task.get("provider") or "").strip()
        model = str(task.get("model") or "").strip()
        log(
            "memoryd",
            "info",
            (
                "create memory task params "
                f"task_id={task.get('task_id')} muid={task.get('muid')} caller_tag={task.get('caller_tag')} "
                f"work_hash={task.get('work_hash')} "
            f"provider={provider or '<default>'} model={model or '<default>'} "
                f"request_text_len={len(request_text)} request_text_preview={request_preview} "
            f"context_types={task.get('context_types') or []} tools={task.get('tools') if task.get('tools') is not None else '<default>'} "
                f"requested_types={task.get('requested_types') or []} "
                f"source_context_len={len(source_context_text)} source_context_preview={source_context_preview} "
                f"final_response_len={len(final_response)} final_response_preview={final_response_preview}"
            ),
        )

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO memoryd_tasks(
                                            task_id, muid, caller_tag, work_hash, request_text, provider, model, tools, context_types, requested_types, source_context, final_response, status, prio
                                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s)
                    """,
                    (
                        task["task_id"],
                        task["muid"],
                        task.get("caller_tag"),
                        task.get("work_hash"),
                        task.get("request_text"),
                                                task.get("provider"),
                                                task.get("model"),
                                                self._json_value(task.get("tools")) if task.get("tools") is not None else None,
                                                self._json_value(task.get("context_types")) if task.get("context_types") is not None else None,
                        self._json_value(task.get("requested_types") or []),
                        self._json_value(task.get("source_context") or {}),
                        str(task.get("final_response") or ""),
                        int(task.get("prio", 0)),
                    ),
                )

    def find_inflight_tasks(
        self,
        muid: str,
        caller_tag: str | None = None,
        work_hash: str | None = None,
    ) -> list[dict[str, Any]]:
        """Find in-flight tasks for one MUID by caller_tag/work_hash.

        Input: muid and optional caller_tag/work_hash.
        Output: pending/running task rows that match at least one provided key.
        """

        if caller_tag is None and work_hash is None:
            return []
        where: list[str] = ["muid=%s", "status IN ('pending','running')"]
        params: list[Any] = [muid]
        keys: list[str] = []
        if caller_tag is not None:
            keys.append("caller_tag=%s")
            params.append(caller_tag)
        if work_hash is not None:
            keys.append("work_hash=%s")
            params.append(work_hash)
        where.append("(" + " OR ".join(keys) + ")")
        sql = (
            "SELECT * FROM memoryd_tasks WHERE "
            + " AND ".join(where)
            + " ORDER BY created_at ASC, task_id ASC"
        )
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                return [dict(row) for row in cur.fetchall()]

    def count_inflight_tasks_by_caller_prefix(self, prefix: str) -> int:
        """Count in-flight tasks whose caller_tag starts with prefix.

        Input: caller_tag prefix.
        Output: number of pending/running tasks.
        """

        text = str(prefix or "").strip()
        if not text:
            return 0
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM memoryd_tasks
                    WHERE status IN ('pending','running')
                      AND caller_tag LIKE %s
                    """,
                    (text + "%",),
                )
                row = cur.fetchone()
                return int((row or {}).get("count", 0) or 0)

    def fetch_pending_tasks(self, limit: int) -> list[dict[str, Any]]:
        """Fetch pending tasks in FIFO order.

        Input: max number of tasks.
        Output: list of task dicts.
        """

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT *
                    FROM memoryd_tasks
                    WHERE status = 'pending'
                    ORDER BY created_at ASC, task_id ASC
                    LIMIT %s
                    """,
                    (max(1, int(limit)),),
                )
                return [dict(row) for row in cur.fetchall()]

    def mark_task_running(self, task_id: str) -> bool:
        """Mark a pending task as running.

        Input: task id.
        Output: True if row was updated.
        """

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE memoryd_tasks
                    SET status='running', started_at=COALESCE(started_at, now()), updated_at=now(), retry_count=retry_count+1
                    WHERE task_id=%s AND status='pending'
                    """,
                    (task_id,),
                )
                return bool(cur.rowcount)

    def mark_task_done(self, task_id: str, conn: Any | None = None) -> None:
        """Mark a task as done.

        Input: task id.
        Output: none.
        """

        if conn is not None:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE memoryd_tasks
                    SET status='done', finished_at=now(), error=NULL, updated_at=now()
                    WHERE task_id=%s
                    """,
                    (task_id,),
                )
            return
        with self._conn() as own_conn:
            with own_conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE memoryd_tasks
                    SET status='done', finished_at=now(), error=NULL, updated_at=now()
                    WHERE task_id=%s
                    """,
                    (task_id,),
                )

    def mark_task_failed(self, task_id: str, error: str) -> None:
        """Mark a task as failed.

        Input: task id and error text.
        Output: none.
        """

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE memoryd_tasks
                    SET status='failed', finished_at=now(), error=%s, updated_at=now()
                    WHERE task_id=%s
                    """,
                    (error[:4000], task_id),
                )

    def mark_task_skipped(self, task_id: str, error: str | None = None) -> None:
        """Mark a task as skipped.

        Input: task id and optional reason.
        Output: none.
        """

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE memoryd_tasks
                    SET status='skipped', finished_at=now(), error=%s, updated_at=now()
                    WHERE task_id=%s
                    """,
                    ((error or "")[:4000], task_id),
                )

    def update_task_error(self, task_id: str, error: str) -> None:
        """Update task error field without changing status.

        Input: task id and error text.
        Output: none.
        """

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE memoryd_tasks SET error=%s, updated_at=now() WHERE task_id=%s",
                    (error[:4000], task_id),
                )


def _instrument_memoryd_store_calls() -> None:
    """Wrap MemorydStore methods to emit info logs on each call."""

    for name, attr in list(MemorydStore.__dict__.items()):
        if name.startswith("__"):
            continue
        if name in {"_instrument_memoryd_store_calls"}:
            continue
        if not callable(attr):
            continue
        if getattr(attr, "_memoryd_call_logged", False):
            continue

        def _make_wrapper(fn: Any, fn_name: str):
            @wraps(fn)
            def _wrapped(self, *args, **kwargs):
                log("memoryd", "info", f"call memoryd.store.{fn_name}")
                return fn(self, *args, **kwargs)

            setattr(_wrapped, "_memoryd_call_logged", True)
            return _wrapped

        setattr(MemorydStore, name, _make_wrapper(attr, name))


_instrument_memoryd_store_calls()
