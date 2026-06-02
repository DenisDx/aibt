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
                "temperature": " 0.4 ",
                "top_p": "0.9",
                "repetition_penalty": " 1.2 ",
                "repeat_last_n": " 256 ",
                "max_tokens": "2000",
                "num_predict": " 512 ",
                "seed": " 42 ",
                "presence_penalty": " 0.1 ",
                "frequency_penalty": "0.2",
                "top_k": "40",
                "min_p": " 0.05 ",
                "execution_policy": " IDLE ",
            }
        )
        self.assertEqual(payload["name"], "sample")
        self.assertEqual(payload["muid"], "muid-1")
        self.assertEqual(payload["request_text"], "hello")
        self.assertEqual(payload["context_types"], ["semantic", "todo"])
        self.assertEqual(payload["update_types"], ["profiles"])
        self.assertEqual(payload["tools"], ["search", "fetch"])
        self.assertEqual(payload["temperature"], 0.4)
        self.assertEqual(payload["top_p"], 0.9)
        self.assertEqual(payload["repetition_penalty"], 1.2)
        self.assertEqual(payload["repeat_last_n"], 256)
        self.assertEqual(payload["max_tokens"], 2000)
        self.assertEqual(payload["num_predict"], 512)
        self.assertEqual(payload["seed"], 42)
        self.assertEqual(payload["presence_penalty"], 0.1)
        self.assertEqual(payload["frequency_penalty"], 0.2)
        self.assertEqual(payload["top_k"], 40)
        self.assertEqual(payload["min_p"], 0.05)
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
                "temperature": " \t ",
                "top_p": " ",
                "repetition_penalty": "\t",
                "repeat_last_n": "\t",
                "max_tokens": "   ",
                "num_predict": " ",
                "seed": " ",
                "presence_penalty": "\t ",
                "frequency_penalty": "",
                "top_k": "   ",
                "min_p": "\t",
                "enqueue_key": "\t   ",
                "tools": ["\t", "   "],
            }
        )
        self.assertIsNone(payload["envid"])
        self.assertIsNone(payload["period_sec"])
        self.assertIsNone(payload["todo_title"])
        self.assertIsNone(payload["provider"])
        self.assertIsNone(payload["model"])
        self.assertIsNone(payload["temperature"])
        self.assertIsNone(payload["top_p"])
        self.assertIsNone(payload["repetition_penalty"])
        self.assertIsNone(payload["repeat_last_n"])
        self.assertIsNone(payload["max_tokens"])
        self.assertIsNone(payload["num_predict"])
        self.assertIsNone(payload["seed"])
        self.assertIsNone(payload["presence_penalty"])
        self.assertIsNone(payload["frequency_penalty"])
        self.assertIsNone(payload["top_k"])
        self.assertIsNone(payload["min_p"])
        self.assertIsNone(payload["enqueue_key"])
        self.assertIsNone(payload["tools"])


if __name__ == "__main__":
    unittest.main()
