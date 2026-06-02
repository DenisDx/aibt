"""HTTP-level tests for memoryd WebUI enqueue endpoint request parameter forwarding."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from webui.backend.app import WebUIServer


class _DummyMemoryService:
    """Stub memory service for WebUIServer constructor in tests."""


class _FakeMemorydService:
    """Capture enqueue_update calls for memoryd API tests."""

    def __init__(self) -> None:
        self.last_enqueue = None

    def enqueue_update(self, **kwargs):
        self.last_enqueue = dict(kwargs)
        return {"ok": True, "queued": True, "task_id": "task-1"}


class _FakeWebUIAdapter:
    """Minimal webui adapter stub for orchestrator dependency."""

    async def handle(self, user_id: str, agent_id: str, message: str, context: dict | None = None) -> dict:
        _ = (user_id, agent_id, message, context)
        return {"task_id": "fake-task"}


class _FakeOrchestrator:
    """Minimal orchestrator stub used by WebUIServer in endpoint tests."""

    def __init__(self, config: dict | None = None, **_: dict):
        self.config = config or {}
        self.adapters = {"webui": _FakeWebUIAdapter()}

    def list_agents(self) -> list[str]:
        return ["echo"]

    def get_agent_info(self, agent: str, limit: int = 20) -> dict:
        _ = (agent, limit)
        return {"agent": {"id": "echo", "type": "fake", "module": "fake"}, "stats": {}, "recent_tasks": []}

    def get_task(self, task_id: str):
        _ = task_id
        return None


class MemorydApiTest(unittest.TestCase):
    """Verify memoryd enqueue endpoint forwards all request parameters."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = self.tmpdir.name
        self.config = {
            "root": self.root,
            "webui": {
                "auth": {
                    "users": [
                        {"login": "admin", "password": "admin", "permissions": ["*"]}
                    ]
                }
            },
        }
        self.fake_memoryd = _FakeMemorydService()
        self._patches = [
            patch("orchestrator.orchestrator.AgentOrchestrator", _FakeOrchestrator),
            patch("webui.backend.app.get_memory_service", return_value=_DummyMemoryService()),
            patch("webui.backend.app.get_memoryd_service", return_value=self.fake_memoryd),
        ]
        for patcher in self._patches:
            patcher.start()
        self.server = WebUIServer(self.root, self.config)
        self.server._memoryd_service_for_envid = lambda envid=None: self.fake_memoryd
        self.client = TestClient(self.server.app)
        login_res = self.client.post("/api/auth/login", json={"login": "admin", "password": "admin"})
        self.assertEqual(login_res.status_code, 200)

    def tearDown(self) -> None:
        for patcher in reversed(self._patches):
            patcher.stop()
        self.tmpdir.cleanup()

    def test_memoryd_enqueue_forwards_all_request_params(self) -> None:
        payload = {
            "source_context": {"adapter": "webui", "envid": "prod"},
            "final_response": "assistant output",
            "muid": "chat-1",
            "caller_tag": "manual-test",
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
            "types": ["semantic"],
        }

        res = self.client.post("/api/memoryd/tasks/enqueue", json=payload)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        self.assertTrue(data.get("queued"))
        self.assertEqual(self.fake_memoryd.last_enqueue.get("temperature"), 0.4)
        self.assertEqual(self.fake_memoryd.last_enqueue.get("top_p"), 0.9)
        self.assertEqual(self.fake_memoryd.last_enqueue.get("repetition_penalty"), 1.2)
        self.assertEqual(self.fake_memoryd.last_enqueue.get("repeat_last_n"), 256)
        self.assertEqual(self.fake_memoryd.last_enqueue.get("max_tokens"), 2000)
        self.assertEqual(self.fake_memoryd.last_enqueue.get("num_predict"), 512)
        self.assertEqual(self.fake_memoryd.last_enqueue.get("seed"), 42)
        self.assertEqual(self.fake_memoryd.last_enqueue.get("presence_penalty"), 0.1)
        self.assertEqual(self.fake_memoryd.last_enqueue.get("frequency_penalty"), 0.2)
        self.assertEqual(self.fake_memoryd.last_enqueue.get("top_k"), 40)
        self.assertEqual(self.fake_memoryd.last_enqueue.get("min_p"), 0.05)


if __name__ == "__main__":
    unittest.main()