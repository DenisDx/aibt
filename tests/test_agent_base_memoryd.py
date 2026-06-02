"""Focused checks for AgentBase MemoryD caller_tag resolution."""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agents.base import AgentBase


class _DummyAgent(AgentBase):
    """Minimal concrete AgentBase for helper-method tests."""

    def build_chain(self):
        return None


class AgentBaseMemorydCallerTagTest(unittest.TestCase):
    """Verify stable caller_tag selection for MemoryD enqueue."""

    def _agent(self) -> _DummyAgent:
        agent = _DummyAgent.__new__(_DummyAgent)
        agent.name = "dummy-agent"
        return agent

    def test_telegram_group_uses_chat_id_as_memoryd_caller_tag(self) -> None:
        agent = self._agent()

        caller_tag = agent._memoryd_caller_tag(
            {
                "adapter": "telegram",
                "chat_type": "group",
                "chat_id": -1001234567890,
                "task_id": "unique-task-id",
            }
        )

        self.assertEqual(caller_tag, "-1001234567890")

    def test_explicit_caller_tag_still_wins(self) -> None:
        agent = self._agent()

        caller_tag = agent._memoryd_caller_tag(
            {
                "adapter": "telegram",
                "chat_type": "supergroup",
                "chat_id": -1001234567890,
                "task_id": "unique-task-id",
                "caller_tag": "explicit-tag",
            }
        )

        self.assertEqual(caller_tag, "explicit-tag")

    def test_non_group_keeps_task_id_fallback(self) -> None:
        agent = self._agent()

        caller_tag = agent._memoryd_caller_tag(
            {
                "adapter": "telegram",
                "chat_type": "private",
                "chat_id": 123,
                "task_id": "unique-task-id",
            }
        )

        self.assertEqual(caller_tag, "unique-task-id")


if __name__ == "__main__":
    unittest.main()