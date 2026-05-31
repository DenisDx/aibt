"""Unit checks for memory_executor store payload normalization."""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agents.memory_executor.agent import MemoryExecutorStore


class MemoryExecutorStoreNormalizeTest(unittest.TestCase):
    """Validate payload guards without DB access."""

    def setUp(self) -> None:
        self.store = MemoryExecutorStore.__new__(MemoryExecutorStore)

    def test_prepare_task_payload_requires_required_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "task.name is required"):
            self.store._prepare_task_payload({"muid": "x", "request_text": "t"})
        with self.assertRaisesRegex(ValueError, "task.muid is required"):
            self.store._prepare_task_payload({"name": "n", "request_text": "t"})
        with self.assertRaisesRegex(ValueError, "task.request_text is required"):
            self.store._prepare_task_payload({"name": "n", "muid": "x"})

    def test_prepare_task_payload_normalizes_fields(self) -> None:
        payload = self.store._prepare_task_payload(
            {
                "name": "  sample  ",
                "muid": "  MUID-1  ",
                "request_text": "  hello  ",
                "context_types": ["Semantic", "semantic", " todo "],
                "update_types": ["Profiles", "profiles"],
                "tools": ["search", "search", "fetch"],
                "execution_policy": " IDLE ",
            }
        )
        self.assertEqual(payload["name"], "sample")
        self.assertEqual(payload["muid"], "muid-1")
        self.assertEqual(payload["request_text"], "hello")
        self.assertEqual(payload["context_types"], ["semantic", "todo"])
        self.assertEqual(payload["update_types"], ["profiles"])
        self.assertEqual(payload["tools"], ["search", "fetch"])
        self.assertEqual(payload["execution_policy"], "idle")
        self.assertTrue(payload["id"])

    def test_prepare_task_payload_whitespace_optional_to_null(self) -> None:
        payload = self.store._prepare_task_payload(
            {
                "name": "sample",
                "muid": "muid-1",
                "request_text": "hello",
                "envid": "\t   ",
                "period_sec": "   ",
                "todo_title": "\t  ",
                "provider": "   ",
                "model": "\t",
                "enqueue_key": "\t   ",
                "tools": ["\t", "   "],
            }
        )
        self.assertIsNone(payload["envid"])
        self.assertIsNone(payload["period_sec"])
        self.assertIsNone(payload["todo_title"])
        self.assertIsNone(payload["provider"])
        self.assertIsNone(payload["model"])
        self.assertIsNone(payload["enqueue_key"])
        self.assertIsNone(payload["tools"])


if __name__ == "__main__":
    unittest.main()
