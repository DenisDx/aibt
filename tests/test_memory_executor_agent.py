"""Unit checks for memory_executor helper logic."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agents.memory_executor.agent import MemoryExecutorAgent


class MemoryExecutorAgentHelpersTest(unittest.TestCase):
    """Validate deterministic helper behavior without DB runtime."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.agent = MemoryExecutorAgent.__new__(MemoryExecutorAgent)
        self.agent.app_config = {"root": self.tmpdir.name}

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_resolve_placeholders_with_default_and_env(self) -> None:
        os.environ["AIBT_TEST_ME_VAR"] = "live-value"
        try:
            text = "alpha=${AIBT_TEST_ME_VAR} beta=${UNSET_VAR:-fallback} gamma=${UNSET_NO_DEFAULT}"
            resolved, unresolved = self.agent._resolve_placeholders(text)
            self.assertIn("alpha=live-value", resolved)
            self.assertIn("beta=fallback", resolved)
            self.assertIn("gamma=", resolved)
            self.assertEqual(unresolved, {"UNSET_NO_DEFAULT"})
        finally:
            os.environ.pop("AIBT_TEST_ME_VAR", None)

    def test_enqueue_key_prefers_custom_value(self) -> None:
        task = {"id": "task-1", "enqueue_key": "custom-key"}
        key = self.agent._enqueue_key(task, None)
        self.assertEqual(key, "custom-key")

    def test_enqueue_key_uses_todo_record_id_when_present(self) -> None:
        task = {"id": "task-2"}
        todo = {"id": 17, "title": "x", "body": "y"}
        key = self.agent._enqueue_key(task, todo)
        self.assertEqual(key, "mx:task-2:17")

    def test_work_hash_changes_when_text_changes(self) -> None:
        task = {"id": "task-3"}
        h1 = self.agent._work_hash(task=task, muid="muid-1", rendered_text="one", todo_record=None)
        h2 = self.agent._work_hash(task=task, muid="muid-1", rendered_text="two", todo_record=None)
        self.assertNotEqual(h1, h2)

    def test_todo_status_gate(self) -> None:
        done_row = {"body": '{"status": "done"}'}
        cancelled_row = {"body": '{"status": "cancelled"}'}
        error_row = {"body": '{"status": "error"}'}
        active_row = {"body": '{"status": "in_progress"}'}
        self.assertFalse(self.agent._todo_should_enqueue(done_row))
        self.assertFalse(self.agent._todo_should_enqueue(cancelled_row))
        self.assertFalse(self.agent._todo_should_enqueue(error_row))
        self.assertTrue(self.agent._todo_should_enqueue(active_row))

    def test_merge_request_with_todo_prefers_json_text_field(self) -> None:
        todo = {
            "title": "collect-news",
            "body": '{"status":"in_progress","text":"focus on launch announcements"}',
        }
        merged = self.agent._merge_request_with_todo("base request", todo)
        self.assertIn("TODO_TITLE:\ncollect-news", merged)
        self.assertIn("TODO_BODY:\nfocus on launch announcements", merged)
        self.assertNotIn('"status":"in_progress"', merged)

    def test_enqueue_one_passes_request_text(self) -> None:
        class _Svc:
            def __init__(self):
                self.last = None

            def enqueue_update(self, **kwargs):
                self.last = kwargs
                return {"queued": True}

        svc = _Svc()
        task = {
            "id": "task-1",
            "muid": "muid-1",
            "envid": "envid-1",
            "provider": "openaix",
            "model": "gpt-test",
            "tools": ["search"],
            "context_types": ["semantic", "todo"],
            "update_types": ["news"],
        }
        queued = self.agent._enqueue_one(
            task=task,
            task_envid="envid-1",
            memoryd_service=svc,
            rendered_text="prompt text",
            todo_record=None,
            now_utc=__import__("datetime").datetime.utcnow(),
        )
        self.assertTrue(queued)
        self.assertIsNotNone(svc.last)
        self.assertEqual(svc.last.get("request_text"), "prompt text")
        self.assertEqual(svc.last.get("final_response"), "prompt text")
        self.assertEqual(svc.last.get("provider"), "openaix")
        self.assertEqual(svc.last.get("model"), "gpt-test")
        self.assertEqual(svc.last.get("tools"), ["search"])
        self.assertEqual(svc.last.get("context_types"), ["semantic", "todo"])
        self.assertEqual(svc.last.get("types"), ["news"])


if __name__ == "__main__":
    unittest.main()
