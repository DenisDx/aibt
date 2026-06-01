"""MemoryExecutorAgent: periodic/todo-driven memoryd enqueue orchestrator."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
from typing import Any
from uuid import uuid4

from langchain_core.runnables import RunnableLambda
import psycopg
from psycopg.rows import dict_row

from agents.base import AgentBase
from core.config import load_env_file
from core.envid_runtime import build_effective_config
from core.logging_utils import log
from memoryd import get_memoryd_service


_PLACEHOLDER_RE = re.compile(r"\$\{([^}:]+)(?::-([^}]*))?\}")


@dataclass
class _TaskOutcome:
    """Outcome of one memory_executor task evaluation cycle."""

    reset_timer: bool
    queued_count: int
    skipped_count: int
    temp_deferred: bool


class MemoryExecutorStore:
    """Postgres-backed storage for memory_executor task definitions."""

    def __init__(self, root_dir: str, config: dict[str, Any]):
        self.root_dir = root_dir
        self.config = config or {}

    def _db_cfg(self) -> dict[str, Any]:
        """Return DB connection settings from app config."""

        db_cfg = self.config.get("database", {}) if isinstance(self.config, dict) else {}
        return {
            "host": str(db_cfg.get("host", "127.0.0.1")),
            "port": int(db_cfg.get("port", 5432)),
            "dbname": str(db_cfg.get("name", "aibt")),
            "user": str(db_cfg.get("user", "aibt")),
            "password": str(db_cfg.get("password", "aibt")),
        }

    def _conn(self):
        """Open a DB connection with dict rows."""

        return psycopg.connect(**self._db_cfg(), autocommit=True, row_factory=dict_row)

    @staticmethod
    def _normalize_types(values: Any) -> list[str]:
        """Normalize list-like task type fields."""

        if not isinstance(values, list):
            return []
        out: list[str] = []
        for item in values:
            text = str(item or "").strip().lower()
            if text and text not in out:
                out.append(text)
        return out

    @staticmethod
    def _normalize_tools(values: Any) -> list[str] | None:
        """Normalize tools list; None means provider default behavior."""

        if values is None:
            return None
        if not isinstance(values, list):
            return []
        out: list[str] = []
        for item in values:
            text = str(item or "").strip()
            if text and text not in out:
                out.append(text)
        return out or None

    @staticmethod
    def _normalize_optional_int(value: Any) -> int | None:
        """Normalize optional integer; blank-like values become None."""

        if value is None:
            return None
        if isinstance(value, bool):
            return int(value)
        text = str(value).strip()
        if not text:
            return None
        return int(text)

    @staticmethod
    def _normalize_optional_float(value: Any) -> float | None:
        """Normalize optional float; blank-like values become None."""

        if value is None:
            return None
        if isinstance(value, bool):
            return float(int(value))
        text = str(value).strip()
        if not text:
            return None
        return float(text)

    def _prepare_task_payload(self, payload: dict[str, Any], task_id: str | None = None) -> dict[str, Any]:
        """Validate and normalize one task payload for DB operations."""

        task: dict[str, Any] = dict(payload or {})
        clean_id = str(task_id or task.get("id") or "").strip()
        if clean_id:
            task["id"] = clean_id
        else:
            task["id"] = str(uuid4())

        name = str(task.get("name") or "").strip()
        if not name:
            raise ValueError("task.name is required")
        task["name"] = name

        muid = str(task.get("muid") or "").strip().lower()
        if not muid:
            raise ValueError("task.muid is required")
        task["muid"] = muid

        request_text = str(task.get("request_text") or "").strip()
        if not request_text:
            raise ValueError("task.request_text is required")
        task["request_text"] = request_text

        envid = str(task.get("envid") or "").strip() or None
        task["envid"] = envid
        task["enabled"] = bool(task.get("enabled", True))
        raw_period = self._normalize_optional_int(task.get("period_sec"))
        task["period_sec"] = raw_period if raw_period and raw_period > 0 else None
        task["todo_title"] = str(task.get("todo_title") or "").strip() or None
        task["provider"] = str(task.get("provider") or "").strip() or None
        task["model"] = str(task.get("model") or "").strip() or None
        task["temperature"] = self._normalize_optional_float(task.get("temperature"))
        task["top_p"] = self._normalize_optional_float(task.get("top_p"))
        task["repetition_penalty"] = self._normalize_optional_float(task.get("repetition_penalty"))
        task["max_tokens"] = self._normalize_optional_int(task.get("max_tokens"))
        task["seed"] = self._normalize_optional_int(task.get("seed"))
        task["presence_penalty"] = self._normalize_optional_float(task.get("presence_penalty"))
        task["frequency_penalty"] = self._normalize_optional_float(task.get("frequency_penalty"))
        task["top_k"] = self._normalize_optional_int(task.get("top_k"))
        task["min_p"] = self._normalize_optional_float(task.get("min_p"))
        task["tools"] = self._normalize_tools(task.get("tools"))
        task["context_types"] = self._normalize_types(task.get("context_types") or [])
        task["update_types"] = self._normalize_types(task.get("update_types") or [])
        task["execution_policy"] = str(task.get("execution_policy") or "idle").strip().lower() or "idle"
        task["enqueue_key"] = str(task.get("enqueue_key") or "").strip() or None
        return task

    @staticmethod
    def _encode_jsonb(value: Any) -> Any:
        """Encode JSON payload in a DB-safe way."""

        return json.dumps(value)

    def ensure_schema(self) -> None:
        """Create or migrate memory_executor table and indexes."""

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memory_executor_tasks (
                      id UUID PRIMARY KEY,
                      name TEXT NOT NULL,
                      enabled BOOLEAN NOT NULL DEFAULT TRUE,
                      envid TEXT,
                      muid TEXT NOT NULL,
                      period_sec INTEGER,
                      todo_title TEXT,
                      request_text TEXT NOT NULL,
                      provider TEXT,
                      model TEXT,
                      temperature DOUBLE PRECISION,
                      top_p DOUBLE PRECISION,
                      repetition_penalty DOUBLE PRECISION,
                      max_tokens INTEGER,
                      seed BIGINT,
                      presence_penalty DOUBLE PRECISION,
                      frequency_penalty DOUBLE PRECISION,
                      top_k INTEGER,
                      min_p DOUBLE PRECISION,
                      tools JSONB,
                      context_types JSONB NOT NULL DEFAULT '[]'::jsonb,
                      update_types JSONB NOT NULL DEFAULT '[]'::jsonb,
                      execution_policy TEXT NOT NULL DEFAULT 'idle',
                      enqueue_key TEXT,
                      last_run_at TIMESTAMPTZ,
                      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                      updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
                cur.execute("ALTER TABLE memory_executor_tasks ADD COLUMN IF NOT EXISTS execution_policy TEXT NOT NULL DEFAULT 'idle'")
                cur.execute("ALTER TABLE memory_executor_tasks ADD COLUMN IF NOT EXISTS enqueue_key TEXT")
                cur.execute("ALTER TABLE memory_executor_tasks ADD COLUMN IF NOT EXISTS temperature DOUBLE PRECISION")
                cur.execute("ALTER TABLE memory_executor_tasks ADD COLUMN IF NOT EXISTS top_p DOUBLE PRECISION")
                cur.execute("ALTER TABLE memory_executor_tasks ADD COLUMN IF NOT EXISTS repetition_penalty DOUBLE PRECISION")
                cur.execute("ALTER TABLE memory_executor_tasks ADD COLUMN IF NOT EXISTS max_tokens INTEGER")
                cur.execute("ALTER TABLE memory_executor_tasks ADD COLUMN IF NOT EXISTS seed BIGINT")
                cur.execute("ALTER TABLE memory_executor_tasks ADD COLUMN IF NOT EXISTS presence_penalty DOUBLE PRECISION")
                cur.execute("ALTER TABLE memory_executor_tasks ADD COLUMN IF NOT EXISTS frequency_penalty DOUBLE PRECISION")
                cur.execute("ALTER TABLE memory_executor_tasks ADD COLUMN IF NOT EXISTS top_k INTEGER")
                cur.execute("ALTER TABLE memory_executor_tasks ADD COLUMN IF NOT EXISTS min_p DOUBLE PRECISION")
                cur.execute(
                    """
                    DO $$
                    BEGIN
                        IF EXISTS (
                            SELECT 1
                            FROM information_schema.columns
                            WHERE table_name = 'memory_executor_tasks' AND column_name = 'template_text'
                        ) AND NOT EXISTS (
                            SELECT 1
                            FROM information_schema.columns
                            WHERE table_name = 'memory_executor_tasks' AND column_name = 'request_text'
                        ) THEN
                            EXECUTE 'ALTER TABLE memory_executor_tasks RENAME COLUMN template_text TO request_text';
                        END IF;
                    END $$;
                    """
                )
                cur.execute(
                    """
                    DO $$
                    BEGIN
                        IF EXISTS (
                            SELECT 1
                            FROM information_schema.columns
                            WHERE table_name = 'memory_executor_tasks' AND column_name = 'codex_agent'
                        ) THEN
                            IF EXISTS (
                                SELECT 1
                                FROM information_schema.columns
                                WHERE table_name = 'memory_executor_tasks' AND column_name = 'request_text'
                            ) THEN
                                EXECUTE 'UPDATE memory_executor_tasks SET request_text = COALESCE(NULLIF(request_text, ''''), codex_agent)';
                            END IF;
                            EXECUTE 'ALTER TABLE memory_executor_tasks DROP COLUMN codex_agent';
                        END IF;
                    END $$;
                    """
                )
                cur.execute("CREATE INDEX IF NOT EXISTS idx_memory_executor_tasks_enabled ON memory_executor_tasks(enabled, envid)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_memory_executor_tasks_due ON memory_executor_tasks(enabled, last_run_at)")

    def list_enabled_tasks(self) -> list[dict[str, Any]]:
        """List all enabled memory_executor tasks."""

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT *
                    FROM memory_executor_tasks
                    WHERE enabled = TRUE
                    ORDER BY created_at ASC, id ASC
                    """
                )
                return [dict(row) for row in cur.fetchall()]

    def list_tasks(self, envid: str | None = None, limit: int = 200, offset: int = 0) -> dict[str, Any]:
        """List tasks for one envid or all when envid is None."""

        where: list[str] = []
        params: list[Any] = []
        if envid is not None:
            where.append("COALESCE(envid, '') = %s")
            params.append(str(envid).strip())
        where_sql = "WHERE " + " AND ".join(where) if where else ""
        safe_limit = max(1, min(500, int(limit)))
        safe_offset = max(0, int(offset))
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT COUNT(*) AS count FROM memory_executor_tasks {where_sql}",
                    tuple(params),
                )
                total = int((cur.fetchone() or {}).get("count", 0) or 0)
                cur.execute(
                    (
                        "SELECT * FROM memory_executor_tasks "
                        f"{where_sql} "
                        "ORDER BY created_at ASC, id ASC "
                        "LIMIT %s OFFSET %s"
                    ),
                    tuple(params + [safe_limit, safe_offset]),
                )
                rows = [dict(row) for row in cur.fetchall()]
                return {"items": rows, "total": total, "limit": safe_limit, "offset": safe_offset}

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        """Get one task by id."""

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM memory_executor_tasks WHERE id=%s", (task_id,))
                row = cur.fetchone()
                return dict(row) if row else None

    def create_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create one memory_executor task."""

        task = self._prepare_task_payload(payload)
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO memory_executor_tasks(
                                            id, name, enabled, envid, muid, period_sec, todo_title, request_text,
                                            provider, model, temperature, top_p, repetition_penalty, max_tokens,
                                            seed, presence_penalty, frequency_penalty, top_k, min_p,
                                            tools, context_types, update_types, execution_policy,
                                            enqueue_key, last_run_at
                    ) VALUES (
                      %s, %s, %s, %s, %s, %s, %s, %s,
                                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s,
                                            %s, NULL
                    )
                    RETURNING *
                    """,
                    (
                        task["id"],
                        task["name"],
                        task["enabled"],
                        task["envid"],
                        task["muid"],
                        task["period_sec"],
                        task["todo_title"],
                        task["request_text"],
                        task["provider"],
                        task["model"],
                        task["temperature"],
                        task["top_p"],
                        task["repetition_penalty"],
                        task["max_tokens"],
                        task["seed"],
                        task["presence_penalty"],
                        task["frequency_penalty"],
                        task["top_k"],
                        task["min_p"],
                        self._encode_jsonb(task["tools"]),
                        self._encode_jsonb(task["context_types"]),
                        self._encode_jsonb(task["update_types"]),
                        task["execution_policy"],
                        task["enqueue_key"],
                    ),
                )
                row = cur.fetchone()
                return dict(row) if row else {}

    def update_task(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Replace one task payload by id."""

        if self.get_task(task_id) is None:
            return None
        task = self._prepare_task_payload(payload, task_id=task_id)
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE memory_executor_tasks
                    SET name=%s,
                        enabled=%s,
                        envid=%s,
                        muid=%s,
                        period_sec=%s,
                        todo_title=%s,
                        request_text=%s,
                        provider=%s,
                        model=%s,
                        temperature=%s,
                        top_p=%s,
                        repetition_penalty=%s,
                        max_tokens=%s,
                        seed=%s,
                        presence_penalty=%s,
                        frequency_penalty=%s,
                        top_k=%s,
                        min_p=%s,
                        tools=%s::jsonb,
                        context_types=%s::jsonb,
                        update_types=%s::jsonb,
                        execution_policy=%s,
                        enqueue_key=%s,
                        updated_at=now()
                    WHERE id=%s
                    RETURNING *
                    """,
                    (
                        task["name"],
                        task["enabled"],
                        task["envid"],
                        task["muid"],
                        task["period_sec"],
                        task["todo_title"],
                        task["request_text"],
                        task["provider"],
                        task["model"],
                        task["temperature"],
                        task["top_p"],
                        task["repetition_penalty"],
                        task["max_tokens"],
                        task["seed"],
                        task["presence_penalty"],
                        task["frequency_penalty"],
                        task["top_k"],
                        task["min_p"],
                        self._encode_jsonb(task["tools"]),
                        self._encode_jsonb(task["context_types"]),
                        self._encode_jsonb(task["update_types"]),
                        task["execution_policy"],
                        task["enqueue_key"],
                        task_id,
                    ),
                )
                row = cur.fetchone()
                return dict(row) if row else None

    def delete_task(self, task_id: str) -> bool:
        """Delete one task by id."""

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM memory_executor_tasks WHERE id=%s", (task_id,))
                return bool(cur.rowcount)

    def mark_task_checked(self, task_id: str, now_utc: datetime) -> None:
        """Set last_run_at for one task after a completed evaluation cycle."""

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE memory_executor_tasks
                    SET last_run_at=%s, updated_at=now()
                    WHERE id=%s
                    """,
                    (now_utc, task_id),
                )


class MemoryExecutorAgent(AgentBase):
    """Cron-driven agent that enqueues planned memoryd update jobs."""

    def __init__(self, app_config: dict[str, Any], agent_config: dict[str, Any] | None = None):
        super().__init__(app_config=app_config, agent_config=agent_config)
        root_dir = str(self.app_config.get("root") or os.getcwd())
        self._store = MemoryExecutorStore(root_dir, self.app_config)

    def build_chain(self):
        """Build trivial chain because this agent runs from cron hooks."""

        async def _noop(payload: dict[str, Any]) -> str:
            _ = payload
            return "memory_executor handles scheduled work in on_cron_tick"

        return RunnableLambda(_noop)

    async def on_init(self, runtime: dict[str, Any]) -> None:
        """Ensure table schema once when app starts."""

        _ = runtime
        self._store.ensure_schema()

    async def on_cron_tick(self, runtime: dict[str, Any]) -> None:
        """Evaluate due tasks and enqueue memoryd work items."""

        runtime_envid = str(runtime.get("envid") or "").strip() or None

        # If a global instance is enabled, it is responsible for all scopes.
        if runtime_envid is not None:
            global_cfg = self.assemble_runtime_config(self.app_config, "memory_executor", envid=None)
            global_enabled = bool(global_cfg.get("enabled", True))
            if global_enabled:
                return

        self._store.ensure_schema()
        now_utc = datetime.now(timezone.utc)
        max_due = self._max_due_tasks_per_tick(self.app_config)
        root_dir = str(self.app_config.get("root") or os.getcwd())
        memoryd_services: dict[str | None, Any] = {}

        due_count = 0
        queued_count = 0
        skipped_count = 0
        deferred_count = 0

        for task in self._store.list_enabled_tasks():
            task_envid = str(task.get("envid") or "").strip() or None
            if runtime_envid is not None and task_envid != runtime_envid:
                continue
            effective_cfg = build_effective_config(self.app_config, task_envid)
            memoryd_service = memoryd_services.get(task_envid)
            if memoryd_service is None:
                memoryd_service = get_memoryd_service(root_dir, effective_cfg)
                memoryd_service.initialize()
                memoryd_services[task_envid] = memoryd_service
            if not self._is_due(task, now_utc):
                continue
            due_count += 1
            if max_due > 0 and due_count > max_due:
                # Keep timer unchanged for due items not processed this tick.
                break

            outcome = self._process_task(task, now_utc, memoryd_service)
            queued_count += int(outcome.queued_count)
            skipped_count += int(outcome.skipped_count)
            if outcome.temp_deferred:
                deferred_count += 1
            if outcome.reset_timer:
                self._store.mark_task_checked(str(task.get("id") or ""), now_utc)

        log(
            "agents",
            "info",
            (
                "memory_executor cron tick "
                f"scope={runtime_envid or 'all'} "
                f"due={due_count} queued={queued_count} skipped={skipped_count} deferred={deferred_count} "
                f"max_due={max_due if max_due > 0 else 'uncapped'}"
            ),
        )

    def run_task_now(self, task_id: str) -> dict[str, Any]:
        """Enqueue one selected task immediately, bypassing due checks."""

        clean_id = str(task_id or "").strip()
        if not clean_id:
            raise ValueError("task_id is required")

        self._store.ensure_schema()
        task = self._store.get_task(clean_id)
        if task is None:
            return {"ok": True, "found": False, "queued": False, "reason": "task_not_found"}

        request_text = str(task.get("request_text") or "").strip()
        if not request_text:
            return {"ok": True, "found": True, "queued": False, "reason": "task_request_text_is_empty"}

        resolved_request, unresolved = self._resolve_placeholders(request_text)
        if unresolved:
            return {
                "ok": True,
                "found": True,
                "queued": False,
                "reason": "unresolved_placeholders",
                "placeholders": sorted(unresolved),
            }

        root_dir = str(self.app_config.get("root") or os.getcwd())
        task_envid = str(task.get("envid") or "").strip() or None
        effective_cfg = build_effective_config(self.app_config, task_envid)
        memoryd_service = get_memoryd_service(root_dir, effective_cfg)
        memoryd_service.initialize()
        now_utc = datetime.now(timezone.utc)

        queued = self._enqueue_one(
            task=task,
            task_envid=task_envid,
            memoryd_service=memoryd_service,
            rendered_text=resolved_request,
            todo_record=None,
            now_utc=now_utc,
        )
        if queued:
            self._store.mark_task_checked(clean_id, now_utc)
            return {"ok": True, "found": True, "queued": True, "task_id": clean_id}
        return {"ok": True, "found": True, "queued": False, "reason": "not_queued", "task_id": clean_id}

    def _max_due_tasks_per_tick(self, config: dict[str, Any]) -> int:
        """Read max due task cap from effective config."""

        agent_cfg = (config.get("agents", {}) or {}).get("items", {}).get("memory_executor", {})
        value = int(agent_cfg.get("max_due_tasks_per_tick", self.agent_config.get("max_due_tasks_per_tick", 200)) or 0)
        return max(0, value)

    def _is_due(self, task: dict[str, Any], now_utc: datetime) -> bool:
        """Check whether one task should be evaluated on this tick."""

        period_raw = task.get("period_sec")
        if period_raw is None:
            return True
        period_sec = max(1, int(period_raw))
        last_run = task.get("last_run_at")
        if last_run is None:
            return True
        if isinstance(last_run, datetime):
            last_dt = last_run.astimezone(timezone.utc)
        else:
            last_dt = datetime.fromisoformat(str(last_run)).astimezone(timezone.utc)
        return now_utc >= last_dt + timedelta(seconds=period_sec)

    def _process_task(
        self,
        task: dict[str, Any],
        now_utc: datetime,
        memoryd_service: Any,
    ) -> _TaskOutcome:
        """Evaluate one task branch and enqueue required memoryd jobs."""

        request_text = str(task.get("request_text") or "").strip()
        if not request_text:
            return _TaskOutcome(reset_timer=True, queued_count=0, skipped_count=1, temp_deferred=False)

        resolved_request, unresolved = self._resolve_placeholders(request_text)
        if unresolved:
            log(
                "agents",
                "warning",
                (
                    "memory_executor unresolved placeholders "
                    f"task_id={task.get('id')} placeholders={sorted(unresolved)}"
                ),
            )
            return _TaskOutcome(reset_timer=True, queued_count=0, skipped_count=1, temp_deferred=False)

        execution_policy = str(task.get("execution_policy") or "idle").strip().lower() or "idle"
        prefix = self._task_enqueue_prefix(task)
        if execution_policy == "idle" and memoryd_service.store.count_inflight_tasks_by_caller_prefix(prefix) > 0:
            return _TaskOutcome(reset_timer=False, queued_count=0, skipped_count=0, temp_deferred=True)
        if execution_policy == "idle":
            provider = str(task.get("provider") or "").strip() or memoryd_service._active_provider()
            model = memoryd_service._resolve_model_for_provider(provider, str(task.get("model") or "").strip() or None)
            if provider == "openaix":
                priority = int(memoryd_service._memoryd_model_cfg().get("memory_task_prio", 0))
                queue_state = memoryd_service._queue_state(provider, model, priority)
                if not queue_state or not bool(queue_state.get("can_run_now", False)):
                    return _TaskOutcome(reset_timer=False, queued_count=0, skipped_count=0, temp_deferred=True)

        todo_title = str(task.get("todo_title") or "").strip()
        if not todo_title:
            queued = self._enqueue_one(
                task=task,
                task_envid=str(task.get("envid") or "").strip() or None,
                memoryd_service=memoryd_service,
                rendered_text=resolved_request,
                todo_record=None,
                now_utc=now_utc,
            )
            if queued is None:
                return _TaskOutcome(reset_timer=False, queued_count=0, skipped_count=0, temp_deferred=True)
            return _TaskOutcome(reset_timer=True, queued_count=1 if queued else 0, skipped_count=0 if queued else 1, temp_deferred=False)

        muid = str(task.get("muid") or "").strip().lower()
        rows = memoryd_service.store.list_records(muid=muid, types=["todo"], limit=500, offset=0)
        matches = [row for row in rows if str(row.get("title") or "").strip() == todo_title]
        if not matches:
            return _TaskOutcome(reset_timer=True, queued_count=0, skipped_count=1, temp_deferred=False)

        queued_count = 0
        skipped_count = 0
        temp_deferred = False

        for row in matches:
            if not self._todo_should_enqueue(row):
                skipped_count += 1
                continue
            merged_text = self._merge_request_with_todo(resolved_request, row)
            queued = self._enqueue_one(
                task=task,
                task_envid=str(task.get("envid") or "").strip() or None,
                memoryd_service=memoryd_service,
                rendered_text=merged_text,
                todo_record=row,
                now_utc=now_utc,
            )
            if queued is None:
                temp_deferred = True
                continue
            if queued:
                queued_count += 1
            else:
                skipped_count += 1

        if temp_deferred and queued_count == 0:
            return _TaskOutcome(reset_timer=False, queued_count=0, skipped_count=skipped_count, temp_deferred=True)
        return _TaskOutcome(reset_timer=True, queued_count=queued_count, skipped_count=skipped_count, temp_deferred=temp_deferred)

    def _enqueue_one(
        self,
        task: dict[str, Any],
        task_envid: str | None,
        memoryd_service: Any,
        rendered_text: str,
        todo_record: dict[str, Any] | None,
        now_utc: datetime,
    ) -> bool | None:
        """Enqueue one memoryd update task and map dedup to temporary defer."""

        muid = str(task.get("muid") or "").strip().lower()
        task_id = str(task.get("id") or "").strip()
        enqueue_key = self._enqueue_key(task, todo_record)
        work_hash = self._work_hash(task=task, muid=muid, rendered_text=rendered_text, todo_record=todo_record)
        context_types = self._resolve_task_memoryd_types(task, task_envid, key="context_types")
        update_types = self._resolve_task_memoryd_types(task, task_envid, key="update_types")
        user_query = self._todo_effective_body(todo_record) if isinstance(todo_record, dict) else ""
        source_context = {
            "adapter": "memory_executor",
            "envid": task_envid,
            "task_id": task_id,
            "caller_tag": enqueue_key,
            "context_types": context_types,
            "update_types": update_types,
            "timestamp": now_utc.isoformat(),
            "todo": {
                "id": todo_record.get("id"),
                "title": todo_record.get("title"),
                "body": todo_record.get("body"),
            }
            if isinstance(todo_record, dict)
            else None,
        }
        if user_query:
            source_context["query"] = user_query
        response = memoryd_service.enqueue_update(
            source_context=source_context,
            final_response=rendered_text,
            muid=muid,
            caller_tag=enqueue_key,
            work_hash=work_hash,
            request_text=rendered_text,
            provider=str(task.get("provider") or "").strip() or None,
            model=str(task.get("model") or "").strip() or None,
            temperature=task.get("temperature"),
            top_p=task.get("top_p"),
            repetition_penalty=task.get("repetition_penalty"),
            max_tokens=task.get("max_tokens"),
            seed=task.get("seed"),
            presence_penalty=task.get("presence_penalty"),
            frequency_penalty=task.get("frequency_penalty"),
            top_k=task.get("top_k"),
            min_p=task.get("min_p"),
            tools=task.get("tools"),
            context_types=context_types,
            types=update_types,
        )
        if not bool(response.get("queued", True)):
            reason = str(response.get("reason") or "").strip().lower()
            if reason == "duplicate_inflight":
                return False
            return False
        return True

    def _todo_should_enqueue(self, todo_record: dict[str, Any]) -> bool:
        """Evaluate todo status gate for one record."""

        payload = self._todo_json_payload(todo_record)
        if not isinstance(payload, dict):
            return True
        status = str(payload.get("status") or "").strip().lower()
        if status in {"done", "cancelled", "canceled", "error"}:
            return False
        return True

    def _merge_request_with_todo(self, request_text: str, todo_record: dict[str, Any]) -> str:
        """Append todo details to rendered task template text."""

        title = str(todo_record.get("title") or "").strip()
        body = self._todo_effective_body(todo_record)
        if not title and not body:
            return request_text
        return (
            request_text
            + "\n\n"
            + "TODO_TITLE:\n"
            + title
            + "\n\n"
            + "TODO_BODY:\n"
            + body
        )

    def _task_enqueue_prefix(self, task: dict[str, Any]) -> str:
        """Build caller_tag prefix used for idle-policy in-flight checks."""

        task_id = str(task.get("id") or "").strip()
        return f"mx:{task_id}:"

    def _enqueue_key(self, task: dict[str, Any], todo_record: dict[str, Any] | None) -> str:
        """Build stable enqueue_key used as memoryd caller_tag."""

        custom = str(task.get("enqueue_key") or "").strip()
        if custom:
            return custom
        base = self._task_enqueue_prefix(task)
        if not isinstance(todo_record, dict):
            return base + "periodic"
        record_id = str(todo_record.get("id") or "").strip()
        if record_id:
            return base + record_id
        payload = f"{todo_record.get('title') or ''}|{todo_record.get('body') or ''}"
        digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
        return base + digest

    def _work_hash(
        self,
        task: dict[str, Any],
        muid: str,
        rendered_text: str,
        todo_record: dict[str, Any] | None,
    ) -> str:
        """Build deterministic work hash for in-flight dedup semantics."""

        task_id = str(task.get("id") or "").strip()
        todo_title = ""
        todo_body = ""
        if isinstance(todo_record, dict):
            todo_title = str(todo_record.get("title") or "").strip()
            todo_body = str(todo_record.get("body") or "").strip()
        payload = "\n".join(
            [
                "v1",
                task_id,
                muid,
                todo_title,
                todo_body,
                rendered_text,
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _resolve_placeholders(self, text: str) -> tuple[str, set[str]]:
        """Resolve ${VAR} and ${VAR:-default}; report unresolved variables."""

        root_dir = str(self.app_config.get("root") or os.getcwd())
        env_file = load_env_file(root_dir)
        unresolved: set[str] = set()

        def _replace(match: re.Match[str]) -> str:
            var = str(match.group(1) or "").strip()
            default = match.group(2)
            value = env_file.get(var)
            if value is None:
                value = os.environ.get(var)
            if value is None:
                if default is not None:
                    return str(default)
                unresolved.add(var)
                return ""
            return str(value)

        return _PLACEHOLDER_RE.sub(_replace, text), unresolved

    def _memoryd_enabled_types(self, task_envid: str | None) -> list[str]:
        """Return enabled memoryd types for one effective envid."""

        effective = build_effective_config(self.app_config, task_envid)
        memoryd_cfg = effective.get("memoryd", {}) if isinstance(effective, dict) else {}
        items_cfg = memoryd_cfg.get("items", {}) if isinstance(memoryd_cfg, dict) else {}
        out: list[str] = []
        if not isinstance(items_cfg, dict):
            return out
        for type_name, type_cfg in items_cfg.items():
            if not isinstance(type_cfg, dict):
                continue
            if not bool(type_cfg.get("enabled", False)):
                continue
            clean = str(type_name).strip().lower()
            if clean:
                out.append(clean)
        return sorted(set(out))

    def _memoryd_auto_writable_types(self, task_envid: str | None) -> list[str]:
        """Return enabled memoryd update types allowed for auto-writes."""

        effective = build_effective_config(self.app_config, task_envid)
        memoryd_cfg = effective.get("memoryd", {}) if isinstance(effective, dict) else {}
        items_cfg = memoryd_cfg.get("items", {}) if isinstance(memoryd_cfg, dict) else {}
        out: list[str] = []
        if not isinstance(items_cfg, dict):
            return out
        for type_name, type_cfg in items_cfg.items():
            if not isinstance(type_cfg, dict):
                continue
            if not bool(type_cfg.get("enabled", False)):
                continue
            if bool(type_cfg.get("manual_only", False)) or bool(type_cfg.get("external_writer", False)):
                continue
            clean = str(type_name).strip().lower()
            if clean:
                out.append(clean)
        return sorted(set(out))

    def _resolve_task_memoryd_types(self, task: dict[str, Any], task_envid: str | None, key: str) -> list[str]:
        """Resolve one task memoryd type list with effective defaults and filtering."""

        effective = build_effective_config(self.app_config, task_envid)
        agents_cfg = effective.get("agents", {}) if isinstance(effective, dict) else {}
        items_cfg = agents_cfg.get("items", {}) if isinstance(agents_cfg, dict) else {}
        agent_cfg = items_cfg.get("memory_executor", {}) if isinstance(items_cfg, dict) else {}
        memoryd_cfg = agent_cfg.get("memoryd", {}) if isinstance(agent_cfg, dict) else {}

        if key == "context_types":
            allowed = self._memoryd_enabled_types(task_envid)
        else:
            allowed = self._memoryd_auto_writable_types(task_envid)

        raw_values = task.get(key)
        if isinstance(raw_values, list) and raw_values:
            requested = [str(item or "").strip().lower() for item in raw_values if str(item or "").strip()]
        else:
            defaults = memoryd_cfg.get(key) if isinstance(memoryd_cfg, dict) else None
            if isinstance(defaults, list) and defaults:
                requested = [str(item or "").strip().lower() for item in defaults if str(item or "").strip()]
            else:
                requested = list(allowed)

        allowed_set = {str(item).strip().lower() for item in allowed if str(item).strip()}
        if not allowed_set:
            resolved: list[str] = []
            for item in requested:
                if item and item not in resolved:
                    resolved.append(item)
            return resolved
        resolved: list[str] = []
        dropped: list[str] = []
        for item in requested:
            if item in allowed_set and item not in resolved:
                resolved.append(item)
            elif item not in allowed_set and item not in dropped:
                dropped.append(item)
        if dropped:
            log(
                "agents",
                "warning",
                (
                    "memory_executor dropped unsupported memoryd types "
                    f"task_id={task.get('id')} field={key} dropped={dropped}"
                ),
            )
        return resolved

    def _todo_json_payload(self, todo_record: dict[str, Any]) -> dict[str, Any] | None:
        """Return parsed todo JSON object when body is valid JSON object."""

        body = str(todo_record.get("body") or "").strip()
        if not body:
            return None
        try:
            payload = json.loads(body)
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def _todo_effective_body(self, todo_record: dict[str, Any]) -> str:
        """Resolve todo text payload according to proposal rules."""

        payload = self._todo_json_payload(todo_record)
        if isinstance(payload, dict) and str(payload.get("text") or "").strip():
            return str(payload.get("text") or "").strip()
        return str(todo_record.get("body") or "").strip()

    def _normalize_types(self, values: Any) -> list[str]:
        """Normalize task type list into compact lowercase strings."""

        if not isinstance(values, list):
            return []
        out: list[str] = []
        for item in values:
            text = str(item or "").strip().lower()
            if text and text not in out:
                out.append(text)
        return out
