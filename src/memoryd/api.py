"""Public memoryd facade for agents, adapters, and cron.

Provides: synchronous context retrieval, async update queue, and task worker utilities.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from functools import wraps
import json
import os
from pathlib import Path
import re
import traceback
from typing import Any
from uuid import uuid4

import httpx
import psycopg
from psycopg.rows import dict_row

from agents.llm_factory import build_llm
from core.llm_wiretap import pop_llm_log_context
from core.llm_wiretap import push_llm_log_context
from core.logging_utils import log
from memoryd.schemas import (
    DEFAULT_MEMORYD_LIMIT_PER_TYPE,
    DEFAULT_MEMORYD_TASK_LIMIT,
    MemorydTaskMutation,
    deserialize_json,
    normalize_json_value,
    normalize_muid,
    normalize_types,
    serialize_json,
)
from memoryd.store_pg import MemorydStore
from core.envid_runtime import build_effective_config
from core.envid_runtime import load_environment_registry
from core.envid_runtime import resolve_envid


_SERVICES: dict[tuple[str, int], "MemorydService"] = {}


def _preview_data_block(value: Any, head: int = 100, tail: int = 100) -> str:
    """Build bounded preview string: up to 100 chars from start + 100 from end.

    Input: any payload value.
    Output: preview string with max length 200.
    """

    text = str(value or "")
    cap = max(0, int(head)) + max(0, int(tail))
    if len(text) <= cap:
        return text
    return text[: max(0, int(head))] + text[-max(0, int(tail)) :]


def _muid_lock_key(muid: str) -> int:
    """Return signed 64-bit advisory lock key derived from MUID."""

    import hashlib

    digest = hashlib.sha1(str(muid or "").encode("utf-8")).digest()[:8]
    return int.from_bytes(digest, "big", signed=True)


def _strip_json_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _extract_first_json_block(text: str) -> str | None:
    """Extract the first balanced JSON object/array substring from text."""

    raw = str(text or "")
    start = -1
    for i, ch in enumerate(raw):
        if ch in "[{":
            start = i
            break
    if start < 0:
        return None

    stack: list[str] = []
    in_string = False
    escaped = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if in_string:
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue
        if ch in "[{":
            stack.append(ch)
            continue
        if ch in "]}":
            if not stack:
                return None
            opening = stack.pop()
            if (opening, ch) not in {("[", "]"), ("{", "}")}:
                return None
            if not stack:
                return raw[start : i + 1]
    return None


def _extract_json_payload(content: Any) -> Any:
    if isinstance(content, (dict, list)):
        return content
    if hasattr(content, "model_dump"):
        try:
            content = content.model_dump()
        except Exception:
            pass
    text = str(content or "").strip()
    if not text:
        return []
    try:
        return json.loads(_strip_json_fences(text))
    except Exception as e:
        candidate = _extract_first_json_block(text)
        if candidate:
            try:
                return json.loads(candidate)
            except Exception:
                pass
        preview = _preview_data_block(text)
        log(
            "memoryd",
            "warning",
            f"invalid JSON payload format: {e}; preview_len={len(preview)} raw_len={len(text)} preview={preview}",
        )
        raise ValueError(f"invalid JSON payload format: {e}") from e


class MemorydService:
    """Main memoryd facade.

    Input: project root and app config.
    Output: unified API for record and task operations.
    """

    def __init__(self, root_dir: str, config: dict[str, Any]):
        self.root_dir = root_dir
        self.config = config or {}
        self.memoryd_cfg = self.config.get("memoryd", {}) if isinstance(self.config, dict) else {}
        self.enabled = bool(self.memoryd_cfg.get("enabled", False))
        self.store = MemorydStore(root_dir, config)
        self._initialized = False
        self._muid_lock_conns: dict[str, Any] = {}
        self._dispatch_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="memoryd-dispatch")

    def initialize(self) -> None:
        """Prepare database schema.

        Input: none.
        Output: initialized memoryd service.
        """

        if self._initialized:
            return
        if self.enabled:
            self.store.ensure_schema()
        self._initialized = True

    def list_envids(self) -> list[dict[str, Any]]:
        """Return known envid overlays with memoryd state.

        Input: none.
        Output: list of envid descriptors.
        """

        items: list[dict[str, Any]] = []
        registry = load_environment_registry(self.config)
        if not isinstance(registry, dict):
            return items
        for envid, entry in registry.items():
            effective = build_effective_config(self.config, envid)
            memoryd_cfg = effective.get("memoryd", {}) if isinstance(effective, dict) else {}
            items_cfg = memoryd_cfg.get("items", {}) if isinstance(memoryd_cfg, dict) else {}
            enabled_types: list[str] = []
            if isinstance(items_cfg, dict):
                for type_name, type_cfg in items_cfg.items():
                    if isinstance(type_cfg, dict) and type_cfg.get("enabled", False):
                        clean_type = str(type_name).strip().lower()
                        if clean_type:
                            enabled_types.append(clean_type)
            items.append(
                {
                    "envid": envid,
                    "title": str(entry.get("title") or envid),
                    "enabled": bool(memoryd_cfg.get("enabled", False)),
                    "default_muid": str(memoryd_cfg.get("muid") or "default"),
                    "types": sorted(set(enabled_types)) or ["episodic", "semantic", "summaries", "profiles"],
                }
            )
        return items

    def list_muids(self, limit: int = 200) -> list[str]:
        """Return known MUID values.

        Input: max number of values.
        Output: ordered MUID list.
        """

        self.initialize()
        return self.store.list_muids(limit=max(1, int(limit)))

    def _memoryd_model_cfg(self) -> dict[str, Any]:
        return self.memoryd_cfg if isinstance(self.memoryd_cfg, dict) else {}

    def _models_cfg(self) -> dict[str, Any]:
        cfg = self.config.get("models", {}) if isinstance(self.config, dict) else {}
        return cfg if isinstance(cfg, dict) else {}

    def _provider_cfg(self, provider: str) -> dict[str, Any]:
        models_cfg = self._models_cfg()
        providers = models_cfg.get("providers", {}) if isinstance(models_cfg, dict) else {}
        if not isinstance(providers, dict):
            return {}
        cfg = providers.get(provider, {})
        return cfg if isinstance(cfg, dict) else {}

    def _provider_api(self, provider: str | None = None) -> str:
        """Return normalized API mode for one configured provider."""

        provider_name = str(provider or "").strip() or self._active_provider()
        provider_cfg = self._provider_cfg(provider_name)
        return str(provider_cfg.get("api", "") or "").strip().lower()

    def _active_provider(self) -> str:
        md_cfg = self._memoryd_model_cfg()
        provider = str(md_cfg.get("provider") or "").strip()
        if provider:
            return provider
        models_cfg = self._models_cfg()
        return str(models_cfg.get("active_provider", "") or "default").strip() or "default"

    def _active_model(self) -> str:
        md_cfg = self._memoryd_model_cfg()
        model = str(md_cfg.get("model") or "").strip()
        if model:
            return model
        models_cfg = self._models_cfg()
        provider_cfg = self._provider_cfg(self._active_provider())
        providers = provider_cfg.get("models", []) if isinstance(provider_cfg, dict) else []
        if isinstance(models_cfg, dict) and models_cfg.get("active_model"):
            return str(models_cfg.get("active_model")).strip()
        if isinstance(providers, list) and providers:
            first = providers[0]
            if isinstance(first, dict):
                return str(first.get("id") or first.get("name") or "gpt-4o-mini").strip()
        return "gpt-4o-mini"

    def _llm_logging_enabled(self) -> bool:
        cfg = self._memoryd_model_cfg()
        return bool(cfg.get("log_llm", False)) if isinstance(cfg, dict) else False

    def _llm_log_path(self) -> str:
        logs_dir = os.path.join(self.root_dir, "logs")
        os.makedirs(logs_dir, exist_ok=True)
        return os.path.join(logs_dir, "memoryd_llm.jsonl")

    def _enabled_types(self) -> list[str]:
        items = self._memoryd_model_cfg().get("items", {})
        if not isinstance(items, dict):
            return []
        out: list[str] = []
        for type_name, cfg in items.items():
            if isinstance(cfg, dict) and cfg.get("enabled", False):
                name = str(type_name).strip().lower()
                if name:
                    out.append(name)
        return sorted(set(out))

    def _normalize_types(self, types: list[Any] | None) -> list[str]:
        return normalize_types(types, self._enabled_types())

    def _normalize_muid(self, muid: str | None) -> str:
        clean = normalize_muid(muid)
        if not clean:
            clean = normalize_muid(self._memoryd_model_cfg().get("muid", "default"))
        if not clean:
            clean = "default"
        return clean

    def _request_groups(self) -> list[dict[str, Any]]:
        groups = self._memoryd_model_cfg().get("requests", [])
        return groups if isinstance(groups, list) else []

    def _request_groups_from_cfg(self, memoryd_cfg: dict[str, Any] | None) -> list[dict[str, Any]]:
        """Return grouped request definitions from one memoryd config dict."""

        if not isinstance(memoryd_cfg, dict):
            return []
        groups = memoryd_cfg.get("requests", [])
        return groups if isinstance(groups, list) else []

    def _resolve_request_file(self, types: list[str]) -> str | None:
        normalized = normalize_types(types)
        for item in self._request_groups():
            if not isinstance(item, dict):
                continue
            req_types = normalize_types(item.get("types"))
            if req_types == normalized:
                request_file = str(item.get("request_file") or "").strip()
                if request_file:
                    return request_file
        return None

    def _resolve_request_file_from_memoryd_cfg(self, types: list[str], memoryd_cfg: dict[str, Any] | None) -> str | None:
        """Resolve request_file using one explicit memoryd config payload."""

        normalized = normalize_types(types)
        for item in self._request_groups_from_cfg(memoryd_cfg):
            if not isinstance(item, dict):
                continue
            req_types = normalize_types(item.get("types"))
            if req_types == normalized:
                request_file = str(item.get("request_file") or "").strip()
                if request_file:
                    return request_file
        return None

    def _resolve_task_envid(self, source_context: dict[str, Any]) -> str | None:
        """Resolve envid from source context metadata or adapter routing rules."""

        candidate = str(source_context.get("envid") or "").strip()
        if candidate:
            return candidate

        adapter_name = str(source_context.get("adapter") or "").strip().lower()
        if not adapter_name:
            return None
        try:
            return resolve_envid(self.config, adapter_name=adapter_name, event_context=source_context)
        except Exception:
            return None

    def _resolve_request_file_for_task(self, task: dict[str, Any], types: list[str]) -> str | None:
        """Resolve request_file for task, including envid-effective fallback."""

        request_file = self._resolve_request_file(types)
        if request_file:
            return request_file

        source_context = deserialize_json(task.get("source_context"), {})
        src = source_context if isinstance(source_context, dict) else {}
        envid = self._resolve_task_envid(src)
        if not envid:
            return None

        effective = build_effective_config(self.config, envid)
        memoryd_cfg = effective.get("memoryd", {}) if isinstance(effective, dict) else {}
        request_file = self._resolve_request_file_from_memoryd_cfg(types, memoryd_cfg)
        if request_file:
            log("memoryd", "info", f"request_file resolved from envid={envid} for task_id={task.get('task_id')}")
        return request_file

    def _load_request_text(self, request_file: str) -> str:
        path = Path(request_file)
        if not path.is_absolute():
            path = Path(self.root_dir) / request_file
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")

    def _record_snapshot(self, muid: str, types: list[str], limit_per_type: int) -> list[dict[str, Any]]:
        rows = self.store.list_records(muid=muid, types=types or None, limit=max(1, int(limit_per_type)) * max(1, len(types) or 1))
        grouped: dict[str, list[dict[str, Any]]] = {type_name: [] for type_name in types}
        for row in rows:
            type_name = str(row.get("type") or "").strip().lower()
            if type_name in grouped and len(grouped[type_name]) < max(1, int(limit_per_type)):
                grouped[type_name].append(row)
        out: list[dict[str, Any]] = []
        for type_name in types:
            out.extend(grouped.get(type_name, []))
        return out

    def get_context(self, muid: str | None, types: list[Any] | None = None, render: str = "markdown", limit_per_type: int = DEFAULT_MEMORYD_LIMIT_PER_TYPE) -> dict[str, Any]:
        """Return formatted memory context for prompts.

        Input: MUID and type filter.
        Output: text fragment plus metadata.
        """

        self.initialize()
        muid = self._normalize_muid(muid)
        resolved_types = self._normalize_types(types)
        rows = self.store.list_records(muid=muid, types=resolved_types or None, limit=max(1, int(limit_per_type)) * max(1, len(resolved_types) or 1))
        grouped: dict[str, list[dict[str, Any]]] = {type_name: [] for type_name in resolved_types}
        for row in rows:
            type_name = str(row.get("type") or "").strip().lower()
            if type_name in grouped and len(grouped[type_name]) < max(1, int(limit_per_type)):
                grouped[type_name].append(row)

        lines: list[str] = []
        for type_name in resolved_types:
            items = grouped.get(type_name, [])
            if not items:
                continue
            lines.append(f"[{type_name}]")
            for idx, row in enumerate(items, start=1):
                title = str(row.get("title") or "").strip()
                body = str(row.get("body") or "").strip()
                if render == "markdown":
                    lines.append(f"{idx}. **{title or 'untitled'}**: {body}")
                else:
                    lines.append(f"{idx}. {title or 'untitled'}: {body}")
        text = "\n".join(lines)
        return {
            "text": text,
            "metadata": {
                "muid": muid,
                "types": resolved_types,
                "selected_records_count": sum(len(v) for v in grouped.values()),
                "render": render,
                "truncated": False,
            },
        }

    def list_records(self, muid: str | None, types: list[Any] | None = None, offset: int = 0, limit: int = 100) -> dict[str, Any]:
        """List memory records as a page.

        Input: filters and pagination.
        Output: record page.
        """

        self.initialize()
        muid = self._normalize_muid(muid)
        resolved_types = self._normalize_types(types)
        rows = self.store.list_records(muid=muid, types=resolved_types or None, limit=limit, offset=offset)
        return {
            "items": rows,
            "offset": max(0, int(offset)),
            "limit": max(1, int(limit)),
            "muid": muid,
            "types": resolved_types,
        }

    def enqueue_update(
        self,
        source_context: Any,
        final_response: Any,
        muid: str | None = None,
        caller_tag: str | None = None,
        work_hash: str | None = None,
        request_text: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        repetition_penalty: float | None = None,
        repeat_last_n: int | None = None,
        max_tokens: int | None = None,
        num_predict: int | None = None,
        seed: int | None = None,
        presence_penalty: float | None = None,
        frequency_penalty: float | None = None,
        top_k: int | None = None,
        min_p: float | None = None,
        tools: list[Any] | None = None,
        context_types: list[Any] | None = None,
        types: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Queue async update work for a memory namespace.

        Input: source context, final response, MUID, caller tag, and optional types.
        Output: task receipt.
        """

        self.initialize()
        muid = self._normalize_muid(muid)
        resolved_types = self._normalize_types(types)
        source_context_preview = _preview_data_block(serialize_json(normalize_json_value(source_context)), head=100, tail=100)
        final_response_preview = _preview_data_block(str(final_response or ""), head=100, tail=100)
        request_override = str(request_text or "")
        request_override_preview = _preview_data_block(request_override, head=100, tail=100)
        resolved_context_types = self._normalize_types(context_types)
        norm_provider = str(provider).strip() if provider is not None and str(provider).strip() else None
        norm_model = str(model).strip() if model is not None and str(model).strip() else None
        norm_tools = normalize_json_value(tools) if tools is not None else None
        log(
            "memoryd",
            "info",
            (
                "enqueue memory task params "
                f"muid={muid} caller_tag={str(caller_tag or '').strip() or '<none>'} "
                f"work_hash={str(work_hash or '').strip() or '<none>'} "
                f"provider={norm_provider or '<default>'} model={norm_model or '<default>'} "
                f"request_text_len={len(request_override)} request_text_preview={request_override_preview} "
                f"context_types={resolved_context_types} tools={norm_tools if norm_tools is not None else '<default>'} "
                f"types={resolved_types} "
                f"source_context_len={len(str(source_context_preview))} source_context_preview={source_context_preview} "
                f"final_response_len={len(str(final_response or ''))} final_response_preview={final_response_preview}"
            ),
        )
        norm_caller_tag = str(caller_tag).strip() if caller_tag is not None and str(caller_tag).strip() else None
        norm_work_hash = str(work_hash).strip() if work_hash is not None and str(work_hash).strip() else None
        norm_request_text = str(request_text).strip() if request_text is not None and str(request_text).strip() else None
        norm_temperature = None if temperature is None or str(temperature).strip() == "" else float(temperature)
        norm_top_p = None if top_p is None or str(top_p).strip() == "" else float(top_p)
        norm_repetition_penalty = None if repetition_penalty is None or str(repetition_penalty).strip() == "" else float(repetition_penalty)
        norm_repeat_last_n = None if repeat_last_n is None or str(repeat_last_n).strip() == "" else int(repeat_last_n)
        norm_max_tokens = None if max_tokens is None or str(max_tokens).strip() == "" else int(max_tokens)
        norm_num_predict = None if num_predict is None or str(num_predict).strip() == "" else int(num_predict)
        norm_seed = None if seed is None or str(seed).strip() == "" else int(seed)
        norm_presence_penalty = None if presence_penalty is None or str(presence_penalty).strip() == "" else float(presence_penalty)
        norm_frequency_penalty = None if frequency_penalty is None or str(frequency_penalty).strip() == "" else float(frequency_penalty)
        norm_top_k = None if top_k is None or str(top_k).strip() == "" else int(top_k)
        norm_min_p = None if min_p is None or str(min_p).strip() == "" else float(min_p)
        if norm_work_hash is not None:
            inflight = self.store.find_inflight_tasks(muid, caller_tag=norm_caller_tag, work_hash=norm_work_hash)
            if inflight:
                return {
                    "ok": True,
                    "queued": False,
                    "reason": "duplicate_inflight",
                    "task_id": str(inflight[0].get("task_id") or ""),
                    "muid": muid,
                    "types": resolved_types,
                }
        queue_cfg = self._memoryd_model_cfg().get("queue", {}) if isinstance(self._memoryd_model_cfg(), dict) else {}
        cancel_policy = str(queue_cfg.get("cancel_policy", "cancel_previous_same_muid") or "cancel_previous_same_muid").strip().lower()
        if caller_tag is not None and cancel_policy != "keep_all":
            pending = self.store.find_pending_tasks_by_key(muid, caller_tag=str(caller_tag).strip() or None)
            for row in pending:
                self.store.delete_task(str(row.get("task_id") or ""))
        task_id = str(uuid4())
        self.store.create_task(
            {
                "task_id": task_id,
                "muid": muid,
                "caller_tag": norm_caller_tag,
                "work_hash": norm_work_hash,
                "request_text": norm_request_text,
                "provider": norm_provider,
                "model": norm_model,
                "temperature": norm_temperature,
                "top_p": norm_top_p,
                "repetition_penalty": norm_repetition_penalty,
                "repeat_last_n": norm_repeat_last_n,
                "max_tokens": norm_max_tokens,
                "num_predict": norm_num_predict,
                "seed": norm_seed,
                "presence_penalty": norm_presence_penalty,
                "frequency_penalty": norm_frequency_penalty,
                "top_k": norm_top_k,
                "min_p": norm_min_p,
                "tools": norm_tools,
                "context_types": resolved_context_types,
                "requested_types": resolved_types,
                "source_context": normalize_json_value(source_context),
                "final_response": str(final_response or ""),
                "prio": int(self._memoryd_model_cfg().get("memory_task_prio", 0)),
            }
        )

        self._dispatch_after_enqueue_async(task_id)

        return {"ok": True, "queued": True, "task_id": task_id, "muid": muid, "types": resolved_types}

    def _dispatch_after_enqueue(self, task_id: str) -> None:
        """Run one non-cron worker tick after enqueue in background."""

        try:
            dispatch_result = self.run_tick(limit=1)
            log(
                "memoryd",
                "info",
                (
                    "memoryd immediate dispatch after enqueue "
                    f"task_id={task_id} picked={dispatch_result.get('picked', 0)} "
                    f"started={dispatch_result.get('started', 0)} done={dispatch_result.get('done', 0)} "
                    f"failed={dispatch_result.get('failed', 0)} skipped={dispatch_result.get('skipped', 0)}"
                ),
            )
        except Exception as e:
            log("memoryd", "warning", f"memoryd immediate dispatch attempt failed for task_id={task_id}: {e}")

    def _dispatch_after_enqueue_async(self, task_id: str) -> None:
        """Schedule immediate worker tick without blocking request/response path."""

        try:
            self._dispatch_executor.submit(self._dispatch_after_enqueue, task_id)
            log("memoryd", "info", f"memoryd immediate dispatch scheduled asynchronously for task_id={task_id}")
        except Exception as e:
            log("memoryd", "warning", f"memoryd immediate dispatch scheduling failed for task_id={task_id}: {e}")

    def upsert_record(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Insert or update one record.

        Input: record payload.
        Output: saved row.
        """

        self.initialize()
        if not isinstance(payload, dict):
            raise TypeError("payload must be a dict")
        if "muid" not in payload or "type" not in payload:
            raise ValueError("muid and type are required")
        body = str(payload.get("body") or payload.get("text") or "")
        body_preview = _preview_data_block(body, head=100, tail=100)
        log(
            "memoryd",
            "info",
            (
                "save memory params "
                f"muid={str(payload.get('muid') or '').strip().lower()} "
                f"type={str(payload.get('type') or '').strip().lower()} "
                f"title={str(payload.get('title') or '').strip()} "
                f"importance={int(payload.get('importance', 5))} "
                f"text_len={len(body)} text_preview={body_preview}"
            ),
        )
        return self.store.upsert_record(payload)

    def _queue_state_url(self, provider: str, model: str) -> str:
        provider_cfg = self._provider_cfg(provider)
        base_url = str(provider_cfg.get("baseUrl") or provider_cfg.get("base_url") or os.environ.get("LLM_BASE_URL") or "https://api.openai.com/v1").strip()
        base_url = base_url.rstrip("/")
        if not base_url.endswith("/v1"):
            base_url = f"{base_url}/v1"
        return f"{base_url}/providers/{provider}/models/{model}/queue-state"

    def _queue_state(self, provider: str, model: str, priority: int) -> dict[str, Any] | None:
        url = self._queue_state_url(provider, model)
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(url, params={"priority": priority})
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            log("memoryd", "warning", f"queue-state check failed for {provider}/{model}: {e}")
            return None

    def _build_llm_messages(self, request_text: str, task: dict[str, Any], snapshot: list[dict[str, Any]]) -> list[Any]:
        """Build one system message plus ordinary user/assistant history messages."""

        from langchain_core.messages import SystemMessage
        from agents.message_utils import build_role_aware_messages

        source_context = task.get("source_context")
        final_response = task.get("final_response")
        requested_types = task.get("requested_types") or []
        system_text = (
            f"{request_text}\n\n"
            f"SOURCE_CONTEXT:\n{serialize_json(source_context)}\n\n"
            f"FINAL_RESPONSE:\n{serialize_json(final_response)}\n\n"
            f"REQUESTED_TYPES:\n{serialize_json(requested_types)}\n\n"
            f"CURRENT_RECORDS:\n{serialize_json(snapshot)}\n\n"
            "Return JSON array of memory mutations only."
        )

        source_ctx = source_context if isinstance(source_context, dict) else {}
        query = str(source_ctx.get("query") or source_ctx.get("text") or "").strip()
        messages = [SystemMessage(content=system_text)]
        messages.extend(build_role_aware_messages(query, source_ctx))
        return messages

    def _parse_mutations(self, content: Any) -> list[dict[str, Any]]:
        payload = _extract_json_payload(content)
        if not isinstance(payload, list):
            preview = _preview_data_block(content)
            raise ValueError(
                "invalid mutations format: expected list, "
                f"got {type(payload).__name__}; preview_len={len(preview)} preview={preview}"
            )
        out: list[dict[str, Any]] = []
        invalid_item_preview = ""
        for item in payload:
            if isinstance(item, dict):
                out.append(item)
            elif not invalid_item_preview:
                invalid_item_preview = _preview_data_block(item)
        if invalid_item_preview:
            raise ValueError(
                "invalid mutation item format: expected dict; "
                f"preview_len={len(invalid_item_preview)} preview={invalid_item_preview}"
            )
        return out

    def _acquire_muid_lock(self, muid: str) -> bool:
        """Acquire a persistent advisory lock for one MUID.

        Input: canonical MUID.
        Output: True when lock is acquired.
        """

        if muid in self._muid_lock_conns:
            log("memoryd", "info", f"same-MUID advisory lock already held in-process for muid={muid}; reuse existing lock")
            return True

        lock_key = _muid_lock_key(muid)
        conn = psycopg.connect(**self.store._db_cfg(), autocommit=True, row_factory=dict_row)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (lock_key,))
                row = cur.fetchone()
                raw_locked = list(row.values())[0] if row else None
                locked = bool(raw_locked)
                if locked:
                    self._muid_lock_conns[muid] = conn
                    log(
                        "memoryd",
                        "info",
                        f"same-MUID advisory lock acquired for muid={muid} lock_key={lock_key} pg_try_advisory_lock={raw_locked}",
                    )
                    return True
                log(
                    "memoryd",
                    "warning",
                    (
                        "same-MUID advisory lock busy "
                        f"for muid={muid} lock_key={lock_key} pg_try_advisory_lock={raw_locked}; "
                        "lock is held by another DB session"
                    ),
                )
        except Exception as e:
            log("memoryd", "warning", f"same-MUID advisory lock check failed for muid={muid} lock_key={lock_key}: {e}")
        conn.close()
        return False

    def _release_muid_lock(self, muid: str) -> None:
        """Release persistent advisory lock for one MUID.

        Input: canonical MUID.
        Output: none.
        """

        conn = self._muid_lock_conns.pop(muid, None)
        if conn is None:
            return
        lock_key = _muid_lock_key(muid)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
        except Exception as e:
            log("memoryd", "warning", f"same-MUID advisory unlock failed for {muid}: {e}")
        finally:
            conn.close()

    def _apply_mutation(self, task: dict[str, Any], mutation: dict[str, Any], conn: Any | None = None) -> None:
        muid = str(task.get("muid") or "").strip().lower()
        requested_types = task.get("requested_types") or []
        type_name = str(mutation.get("type") or "").strip().lower()
        record_id = mutation.get("id")
        operation = str(mutation.get("operation") or "").strip().upper()
        title = str(mutation.get("title") or "").strip()
        text = mutation.get("text")
        importance = int(mutation.get("importance", 5))

        if not type_name and len(requested_types) == 1:
            type_name = str(requested_types[0]).strip().lower()

        if record_id is not None:
            existing = self.store.get_record_by_id(record_id, conn=conn)
            if existing and not type_name:
                type_name = str(existing.get("type") or "").strip().lower()

        if not operation:
            if record_id is None:
                operation = "INSERT"
            else:
                operation = "UPDATE" if str(text or "").strip() else "DELETE"

        if operation == "DELETE":
            if record_id is not None:
                self.store.delete_record_by_id(record_id, conn=conn)
                return
            if not (type_name and title):
                raise ValueError("title-based delete requires type and title")
            matches = self.store.find_records_by_title(muid, type_name, title, conn=conn)
            if len(matches) != 1:
                raise ValueError(f"ambiguous or missing title match for delete: {title}")
            self.store.delete_record_by_id(matches[0]["id"], conn=conn)
            return

        if operation in {"INSERT", "UPDATE"}:
            if record_id is not None:
                payload = {
                    "id": record_id,
                    "muid": muid,
                    "type": type_name,
                    "title": title,
                    "body": str(text or ""),
                    "importance": importance,
                }
                self.store.upsert_record(payload, conn=conn)
                return

            if not (type_name and title):
                raise ValueError("title-based insert/update requires type and title")
            matches = self.store.find_records_by_title(muid, type_name, title, conn=conn)
            if len(matches) > 1:
                raise ValueError(f"ambiguous title match for update: {title}")
            if len(matches) == 1:
                payload = {
                    "id": matches[0]["id"],
                    "muid": muid,
                    "type": type_name,
                    "title": title,
                    "body": str(text or ""),
                    "importance": importance,
                }
            else:
                payload = {
                    "muid": muid,
                    "type": type_name,
                    "title": title,
                    "body": str(text or ""),
                    "importance": importance,
                }
            self.store.upsert_record(payload, conn=conn)
            return

        raise ValueError(f"unsupported operation: {operation}")

    def _prune_after_task(self, muid: str, types: list[str], conn: Any | None = None) -> int:
        md_cfg = self._memoryd_model_cfg()
        items_cfg = md_cfg.get("items", {}) if isinstance(md_cfg, dict) else {}
        pruned = 0
        for type_name in types:
            cfg = items_cfg.get(type_name, {}) if isinstance(items_cfg, dict) else {}
            if not isinstance(cfg, dict) or not cfg.get("enabled", False):
                continue
            pruned += self.store.prune_records(
                muid=muid,
                type_name=type_name,
                max_record_count=int(cfg.get("max_record_count", 50)),
                max_content_length=int(cfg.get("max_content_length", 8196)),
                conn=conn,
            )
        return pruned

    def _process_task(self, task: dict[str, Any]) -> dict[str, Any]:
        muid = str(task.get("muid") or "").strip().lower()
        requested_types = [str(x).strip().lower() for x in (deserialize_json(task.get("requested_types"), []) or []) if str(x).strip()]
        context_types = [str(x).strip().lower() for x in (deserialize_json(task.get("context_types"), []) or []) if str(x).strip()]
        request_override = str(task.get("request_text") or "").strip()
        if not muid:
            raise ValueError("task muid is required")
        try:
            provider = str(task.get("provider") or "").strip() or self._active_provider()
            model = self._resolve_model_for_provider(provider, str(task.get("model") or "").strip() or None)
            temperature = task.get("temperature")
            top_p = task.get("top_p")
            repetition_penalty = task.get("repetition_penalty")
            repeat_last_n = task.get("repeat_last_n")
            max_tokens = task.get("max_tokens")
            num_predict = task.get("num_predict")
            seed = task.get("seed")
            presence_penalty = task.get("presence_penalty")
            frequency_penalty = task.get("frequency_penalty")
            top_k = task.get("top_k")
            min_p = task.get("min_p")
            if request_override:
                request_text = request_override
                if not request_text:
                    self.store.mark_task_skipped(task["task_id"], "request_text override is set but empty")
                    return {"status": "skipped", "pruned": 0}
                log(
                    "memoryd",
                    "info",
                    f"memoryd task uses request_text override for task_id={task.get('task_id')} muid={muid} request_text_len={len(request_text)}",
                )
            else:
                request_file = self._resolve_request_file_for_task(task, requested_types)
                if not request_file:
                    self.store.mark_task_skipped(task["task_id"], "no exact request_file for requested_types")
                    log(
                        "memoryd",
                        "warning",
                        f"memoryd task skipped: no exact request_file for task_id={task.get('task_id')} muid={muid} types={requested_types}",
                    )
                    return {"status": "skipped", "pruned": 0}

                request_text = self._load_request_text(request_file)
                if not request_text:
                    self.store.mark_task_skipped(task["task_id"], f"missing request file: {request_file}")
                    return {"status": "skipped", "pruned": 0}

            snapshot = self._record_snapshot(muid, context_types or requested_types, DEFAULT_MEMORYD_LIMIT_PER_TYPE)
            messages = self._build_llm_messages(request_text, task, snapshot)

            task_tools = deserialize_json(task.get("tools"), None)
            build_kwargs: dict[str, Any] = {"provider": provider, "model": model}
            if task_tools is not None:
                build_kwargs["tools"] = task_tools
            if temperature is not None:
                build_kwargs["temperature"] = temperature
            if top_p is not None:
                build_kwargs["top_p"] = top_p
            if repetition_penalty is not None:
                build_kwargs["repetition_penalty"] = repetition_penalty
            if repeat_last_n is not None:
                build_kwargs["repeat_last_n"] = repeat_last_n
            if max_tokens is not None:
                build_kwargs["max_tokens"] = max_tokens
            if num_predict is not None:
                build_kwargs["num_predict"] = num_predict
            if seed is not None:
                build_kwargs["seed"] = seed
            if presence_penalty is not None:
                build_kwargs["presence_penalty"] = presence_penalty
            if frequency_penalty is not None:
                build_kwargs["frequency_penalty"] = frequency_penalty
            if top_k is not None:
                build_kwargs["top_k"] = top_k
            if min_p is not None:
                build_kwargs["min_p"] = min_p
            llm = build_llm(self.config, **build_kwargs)
            log_token = None
            if self._llm_logging_enabled():
                log_token = push_llm_log_context(
                    agent_id="memoryd",
                    envid=task.get("envid") if isinstance(task, dict) else None,
                    log_path=self._llm_log_path(),
                    payload=None,
                )
            try:
                response = llm.invoke(messages)
            finally:
                if log_token is not None:
                    pop_llm_log_context(log_token)
            mutations = self._parse_mutations(getattr(response, "content", response))

            with self.store._tx_conn() as tx_conn:
                for mutation in mutations:
                    self._apply_mutation(task, mutation, conn=tx_conn)

                pruned = self._prune_after_task(muid, requested_types or self._enabled_types(), conn=tx_conn)
                self.store.mark_task_done(task["task_id"], conn=tx_conn)
            return {"status": "done", "pruned": pruned, "mutations": len(mutations)}
        except Exception as e:
            err = str(e)
            self.store.mark_task_failed(task["task_id"], err)
            log("memoryd", "error", f"memoryd task failed task_id={task.get('task_id')} muid={muid}: {err}")
            return {"status": "failed", "error": err, "pruned": 0}

    def _resolve_model_for_provider(self, provider: str, model: str | None = None) -> str:
        """Resolve explicit-or-default model for one provider."""

        clean_provider = str(provider or "").strip()
        clean_model = str(model or "").strip()
        if clean_model:
            return clean_model
        if clean_provider == self._active_provider():
            return self._active_model()

        provider_cfg = self._provider_cfg(clean_provider)
        models = provider_cfg.get("models", []) if isinstance(provider_cfg, dict) else []
        if isinstance(models, list) and models:
            first = models[0]
            if isinstance(first, dict):
                return str(first.get("id") or first.get("name") or "gpt-4o-mini").strip() or "gpt-4o-mini"
        return "gpt-4o-mini"

    def run_tick(self, limit: int | None = None) -> dict[str, Any]:
        """Run memoryd worker tick.

        Input: optional dispatch limit.
        Output: processing counters.
        """

        self.initialize()
        max_tasks = max(1, int(limit or self._memoryd_model_cfg().get("max_sim_task", DEFAULT_MEMORYD_TASK_LIMIT)))
        tasks = self.store.fetch_pending_tasks(max_tasks)
        counters = {"picked": len(tasks), "started": 0, "done": 0, "failed": 0, "pruned": 0, "skipped": 0}

        for task in tasks:
            muid = str(task.get("muid") or "").strip().lower()
            provider = self._active_provider()
            model = self._active_model()
            priority = int(task.get("prio", self._memoryd_model_cfg().get("memory_task_prio", 0)))
            if self._provider_api(provider) == "openaix":
                queue_state = self._queue_state(provider, model, priority)
                if not queue_state or not bool(queue_state.get("can_run_now", False)):
                    continue

            first_try = self._acquire_muid_lock(muid)
            if not first_try:
                task_id = str(task.get("task_id") or "")
                caller_tag = str(task.get("caller_tag") or "").strip() or "<none>"
                lock_key = _muid_lock_key(muid)
                log(
                    "memoryd",
                    "warning",
                    (
                        "same-MUID advisory lock contention "
                        f"task_id={task_id} muid={muid} caller_tag={caller_tag} lock_key={lock_key} "
                        "first_try=False; retrying once"
                    ),
                )
                second_try = self._acquire_muid_lock(muid)
                if not second_try:
                    log(
                        "memoryd",
                        "warning",
                        (
                            "same-MUID advisory lock skip "
                            f"task_id={task_id} muid={muid} caller_tag={caller_tag} lock_key={lock_key} "
                            "second_try=False; task stays pending for next tick"
                        ),
                    )
                    continue

            try:
                if not self.store.mark_task_running(task["task_id"]):
                    log("memoryd", "warning", f"memoryd task race: task_id={task.get('task_id')} skipped")
                    counters["skipped"] += 1
                    continue

                counters["started"] += 1
                result = self._process_task(task)
                status = result.get("status")
                if status == "done":
                    counters["done"] += 1
                    counters["pruned"] += int(result.get("pruned", 0))
                elif status == "skipped":
                    counters["skipped"] += 1
                else:
                    counters["failed"] += 1
            finally:
                try:
                    self._release_muid_lock(muid)
                except Exception:
                    pass

        return counters


def _instrument_memoryd_service_calls() -> None:
    """Wrap MemorydService methods to emit info logs on each call."""

    for name, attr in list(MemorydService.__dict__.items()):
        if name.startswith("__"):
            continue
        if name in {"_instrument_memoryd_service_calls"}:
            continue
        if not callable(attr):
            continue
        if getattr(attr, "_memoryd_call_logged", False):
            continue

        def _make_wrapper(fn: Any, fn_name: str):
            @wraps(fn)
            def _wrapped(self, *args, **kwargs):
                log("memoryd", "info", f"call memoryd.service.{fn_name}")
                return fn(self, *args, **kwargs)

            setattr(_wrapped, "_memoryd_call_logged", True)
            return _wrapped

        setattr(MemorydService, name, _make_wrapper(attr, name))


_instrument_memoryd_service_calls()


def get_memoryd_service(root_dir: str, config: dict[str, Any]) -> MemorydService:
    """Return cached memoryd service for root+config pair.

    Input: root directory and app config.
    Output: initialized MemorydService instance.
    """

    key = (root_dir, id(config))
    svc = _SERVICES.get(key)
    if svc is None:
        svc = MemorydService(root_dir, config)
        _SERVICES[key] = svc
    return svc
