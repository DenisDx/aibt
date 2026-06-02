"""HTTP-level tests for memory_executor WebUI API endpoints."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from webui.backend.app import WebUIServer


class _DummyMemoryService:
    """Stub memory service for WebUIServer constructor in tests."""


class _DummyMemorydService:
    """Stub memoryd service for WebUIServer constructor in tests."""


class _FakeWebUIAdapter:
    """Minimal webui adapter stub for orchestrator dependency."""

    async def handle(self, user_id: str, agent_id: str, message: str, context: dict | None = None) -> dict:
        return {"task_id": "fake-task", "agent": agent_id, "submitted_by": user_id}


class _FakeOrchestrator:
    """Minimal orchestrator stub used by WebUIServer in endpoint tests."""

    def __init__(self, config: dict | None = None, **_: dict):
        self.config = config or {}
        self.adapters = {"webui": _FakeWebUIAdapter()}
        self.tasks = {}
        self.last_runtime = None

    async def run_cron_tick_hooks(self, runtime: dict | None = None) -> dict:
        self.last_runtime = dict(runtime or {})
        return {"called": 1, "failed": 0}

    async def run_init_hooks(self, runtime: dict | None = None) -> dict:
        _ = runtime
        return {"called": 0, "failed": 0}

    async def run_shutdown_hooks(self, runtime: dict | None = None) -> dict:
        _ = runtime
        return {"called": 0, "failed": 0}

    def list_agents(self) -> list[str]:
        return ["echo"]

    def get_agent_info(self, agent: str, limit: int = 20) -> dict:
        _ = limit
        return {"agent": {"id": agent, "type": "fake", "module": "fake"}, "stats": {}, "recent_tasks": []}

    def get_task(self, task_id: str):
        return self.tasks.get(task_id)


class _FakeMemoryExecutorStore:
    """In-memory task store implementing required API used by endpoints."""

    def __init__(self):
        self.items: dict[str, dict] = {}

    def list_tasks(self, envid: str | None = None, limit: int = 200, offset: int = 0) -> dict:
        rows = list(self.items.values())
        if envid is not None:
            rows = [row for row in rows if str(row.get("envid") or "") == str(envid)]
        rows = sorted(rows, key=lambda r: str(r.get("id") or ""))
        total = len(rows)
        sliced = rows[offset : offset + limit]
        return {"items": sliced, "total": total, "limit": limit, "offset": offset}

    def create_task(self, payload: dict) -> dict:
        item = dict(payload or {})
        item_id = str(item.get("id") or uuid4())
        item["id"] = item_id
        self.items[item_id] = item
        return dict(item)

    def update_task(self, task_id: str, payload: dict) -> dict | None:
        if task_id not in self.items:
            return None
        item = dict(payload or {})
        item["id"] = task_id
        self.items[task_id] = item
        return dict(item)

    def delete_task(self, task_id: str) -> bool:
        return self.items.pop(task_id, None) is not None


class MemoryExecutorApiTest(unittest.TestCase):
    """Verify memory_executor endpoints through HTTP layer."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = self.tmpdir.name
        os.makedirs(os.path.join(self.root, "agent_files"), exist_ok=True)
        with open(os.path.join(self.root, "agent_files", "mx_template.md"), "w", encoding="utf-8") as f:
            f.write("Template body text")

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

        self.fake_store = _FakeMemoryExecutorStore()

        self._patches = [
            patch("orchestrator.orchestrator.AgentOrchestrator", _FakeOrchestrator),
            patch("webui.backend.app.get_memory_service", return_value=_DummyMemoryService()),
            patch("webui.backend.app.get_memoryd_service", return_value=_DummyMemorydService()),
        ]
        for p in self._patches:
            p.start()

        self.server = WebUIServer(self.root, self.config)
        self.server._memory_executor_store_for_envid = lambda envid=None: self.fake_store
        self.server._memory_executor_run_task_now = lambda task_id: {
            "ok": True,
            "found": task_id in self.fake_store.items,
            "queued": task_id in self.fake_store.items,
            "task_id": task_id,
            "reason": None if task_id in self.fake_store.items else "task_not_found",
        }
        self.client = TestClient(self.server.app)

        login_res = self.client.post("/api/auth/login", json={"login": "admin", "password": "admin"})
        self.assertEqual(login_res.status_code, 200)

    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        self.tmpdir.cleanup()

    def test_templates_endpoint(self) -> None:
        res = self.client.get("/api/memory-executor/templates")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        items = data.get("items") or []
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].get("name"), "mx_template.md")
        self.assertIn("Template body text", str(items[0].get("text") or ""))

    def test_tasks_crud_flow(self) -> None:
        create_payload = {
            "task": {
                "name": "nightly-summary",
                "enabled": True,
                "envid": "",
                "muid": "chat-1",
                "period_sec": 3600,
                "request_text": "Summarize events",
                "todo_title": None,
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
                "execution_policy": "idle",
            }
        }
        create_res = self.client.post("/api/memory-executor/tasks", json=create_payload)
        self.assertEqual(create_res.status_code, 200)
        created = create_res.json().get("item") or {}
        task_id = str(created.get("id") or "")
        self.assertTrue(task_id)

        list_res = self.client.get("/api/memory-executor/tasks")
        self.assertEqual(list_res.status_code, 200)
        list_items = list_res.json().get("items") or []
        self.assertEqual(len(list_items), 1)
        self.assertEqual(str(list_items[0].get("id") or ""), task_id)
        self.assertEqual(list_items[0].get("temperature"), 0.4)
        self.assertEqual(list_items[0].get("top_p"), 0.9)
        self.assertEqual(list_items[0].get("repetition_penalty"), 1.2)
        self.assertEqual(list_items[0].get("repeat_last_n"), 256)
        self.assertEqual(list_items[0].get("max_tokens"), 2000)
        self.assertEqual(list_items[0].get("num_predict"), 512)
        self.assertEqual(list_items[0].get("seed"), 42)
        self.assertEqual(list_items[0].get("presence_penalty"), 0.1)
        self.assertEqual(list_items[0].get("frequency_penalty"), 0.2)
        self.assertEqual(list_items[0].get("top_k"), 40)
        self.assertEqual(list_items[0].get("min_p"), 0.05)

        update_payload = {
            "task": {
                "name": "nightly-summary-updated",
                "enabled": False,
                "envid": "",
                "muid": "chat-1",
                "period_sec": 7200,
                "request_text": "Summarize and compact",
                "todo_title": "todo",
                "temperature": 0.1,
                "top_p": 0.5,
                "repetition_penalty": 1.05,
                "repeat_last_n": 128,
                "max_tokens": 777,
                "num_predict": 333,
                "seed": 7,
                "presence_penalty": 0.3,
                "frequency_penalty": 0.4,
                "top_k": 12,
                "min_p": 0.07,
                "execution_policy": "always",
            }
        }
        update_res = self.client.put(f"/api/memory-executor/tasks/{task_id}", json=update_payload)
        self.assertEqual(update_res.status_code, 200)
        updated = update_res.json().get("item") or {}
        self.assertEqual(updated.get("name"), "nightly-summary-updated")
        self.assertFalse(bool(updated.get("enabled")))
        self.assertEqual(updated.get("temperature"), 0.1)
        self.assertEqual(updated.get("top_p"), 0.5)
        self.assertEqual(updated.get("repetition_penalty"), 1.05)
        self.assertEqual(updated.get("repeat_last_n"), 128)
        self.assertEqual(updated.get("max_tokens"), 777)
        self.assertEqual(updated.get("num_predict"), 333)
        self.assertEqual(updated.get("seed"), 7)
        self.assertEqual(updated.get("presence_penalty"), 0.3)
        self.assertEqual(updated.get("frequency_penalty"), 0.4)
        self.assertEqual(updated.get("top_k"), 12)
        self.assertEqual(updated.get("min_p"), 0.07)

        delete_res = self.client.delete(f"/api/memory-executor/tasks/{task_id}")
        self.assertEqual(delete_res.status_code, 200)
        self.assertTrue(bool(delete_res.json().get("deleted")))

        list_after_delete = self.client.get("/api/memory-executor/tasks")
        self.assertEqual(list_after_delete.status_code, 200)
        self.assertEqual(list_after_delete.json().get("items"), [])

    def test_run_endpoint(self) -> None:
        run_res = self.client.post("/api/memory-executor/tasks/run", json={"envid": "prod"})
        self.assertEqual(run_res.status_code, 200)
        data = run_res.json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(int(data.get("called", 0)), 1)
        self.assertEqual(int(data.get("failed", 0)), 0)
        self.assertEqual(self.server.orchestrator.last_runtime.get("envid"), "prod")

    def test_run_selected_task_endpoint(self) -> None:
        item = self.fake_store.create_task(
            {
                "name": "nightly-summary",
                "enabled": True,
                "envid": "",
                "muid": "chat-1",
                "period_sec": 3600,
                "request_text": "Summarize events",
                "todo_title": None,
                "execution_policy": "idle",
            }
        )
        task_id = str(item.get("id") or "")
        res = self.client.post(f"/api/memory-executor/tasks/{task_id}/run", json={})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        self.assertTrue(bool(data.get("queued")))
        self.assertEqual(str(data.get("task_id") or ""), task_id)


if __name__ == "__main__":
    unittest.main()
