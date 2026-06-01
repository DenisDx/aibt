"""Unit checks for memoryd task-level LLM request parameter overrides."""
from __future__ import annotations

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
            "max_tokens": 2000,
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
        self.assertEqual(built_kwargs.get("max_tokens"), 2000)
        self.assertEqual(built_kwargs.get("seed"), 42)
        self.assertEqual(built_kwargs.get("presence_penalty"), 0.1)
        self.assertEqual(built_kwargs.get("frequency_penalty"), 0.2)
        self.assertEqual(built_kwargs.get("top_k"), 40)
        self.assertEqual(built_kwargs.get("min_p"), 0.05)
        self.assertEqual(built_kwargs.get("tools"), ["search"])
        self.assertEqual(svc.store.done_task_id, "task-1")


if __name__ == "__main__":
    unittest.main()