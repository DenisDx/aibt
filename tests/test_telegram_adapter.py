"""Focused checks for Telegram adapter timeout and late-delivery behavior."""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from adapters.telegram.app import TelegramAdapter


class _SequenceOrchestrator:
    """Return predefined task snapshots in sequence for polling tests."""

    def __init__(self, snapshots: list[dict]) -> None:
        self._snapshots = list(snapshots)
        self._last = snapshots[-1] if snapshots else {}

    def get_task(self, task_id: str) -> dict:
        """Return next snapshot for the requested task id."""

        if self._snapshots:
            self._last = self._snapshots.pop(0)
        return dict(self._last)


class TelegramAdapterLateDeliveryTest(unittest.IsolatedAsyncioTestCase):
    """Verify that timed-out Telegram requests can still be delivered later."""

    def _build_adapter(self, snapshots: list[dict]) -> tuple[TelegramAdapter, list[tuple[int, str, int | None]]]:
        """Build a minimal TelegramAdapter instance for isolated helper tests."""

        adapter = TelegramAdapter.__new__(TelegramAdapter)
        adapter.orchestrator = _SequenceOrchestrator(snapshots)
        adapter.dialog_log_type = "telegram_dialog"
        adapter._recent_group_messages = {}

        sent_messages: list[tuple[int, str, int | None]] = []

        async def _send_message(chat_id: int, text: str, reply_to_message_id: int | None = None) -> bool:
            sent_messages.append((chat_id, text, reply_to_message_id))
            return True

        adapter.send_message = _send_message
        adapter._remember_recent_assistant_message = lambda chat_id, text: None
        adapter._log_dialog_event_data = lambda **kwargs: None
        return adapter, sent_messages

    async def test_deliver_task_result_late_sends_reply_after_timeout(self) -> None:
        adapter, sent_messages = self._build_adapter(
            [
                {"status": "running", "result": None},
                {"status": "done", "result": {"result": "__REPLY__:104 Delayed hello"}},
            ]
        )

        await adapter._deliver_task_result_late(
            task_id="task-1",
            chat_id=123,
            incoming_text="hello?",
            username="denisdx",
            late_timeout_seconds=1,
            poll_interval=0.01,
        )

        self.assertEqual(sent_messages, [(123, "Delayed hello", 104)])

    async def test_deliver_task_result_late_skips_model_requested_silence(self) -> None:
        adapter, sent_messages = self._build_adapter(
            [
                {"status": "done", "result": {"skip_send": True, "skip_reason": "ignored_by_model_decision"}},
            ]
        )

        await adapter._deliver_task_result_late(
            task_id="task-2",
            chat_id=123,
            incoming_text="hello?",
            username="denisdx",
            late_timeout_seconds=1,
            poll_interval=0.01,
        )

        self.assertEqual(sent_messages, [])


if __name__ == "__main__":
    unittest.main()