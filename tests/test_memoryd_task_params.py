"""Unit checks for memoryd task-level LLM request parameter overrides."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import sys
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from memoryd.api import MemorydService


class _DummyStore:
    """Minimal store stub for _process_task unit tests."""

    def __init__(self) -> None:
        self.done_task_id = None
        self.failed_task_id = None
        self.failed_error = None

    @contextmanager
    def _tx_conn(self):
        yield object()

    def mark_task_done(self, task_id: str, conn=None) -> None:
        _ = conn
        self.done_task_id = task_id

    def mark_task_failed(self, task_id: str, error: str) -> None:
        self.failed_task_id = task_id
        self.failed_error = error

    def mark_task_skipped(self, task_id: str, reason: str) -> None:
        raise AssertionError(f"task should not be skipped: {task_id} {reason}")

    def update_task_phase(self, task_id: str, phase: str, conn=None) -> None:
        _ = (task_id, phase, conn)

    def update_task_error(self, task_id: str, error: str) -> None:
        _ = (task_id, error)


class _QueueingStore:
    """Minimal store stub for enqueue_update tests."""

    def __init__(self) -> None:
        self.created_task = None

    def find_inflight_tasks(self, muid, caller_tag=None, work_hash=None):
        _ = (muid, caller_tag, work_hash)
        return []

    def find_pending_tasks_by_key(self, muid, caller_tag=None):
        _ = (muid, caller_tag)
        return []

    def delete_task(self, task_id):
        _ = task_id

    def create_task(self, task):
        self.created_task = dict(task)

    def update_task_phase(self, task_id: str, phase: str, conn=None) -> None:
        _ = (task_id, phase, conn)

    def update_task_error(self, task_id: str, error: str) -> None:
        _ = (task_id, error)


class _ListActiveStore:
    """Minimal store stub for list_active_tasks/watchdog tests."""

    def list_active_tasks(self, envid=None, limit=200, offset=0):
        _ = (envid, limit, offset)
        started_at = datetime.now(timezone.utc) - timedelta(seconds=95)
        created_at = started_at - timedelta(seconds=10)
        return {
            "items": [
                {
                    "task_id": "task-1",
                    "status": "running",
                    "phase": "invoke_llm",
                    "muid": "m1",
                    "requested_types": ["semantic"],
                    "context_types": ["semantic"],
                    "tools": None,
                    "source_context": {},
                    "created_at": created_at,
                    "started_at": started_at,
                    "updated_at": started_at,
                    "error": "",
                }
            ],
            "total": 1,
            "limit": limit,
            "offset": offset,
        }


class MemorydTaskParamsTest(unittest.TestCase):
    """Validate that queued task overrides are passed into build_llm."""

    def test_process_task_passes_request_params_to_build_llm(self) -> None:
        svc = MemorydService.__new__(MemorydService)
        svc.root_dir = ROOT
        svc.config = {}
        svc.memoryd_cfg = {}
        svc.store = _DummyStore()
        svc._active_provider = lambda: "default"
        svc._resolve_model_for_provider = lambda provider, model=None: str(model or "gpt-test")
        svc._record_snapshot = lambda muid, types, limit: []
        svc._enabled_types = lambda: ["semantic"]
        svc._llm_logging_enabled = lambda: False
        svc._prune_after_task = lambda muid, types, conn=None: 0

        built_kwargs = {}

        def _fake_build_llm(config, **kwargs):
            _ = config
            built_kwargs.update(kwargs)

            class _FakeLLM:
                def invoke(self, messages):
                    self.messages = messages
                    return SimpleNamespace(content="[]")

            return _FakeLLM()

        task = {
            "task_id": "task-1",
            "muid": "muid-1",
            "requested_types": ["semantic"],
            "context_types": ["semantic"],
            "source_context": {},
            "final_response": "done",
            "request_text": "Write semantic memories.",
            "provider": "openaix",
            "model": "gpt-test",
            "temperature": 0.4,
            "top_p": 0.9,
            "repetition_penalty": 1.2,
            "repeat_last_n": 256,
            "max_tokens": 2000,
            "num_predict": 512,
            "seed": 42,
            "presence_penalty": 0.1,
            "frequency_penalty": 0.2,
            "top_k": 40,
            "min_p": 0.05,
            "tools": ["search"],
        }

        with patch("memoryd.api.build_llm", side_effect=_fake_build_llm):
            result = svc._process_task(task)

        self.assertEqual(result.get("status"), "done")
        self.assertEqual(built_kwargs.get("provider"), "openaix")
        self.assertEqual(built_kwargs.get("model"), "gpt-test")
        self.assertEqual(built_kwargs.get("temperature"), 0.4)
        self.assertEqual(built_kwargs.get("top_p"), 0.9)
        self.assertEqual(built_kwargs.get("repetition_penalty"), 1.2)
        self.assertEqual(built_kwargs.get("repeat_last_n"), 256)
        self.assertEqual(built_kwargs.get("max_tokens"), 2000)
        self.assertEqual(built_kwargs.get("num_predict"), 512)
        self.assertEqual(built_kwargs.get("seed"), 42)
        self.assertEqual(built_kwargs.get("presence_penalty"), 0.1)
        self.assertEqual(built_kwargs.get("frequency_penalty"), 0.2)
        self.assertEqual(built_kwargs.get("top_k"), 40)
        self.assertEqual(built_kwargs.get("min_p"), 0.05)
        self.assertEqual(built_kwargs.get("tools"), ["search"])
        self.assertEqual(svc.store.done_task_id, "task-1")

    def test_run_tick_uses_provider_api_mode_for_openaix_queue_state(self) -> None:
        svc = MemorydService.__new__(MemorydService)
        svc.root_dir = ROOT
        svc.config = {
            "models": {
                "active_provider": "default",
                "providers": {
                    "default": {
                        "api": "openaix",
                        "baseUrl": "https://example.invalid/v1",
                        "apiKey": "secret",
                    }
                },
            }
        }
        svc.memoryd_cfg = {"memory_task_prio": 8}
        svc.initialize = lambda: None

        class _TickStore:
            def fetch_pending_tasks(self, limit):
                _ = limit
                return [{"task_id": "task-1", "muid": "m1", "prio": 8}]

            def update_task_phase(self, task_id: str, phase: str, conn=None) -> None:
                _ = (task_id, phase, conn)

            def update_task_error(self, task_id: str, error: str) -> None:
                _ = (task_id, error)

        svc.store = _TickStore()
        svc._active_provider = lambda: "default"
        svc._active_model = lambda: "gpt-test"

        called = {}

        def _fake_queue_state(provider, model, priority):
            called["provider"] = provider
            called["model"] = model
            called["priority"] = priority
            return {"can_run_now": False}

        svc._queue_state = _fake_queue_state

        result = svc.run_tick(limit=1)

        self.assertEqual(called.get("provider"), "default")
        self.assertEqual(called.get("model"), "gpt-test")
        self.assertEqual(called.get("priority"), 8)
        self.assertEqual(result.get("picked"), 1)
        self.assertEqual(result.get("started"), 0)
        self.assertEqual(result.get("done"), 0)
        self.assertEqual(result.get("failed"), 0)

    def test_enqueue_update_schedules_async_dispatch_immediately(self) -> None:
        svc = MemorydService.__new__(MemorydService)
        svc.initialize = lambda: None
        svc._normalize_muid = lambda muid: str(muid or "default")
        svc._normalize_types = lambda values: [str(v) for v in (values or [])]
        svc._memoryd_model_cfg = lambda: {"memory_task_prio": 8}
        svc.store = _QueueingStore()

        scheduled = {}

        def _fake_dispatch(task_id):
            scheduled["task_id"] = task_id

        svc._dispatch_after_enqueue_async = _fake_dispatch

        result = svc.enqueue_update(
            source_context={"adapter": "test"},
            final_response="ok",
            muid="m1",
            caller_tag="c1",
            request_text="Write memories",
            types=["semantic"],
        )

        self.assertTrue(result.get("queued"))
        self.assertEqual(scheduled.get("task_id"), result.get("task_id"))
        self.assertEqual(svc.store.created_task.get("phase"), "queued")

    def test_list_active_tasks_adds_phase_reason_and_watchdog(self) -> None:
        svc = MemorydService.__new__(MemorydService)
        svc.initialize = lambda: None
        svc.store = _ListActiveStore()
        svc.memoryd_cfg = {"running_watchdog_seconds": 30}

        page = svc.list_active_tasks(limit=10)

        self.assertEqual(page.get("total"), 1)
        item = page["items"][0]
        self.assertEqual(item.get("phase"), "invoke_llm")
        self.assertEqual(item.get("watchdog_state"), "overdue")
        self.assertIn("Running for", item.get("watchdog_message") or "")
        self.assertEqual(item.get("reason"), "Waiting for LLM response.")


if __name__ == "__main__":
    unittest.main()