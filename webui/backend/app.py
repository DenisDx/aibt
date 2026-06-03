"""aibt WebUI backend.

Provides FastAPI application with:
  - Login/logout (file-based session cookies)
  - Dashboard status, logs, and service restart API
  - Real-time log streaming via WebSocket (/ws/logs)
  - Static file serving for the frontend (last route)

Imported and started by src/core/app.py as WebUIServer.
"""
from __future__ import annotations
import asyncio
import hashlib
import hmac
import inspect
import json
import os
import secrets
import signal
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Ensure src/ is importable when this module is loaded.
_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SRC_DIR = os.path.join(_ROOT_DIR, "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

import uvicorn
from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi import WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.logging_utils import log, register_log_listener, unregister_log_listener
from core.envid_runtime import build_effective_config
from memory.api import get_memory_service
from memoryd import get_memoryd_service


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _const_eq(a: str, b: str) -> bool:
    """Constant-time string comparison to prevent timing attacks."""
    return hmac.compare_digest(a.encode(), b.encode())


# ── Session store ─────────────────────────────────────────────────────────────

class SessionStore:
    """File-based sessions stored in {root}/sessions/<sha256(token)>.json."""

    def __init__(self, root_dir: str, ttl: int = 86400) -> None:
        self._dir = Path(root_dir) / "sessions"
        self._dir.mkdir(parents=True, exist_ok=True)
        self.ttl = max(60, ttl)

    def _path(self, token: str) -> Path:
        return self._dir / f"{hashlib.sha256(token.encode()).hexdigest()}.json"

    def create(self, login: str, permissions: list[str]) -> str:
        """Create session. Returns opaque session token."""
        token = secrets.token_hex(32)
        payload = {
            "login": login,
            "permissions": permissions,
            "expires_at": datetime.fromtimestamp(
                _utcnow().timestamp() + self.ttl, tz=timezone.utc
            ).isoformat(),
        }
        self._path(token).write_text(json.dumps(payload), encoding="utf-8")
        return token

    def get(self, token: str) -> Optional[dict[str, Any]]:
        """Return session data if token is valid and not expired, else None."""
        if not token:
            return None
        path = self._path(token)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            exp = datetime.fromisoformat(data["expires_at"])
            if exp <= _utcnow():
                path.unlink(missing_ok=True)
                return None
            return data
        except Exception:
            path.unlink(missing_ok=True)
            return None

    def delete(self, token: str) -> None:
        if token:
            self._path(token).unlink(missing_ok=True)


# ── Request models ────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    login: str
    password: str


class MemorySearchRequest(BaseModel):
    query: str
    corpora: list[str] | None = None
    filters: dict[str, Any] | None = None
    limit: int = 8
    agent: str | None = None


class MemoryIngestRequest(BaseModel):
    source: dict[str, Any]
    corpus_id: str
    title: str | None = None
    tags: list[str] | None = None


class MemoryRunIngestRequest(BaseModel):
    limit: int | None = None


class MemorydEnqueueRequest(BaseModel):
    source_context: dict[str, Any]
    final_response: str
    muid: str | None = None
    caller_tag: str | None = None
    request_text: str | None = None
    provider: str | None = None
    model: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    repetition_penalty: float | None = None
    repeat_last_n: int | None = None
    max_tokens: int | None = None
    num_predict: int | None = None
    seed: int | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    top_k: int | None = None
    min_p: float | None = None
    tools: list[Any] | None = None
    context_types: list[str] | None = None
    types: list[str] | None = None


class MemorydRunRequest(BaseModel):
    limit: int | None = None


class MemorydUpsertRecordRequest(BaseModel):
    payload: dict[str, Any]


class MemoryExecutorTaskRequest(BaseModel):
    task: dict[str, Any]


class MemoryExecutorRunRequest(BaseModel):
    envid: str | None = None


# ── WebUI server ──────────────────────────────────────────────────────────────

class WebUIServer:
    """FastAPI-based WebUI server.

    Instantiated by src/core/app.py; call start() to begin serving.
    started_event is set once uvicorn is ready to accept connections.
    """

    def __init__(self, root_dir: str, config: dict) -> None:
        self.root_dir = root_dir
        self.config = config
        wui = config.get("webui", {})
        self.host = str(wui.get("bind", "0.0.0.0"))
        self.port = int(wui.get("port", 50080))
        auth = wui.get("auth", {})
        self._users: list[dict] = list(auth.get("users", []))
        self._session_ttl = int(auth.get("session_ttl", 86400))
        self.sessions = SessionStore(root_dir, self._session_ttl)
        self.started_event = asyncio.Event()
        self._restart_loop_time: float = 0.0
        self._server: Optional[uvicorn.Server] = None
        self._adapter_tasks: dict[str, asyncio.Task] = {}
        self.app = self._build_app()

        # Multi-agent orchestrator
        from orchestrator.orchestrator import AgentOrchestrator
        self.orchestrator = AgentOrchestrator(self.config)
        self.adapters = dict(self.orchestrator.adapters)
        self.webui_adapter = self.adapters.get("webui")
        if self.webui_adapter is None:
            raise RuntimeError("webui adapter is not discovered; ensure src/adapters/webui/app.py is valid")
        self.memory_service = get_memory_service(self.root_dir, self.config)
        self.memoryd_service = get_memoryd_service(self.root_dir, self.config)

    def _agent_allowed_corpora(self, agent_id: str | None) -> list[str] | None:
        """Return configured corpus allowlist for agent.

        Input: agent id.
        Output: list of allowed corpora or None when unrestricted.
        """

        if not agent_id:
            return None
        items = self.config.get("agents", {}).get("items", {})
        if not isinstance(items, dict):
            return None
        a_cfg = items.get(agent_id, {})
        if not isinstance(a_cfg, dict):
            return None
        rag_cfg = a_cfg.get("rag", {})
        if not isinstance(rag_cfg, dict):
            return None
        corpora = rag_cfg.get("corpora")
        if not isinstance(corpora, list):
            return None
        cleaned = [str(x).strip() for x in corpora if str(x).strip()]
        return cleaned or []

    @staticmethod
    def _apply_corpus_acl(requested: list[str] | None, allowed: list[str] | None) -> list[str] | None:
        """Intersect requested corpora with ACL.

        Input: requested corpora and allowed corpora.
        Output: effective corpora filter or None.
        """

        if allowed is None:
            return requested
        if not requested:
            return allowed
        req = [str(x).strip() for x in requested if str(x).strip()]
        if not req:
            return allowed
        allowed_set = set(allowed)
        return [x for x in req if x in allowed_set]

    def _langgraph_cfg(self) -> dict[str, Any]:
        """Return LangGraph runtime config from webui section with defaults."""
        cfg = self.config.get("webui", {}).get("langgraph", {})
        return {
            "host": str(cfg.get("host", "0.0.0.0")),
            "port": int(cfg.get("port", 2024)),
            "config": str(cfg.get("config", "langgraph.json")),
        }

    def _langgraph_log_file(self) -> str:
        return os.path.join(self.root_dir, "logs", "langgraph-dev.log")

    def _langgraph_script(self) -> str:
        return os.path.join(self.root_dir, "restart_langgraph.sh")

    def _langgraph_status(self, req_host: str | None = None) -> dict[str, Any]:
        """Collect LangGraph status using local socket + process checks."""
        cfg = self._langgraph_cfg()
        port = cfg["port"]
        running = False
        pid = ""

        try:
            r = subprocess.run(["ss", "-ltn"], capture_output=True, text=True, timeout=3)
            running = f":{port} " in r.stdout
        except Exception:
            running = False

        try:
            grep = f"langgraph dev --config {cfg['config']}"
            r = subprocess.run(["pgrep", "-af", grep], capture_output=True, text=True, timeout=3)
            if r.returncode == 0 and r.stdout.strip():
                line = r.stdout.strip().splitlines()[0]
                pid = line.split(" ", 1)[0]
        except Exception:
            pid = ""

        host_for_url = req_host or "127.0.0.1"
        base_url = f"http://{host_for_url}:{port}"
        return {
            "running": running,
            "pid": pid,
            "host": cfg["host"],
            "port": port,
            "base_url": base_url,
            "docs_url": f"{base_url}/docs",
            "studio_url": f"https://smith.langchain.com/studio/?baseUrl={base_url}",
            "log_file": self._langgraph_log_file(),
        }

    # ── Auth helpers ──────────────────────────────────────────────────────────

    def _auth_user(self, login: str, password: str) -> Optional[dict]:
        """Validate credentials using constant-time comparison. Returns user dict or None."""
        for user in self._users:
            if _const_eq(str(user.get("login", "")), login) and \
               _const_eq(str(user.get("password", "")), password):
                return user
        return None

    def _require_session(self, aibt_session: Optional[str] = Cookie(default=None)) -> dict:
        """FastAPI dependency: require a valid session cookie."""
        session = self.sessions.get(aibt_session or "")
        if not session:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
        return session

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _service_state(self) -> str:
        """Return 'restarting' briefly after a restart request, else 'online'."""
        if self._restart_loop_time > 0:
            try:
                elapsed = asyncio.get_running_loop().time() - self._restart_loop_time
                if elapsed < 120.0:
                    return "restarting"
            except RuntimeError:
                pass
        return "online"

    def _memoryd_service_for_envid(self, envid: str | None = None):
        """Return memoryd service bound to an effective config for one envid."""

        effective = build_effective_config(self.config, envid)
        return get_memoryd_service(self.root_dir, effective)

    @staticmethod
    def _memoryd_enabled_types(effective_config: dict[str, Any]) -> list[str]:
        """Return enabled memoryd item types from effective config."""

        memoryd_cfg = effective_config.get("memoryd", {}) if isinstance(effective_config, dict) else {}
        items_cfg = memoryd_cfg.get("items", {}) if isinstance(memoryd_cfg, dict) else {}
        out: list[str] = []
        if not isinstance(items_cfg, dict):
            return out
        for type_name, type_cfg in items_cfg.items():
            if isinstance(type_cfg, dict) and bool(type_cfg.get("enabled", False)):
                clean = str(type_name).strip().lower()
                if clean:
                    out.append(clean)
        return sorted(set(out))

    @staticmethod
    def _memoryd_auto_writable_types(effective_config: dict[str, Any]) -> list[str]:
        """Return enabled memoryd types allowed for auto enqueue writes."""

        memoryd_cfg = effective_config.get("memoryd", {}) if isinstance(effective_config, dict) else {}
        items_cfg = memoryd_cfg.get("items", {}) if isinstance(memoryd_cfg, dict) else {}
        out: list[str] = []
        if not isinstance(items_cfg, dict):
            return out
        for type_name, type_cfg in items_cfg.items():
            if not isinstance(type_cfg, dict):
                continue
            if not bool(type_cfg.get("enabled", False)):
                continue
            if bool(type_cfg.get("manual_only", False)) or bool(type_cfg.get("external_writer", False)):
                continue
            clean = str(type_name).strip().lower()
            if clean:
                out.append(clean)
        return sorted(set(out))

    @staticmethod
    def _resolve_agent_memoryd_types(
        effective_config: dict[str, Any],
        *,
        agent_id: str,
        key: str,
        default_types: list[str],
        allowed_types: list[str],
    ) -> list[str]:
        """Resolve one agent memoryd type list from effective config with filtering."""

        agents_cfg = effective_config.get("agents", {}) if isinstance(effective_config, dict) else {}
        items_cfg = agents_cfg.get("items", {}) if isinstance(agents_cfg, dict) else {}
        agent_cfg = items_cfg.get(agent_id, {}) if isinstance(items_cfg, dict) else {}
        memoryd_cfg = agent_cfg.get("memoryd", {}) if isinstance(agent_cfg, dict) else {}
        raw = memoryd_cfg.get(key) if isinstance(memoryd_cfg, dict) else None
        if raw is None:
            requested = list(default_types)
        elif isinstance(raw, list):
            requested = [str(item).strip().lower() for item in raw if str(item).strip()]
        else:
            requested = list(default_types)
        allowed = {str(x).strip().lower() for x in allowed_types if str(x).strip()}
        return sorted(item for item in set(requested) if item in allowed)

    def _memory_executor_store_for_envid(self, envid: str | None = None):
        """Return memory_executor store bound to effective config for one envid."""

        if not self._memory_executor_available():
            raise RuntimeError("memory_executor agent is not available")

        from agents.memory_executor.agent import MemoryExecutorStore

        effective = build_effective_config(self.config, envid)
        store = MemoryExecutorStore(self.root_dir, effective)
        store.ensure_schema()
        return store

    def _memory_executor_available(self) -> bool:
        """Return whether memory_executor code is discovered in this runtime."""

        agent_classes = getattr(self.orchestrator, "agent_classes", None)
        if agent_classes is None:
            return True
        return "memory_executor" in agent_classes

    def _memory_executor_run_task_now(self, task_id: str) -> dict[str, Any]:
        """Enqueue one Memory Executor task immediately."""

        from agents.memory_executor.agent import MemoryExecutorAgent

        agent = MemoryExecutorAgent(self.config, {"name": "memory_executor"})
        return agent.run_task_now(task_id)

    def _inflight_task_stats(self) -> dict[str, int]:
        """Count not-finished orchestrator tasks.

        pending: accepted but not yet running.
        running: currently executing agent/fallback node.
        retrying: failed attempt with active retry flow.
        total: pending + running + retrying.
        """

        pending = 0
        running = 0
        retrying = 0
        for task in self.orchestrator.tasks.values():
            status = str(task.get("status", "")).strip().lower()
            if status == "pending":
                pending += 1
            elif status == "running":
                running += 1
            elif status == "retrying":
                retrying += 1
        return {
            "pending": pending,
            "running": running,
            "retrying": retrying,
            "total": pending + running + retrying,
        }

    def _read_log(self, log_type: str, lines: int = 200) -> dict:
        """Read last N lines from logs/<log_type>.log.

        Returns dict with keys: ok, text, lines, log_type.
        """
        limit = max(10, min(5000, lines))
        log_path = Path(self.root_dir) / "logs" / f"{log_type}.log"
        if not log_path.exists():
            return {"ok": True, "text": "", "lines": 0, "log_type": log_type}
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
            all_lines = text.splitlines()
            selected = all_lines[-limit:]
            return {"ok": True, "text": "\n".join(selected), "lines": len(selected), "log_type": log_type}
        except Exception as e:
            return {"ok": False, "error": str(e), "log_type": log_type}

    def _list_log_types(self) -> list[str]:
        """Return sorted list of log types present in logs/."""
        logs_dir = Path(self.root_dir) / "logs"
        if not logs_dir.is_dir():
            return ["all"]
        types = sorted(f.stem for f in logs_dir.glob("*.log") if not f.name.startswith("."))
        return types or ["all"]

    def _list_agent_llm_logs(self) -> list[str]:
        """Return sorted list of agent LLM JSONL log files in logs/."""

        logs_dir = Path(self.root_dir) / "logs"
        if not logs_dir.is_dir():
            return []
        files = sorted(f.name for f in logs_dir.glob("*_llm.jsonl") if f.is_file() and not f.name.startswith("."))
        return files

    def _resolve_agent_llm_log_path(self, name: str) -> Path:
        """Resolve safe file path for one *_llm.jsonl log name."""

        clean = str(name or "").strip()
        if not clean.endswith("_llm.jsonl"):
            raise ValueError("invalid agent log file name")
        if "/" in clean or "\\" in clean or clean.startswith("."):
            raise ValueError("invalid agent log file name")
        path = Path(self.root_dir) / "logs" / clean
        if not path.exists() or not path.is_file():
            raise ValueError("agent log file not found")
        return path

    @staticmethod
    def _extract_text_field(value: Any) -> str:
        """Extract human-readable text from plain/JSON-like payloads."""

        if value is None:
            return ""

        if isinstance(value, str):
            src = value.strip()
            if not src:
                return ""
            try:
                parsed = json.loads(src)
            except Exception:
                return src
            return WebUIServer._extract_text_field(parsed) or src

        if isinstance(value, dict):
            for key in ("text", "message", "content"):
                if key not in value:
                    continue
                part = WebUIServer._extract_text_field(value.get(key))
                if part:
                    return part

            # OpenAI-style response payloads.
            choices = value.get("choices")
            if isinstance(choices, list) and choices:
                first = choices[0] if isinstance(choices[0], dict) else {}
                msg = first.get("message") if isinstance(first, dict) else None
                delta = first.get("delta") if isinstance(first, dict) else None
                part = WebUIServer._extract_text_field(msg)
                if part:
                    return part
                part = WebUIServer._extract_text_field(delta)
                if part:
                    return part

            err = value.get("error")
            if isinstance(err, dict):
                part = WebUIServer._extract_text_field(err.get("message"))
                if part:
                    return part
            return ""

        if isinstance(value, list):
            out: list[str] = []
            for item in value:
                part = WebUIServer._extract_text_field(item)
                if part:
                    out.append(part)
            return " ".join(out).strip()

        return str(value).strip()

    @staticmethod
    def _extract_last_user_preview(payload: dict[str, Any], request_messages: Any = None) -> str:
        """Extract last user message preview from exact request or payload fallback."""

        if isinstance(request_messages, list):
            # on_chat_model_start for chat models provides a batch of message lists
            batches = request_messages
            if batches and isinstance(batches[0], list):
                messages = batches[0]
            else:
                messages = batches
            for item in reversed(messages):
                if not isinstance(item, dict):
                    continue
                m_type = str(item.get("type", "") or item.get("role", "")).strip().lower()
                if m_type in ("human", "user"):
                    preview = WebUIServer._extract_text_field(item.get("content", ""))
                    if preview:
                        return preview[:240]

        if not isinstance(payload, dict):
            return ""
        messages = payload.get("messages")
        if isinstance(messages, list):
            for item in reversed(messages):
                if not isinstance(item, dict):
                    continue
                m_type = str(item.get("type", "") or "").strip().lower()
                if m_type in ("human", "user"):
                    preview = WebUIServer._extract_text_field(item.get("content", ""))
                    if preview:
                        return preview[:240]
            if messages:
                tail = messages[-1]
                if isinstance(tail, dict):
                    preview = WebUIServer._extract_text_field(tail.get("content", ""))
                    if preview:
                        return preview[:240]
        return WebUIServer._extract_text_field(payload.get("query", ""))[:240]

    @staticmethod
    def _extract_response_preview(response: Any, response_raw: Any) -> str:
        """Extract assistant response preview with JSON unpacking."""

        text = WebUIServer._extract_text_field(response)
        if text:
            return text[:240]
        text = WebUIServer._extract_text_field(response_raw)
        if text:
            return text[:240]
        if response is None:
            return ""
        if isinstance(response, str):
            return response[:240]
        return json.dumps(response, ensure_ascii=False, default=str)[:240]

    @staticmethod
    def _build_agent_log_view(path: Path, limit: int) -> dict[str, Any]:
        """Build parsed exchange list from one *_llm.jsonl file."""

        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        pending_input: list[dict[str, Any]] = []
        pending_input_by_exchange: dict[str, dict[str, Any]] = {}
        exchanges: list[dict[str, Any]] = []

        def _is_request_row(obj: dict[str, Any]) -> bool:
            if not isinstance(obj, dict):
                return False
            if "phase" in obj:
                return str(obj.get("phase", "")).strip().lower() == "input"
            return "messages" in obj and ("model" in obj or "stream" in obj)

        def _is_response_row(obj: dict[str, Any]) -> bool:
            if not isinstance(obj, dict):
                return False
            if "phase" in obj:
                return str(obj.get("phase", "")).strip().lower() == "output"
            return "choices" in obj or "error" in obj or "id" in obj

        for idx, line in enumerate(lines, start=1):
            text = str(line or "").strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except Exception:
                continue
            if _is_request_row(row):
                wrapped = {"line": idx, "raw": text, "data": row}
                exchange_id = str(row.get("exchange_id") or "").strip()
                if exchange_id:
                    pending_input_by_exchange[exchange_id] = wrapped
                else:
                    pending_input.append(wrapped)
                continue
            if not _is_response_row(row):
                continue

            exchange_id = str(row.get("exchange_id") or "").strip()
            input_row = pending_input_by_exchange.pop(exchange_id, None) if exchange_id else None
            if input_row is None:
                input_row = pending_input.pop(0) if pending_input else None
            payload = input_row.get("data", {}).get("payload", {}) if input_row else {}
            request_messages = input_row.get("data", {}).get("request_messages") if input_row else None
            if request_messages is None and input_row:
                request_messages = input_row.get("data", {}).get("messages")
            request_messages_raw = input_row.get("raw") if input_row else None
            request_prompts = input_row.get("data", {}).get("request_prompts") if input_row else None
            if request_prompts is None and input_row:
                request_prompts = input_row.get("data", {}).get("prompt")
            request_prompts_raw = input_row.get("raw") if input_row else None
            invocation_params = input_row.get("data", {}).get("invocation_params") if input_row else None
            invocation_params_raw = input_row.get("data", {}).get("invocation_params_raw") if input_row else None
            response = row.get("response") if isinstance(row, dict) else None
            if response is None:
                response = row
            response_raw = row.get("response_raw") if isinstance(row, dict) else None
            if response_raw is None:
                response_raw = text
            exchanges.append(
                {
                    "entry_id": f"{path.name}:{idx}",
                    "time": str(row.get("ts") or (input_row or {}).get("data", {}).get("ts") or ""),
                    "agent_id": str(row.get("agent_id") or (input_row or {}).get("data", {}).get("agent_id") or ""),
                    "envid": row.get("envid") if isinstance(row, dict) and row.get("envid") is not None else (input_row or {}).get("data", {}).get("envid"),
                    "user_preview": WebUIServer._extract_last_user_preview(payload, request_messages=request_messages),
                    "response_preview": WebUIServer._extract_response_preview(response, response_raw),
                    "query": str(payload.get("query", "") or ""),
                    "request_messages": payload.get("messages") if isinstance(payload, dict) else [],
                    "request_messages_exact": request_messages,
                    "request_messages_raw": request_messages_raw,
                    "request_prompts_exact": request_prompts,
                    "request_prompts_raw": request_prompts_raw,
                    "invocation_params": invocation_params,
                    "invocation_params_raw": invocation_params_raw,
                    "memory_context": str(payload.get("memory_context", "") or "") if isinstance(payload, dict) else "",
                    "context": payload.get("context") if isinstance(payload, dict) else {},
                    "response": response,
                    "response_raw": response_raw,
                    "raw_input_line": input_row.get("raw") if input_row else "",
                    "raw_output_line": text,
                    "input_line_no": input_row.get("line") if input_row else None,
                    "output_line_no": idx,
                }
            )

        safe_limit = max(1, min(500, int(limit)))
        return {
            "items": list(reversed(exchanges[-safe_limit:])),
            "total": len(exchanges),
            "source_lines": len(lines),
        }

    async def _do_restart(self) -> None:
        """Send restart command (systemd or SIGTERM) after a short delay."""
        await asyncio.sleep(0.5)
        instance = self.config.get("instance", "aibt")
        svc = str(instance)
        sd = [] if os.geteuid() == 0 else ["--user"]
        try:
            r = subprocess.run(
                ["systemctl"] + sd + ["restart", svc],
                capture_output=True, text=True, timeout=10
            )
            if r.returncode == 0:
                return
        except Exception:
            pass
        os.kill(os.getpid(), signal.SIGTERM)

    # ── FastAPI app ───────────────────────────────────────────────────────────

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="aibt WebUI", docs_url=None, redoc_url=None, openapi_url=None)
        require = self._require_session  # bound method used as Depends target

        # ── Auth ──────────────────────────────────────────────────────────────

        @app.post("/api/auth/login")
        async def api_login(body: LoginRequest, response: Response, request: Request):
            user = self._auth_user(body.login, body.password)
            if not user:
                log("webui", "warning", f"Login failed for '{body.login}' from {request.client.host if request.client else '?'}")
                raise HTTPException(status_code=401, detail="Invalid credentials")
            token = self.sessions.create(
                login=str(user.get("login", "")),
                permissions=list(user.get("permissions", [])),
            )
            response.set_cookie(
                "aibt_session", token,
                max_age=self._session_ttl, httponly=True, samesite="strict",
                secure=(request.url.scheme == "https"),
            )
            log("webui", "info", f"User '{body.login}' logged in")
            return {"ok": True}

        @app.get("/api/auth/me")
        async def api_me(session: dict = Depends(require)):
            return {"ok": True, "login": session.get("login", ""), "permissions": session.get("permissions", [])}

        @app.post("/api/auth/logout")
        async def api_logout(response: Response, aibt_session: Optional[str] = Cookie(default=None)):
            self.sessions.delete(aibt_session or "")
            response.delete_cookie("aibt_session")
            return {"ok": True}

        # ── Dashboard ─────────────────────────────────────────────────────────

        @app.get("/api/status")
        async def api_status(session: dict = Depends(require)):
            mem_enabled = bool(self.config.get("memory", {}).get("enabled", True))
            mem_corpora = 0
            mem_docs = 0
            if mem_enabled:
                try:
                    corpora = self.memory_service.list_corpora()
                    mem_corpora = len(corpora)
                    mem_docs = sum(int(c.get("documents", 0) or 0) for c in corpora)
                except Exception as e:
                    log("webui", "warning", f"Memory status unavailable: {e}")
            processing = self._inflight_task_stats()
            return {
                "service_state": self._service_state(),
                "instance": self.config.get("instance", "aibt"),
                "title": self.config.get("title", "aibt"),
                "session": {"login": session.get("login", "")},
                "memory": {
                    "enabled": mem_enabled,
                    "corpora": mem_corpora,
                    "documents": mem_docs,
                },
                "processing": processing,
                "capabilities": {
                    "memory_executor": self._memory_executor_available(),
                },
            }

        @app.get("/api/logs")
        async def api_logs(log_type: str = "all", lines: int = 200, session: dict = Depends(require)):
            return self._read_log(log_type, lines)

        @app.get("/api/logs/types")
        async def api_log_types(session: dict = Depends(require)):
            return {"types": self._list_log_types()}

        @app.get("/api/agent-logs/files")
        async def api_agent_log_files(session: dict = Depends(require)):
            return {"ok": True, "items": self._list_agent_llm_logs()}

        @app.get("/api/agent-logs/view")
        async def api_agent_log_view(file: str, limit: int = 120, session: dict = Depends(require)):
            try:
                path = self._resolve_agent_llm_log_path(file)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            try:
                view = self._build_agent_log_view(path, limit=limit)
                return {"ok": True, "file": path.name, **view}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.post("/api/service/restart")
        async def api_restart(session: dict = Depends(require)):
            log("webui", "notice", f"Restart requested by '{session.get('login', '?')}'")
            try:
                self._restart_loop_time = asyncio.get_running_loop().time()
            except RuntimeError:
                pass
            asyncio.create_task(self._do_restart())
            return {"ok": True, "message": "Restart scheduled"}

        # ── LangGraph runtime API ────────────────────────────────────────────

        @app.get("/api/langgraph/status")
        async def api_langgraph_status(request: Request, session: dict = Depends(require)):
            host = request.headers.get("x-forwarded-host") or request.headers.get("host", "").split(":")[0]
            return {"ok": True, **self._langgraph_status(host)}

        @app.post("/api/langgraph/restart")
        async def api_langgraph_restart(request: Request, session: dict = Depends(require)):
            script = self._langgraph_script()
            if not os.path.exists(script):
                raise HTTPException(status_code=500, detail="restart_langgraph.sh is missing")

            env = os.environ.copy()
            cfg = self._langgraph_cfg()
            env["LG_HOST"] = str(cfg["host"])
            env["LG_PORT"] = str(cfg["port"])
            env["LG_CONFIG"] = str(cfg["config"])

            try:
                r = subprocess.run([script, "restart"], cwd=self.root_dir, capture_output=True, text=True, timeout=25, env=env)
                if r.returncode != 0:
                    raise HTTPException(status_code=500, detail=r.stderr.strip() or "restart failed")
                log("webui", "notice", f"LangGraph restart requested by '{session.get('login', '?')}'")
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

            host = request.headers.get("x-forwarded-host") or request.headers.get("host", "").split(":")[0]
            return {"ok": True, "message": "LangGraph restarted", **self._langgraph_status(host)}

        @app.get("/api/langgraph/logs")
        async def api_langgraph_logs(lines: int = 120, session: dict = Depends(require)):
            limit = max(20, min(1000, lines))
            path = self._langgraph_log_file()
            if not os.path.exists(path):
                return {"ok": True, "text": "", "lines": 0}
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    data = f.read().splitlines()
                tail = data[-limit:]
                return {"ok": True, "text": "\n".join(tail), "lines": len(tail)}
            except Exception as e:
                return {"ok": False, "error": str(e)}

        # ── WebSocket log streaming ────────────────────────────────────────────

        @app.websocket("/ws/logs")
        async def ws_logs(websocket: WebSocket, log_type: str = "all"):
            await websocket.accept()

            # Authenticate via session cookie
            token = websocket.cookies.get("aibt_session", "")
            if not self.sessions.get(token):
                await websocket.send_json({"type": "error", "message": "not authenticated"})
                await websocket.close(code=4001)
                return

            # Send initial log content
            init_data = self._read_log(log_type, 200)
            initial = init_data.get("text", "").splitlines() if init_data.get("ok") else []
            await websocket.send_json({"type": "init", "lines": initial, "log_type": log_type})

            # Stream new log lines via pub-sub queue
            q: asyncio.Queue = asyncio.Queue(maxsize=2000)
            register_log_listener(q)
            try:
                while True:
                    try:
                        ltype, text = await asyncio.wait_for(q.get(), timeout=30.0)
                        if log_type == "all" or ltype == log_type:
                            await websocket.send_json({"type": "line", "text": text})
                    except asyncio.TimeoutError:
                        await websocket.send_json({"type": "ping"})
            except WebSocketDisconnect:
                pass
            except Exception:
                pass
            finally:
                unregister_log_listener(q)

        # ── Multi-agent API ───────────────────────────────────────────────────

        @app.post("/api/agents/query")
        async def api_agents_query(body: dict[str, Any], session: dict = Depends(require)):
            """Submit a query to an agent via WebUIAdapter. Returns task id."""
            agent = str(body.get("agent", "")).strip()
            query = str(
                body.get("query")
                or body.get("message")
                or body.get("prompt")
                or body.get("text")
                or ""
            ).strip()
            user_id = session.get("login", "webui")
            if not agent:
                raise HTTPException(status_code=400, detail="agent is required")
            if not query:
                raise HTTPException(status_code=400, detail="query is required")
            try:
                result = await self.webui_adapter.handle(
                    user_id,
                    agent,
                    query,
                    context={"session": session},
                )
            except Exception as e:
                log(
                    "webui",
                    "error",
                    f"Agent query submit failed for agent={agent} user={user_id}: {e}\n{traceback.format_exc()}",
                )
                raise HTTPException(status_code=500, detail=str(e))
            log(
                "webui",
                "info",
                f"Submitted agent task {result.get('task_id', '')} for agent={agent} user={user_id}",
            )
            return {"ok": True, **result}

        @app.get("/api/agents/status")
        async def api_agents_status(task_id: str, session: dict = Depends(require)):
            """Get status/result for a task."""
            task = self.orchestrator.get_task(task_id)
            if not task:
                return {"ok": False, "error": "not found"}
            return {"ok": True, **task}

        @app.get("/api/agents/list")
        async def api_agents_list(session: dict = Depends(require)):
            """List available agents."""
            return {"ok": True, "agents": self.orchestrator.list_agents()}

        @app.get("/api/agents/info")
        async def api_agents_info(agent: str, limit: int = 20, session: dict = Depends(require)):
            """Return metadata and recent tasks for selected agent."""
            info = self.orchestrator.get_agent_info(agent, limit)
            if not info:
                return {"ok": False, "error": "agent not found"}
            return {"ok": True, **info}

        # ── Memory / RAG API ───────────────────────────────────────────────

        @app.get("/api/memory/status")
        async def api_memory_status(session: dict = Depends(require)):
            enabled = bool(self.config.get("memory", {}).get("enabled", True))
            if not enabled:
                return {"ok": True, "enabled": False, "corpora": 0, "documents": 0}
            try:
                corpora = self.memory_service.list_corpora()
                docs = sum(int(c.get("documents", 0) or 0) for c in corpora)
                return {"ok": True, "enabled": True, "corpora": len(corpora), "documents": docs}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.get("/api/memory/corpora")
        async def api_memory_corpora(agent: str = "", session: dict = Depends(require)):
            try:
                rows = self.memory_service.list_corpora()
                allowed = self._agent_allowed_corpora(agent.strip() or None)
                if allowed is not None:
                    allowed_set = set(allowed)
                    rows = [r for r in rows if str(r.get("corpus_id", "")) in allowed_set]
                return {"ok": True, "items": rows}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.get("/api/memory/documents")
        async def api_memory_documents(
            corpus_id: str,
            limit: int = 50,
            offset: int = 0,
            q: str = "",
            tag: str = "",
            sort_by: str = "updated_at",
            sort_dir: str = "desc",
            agent: str = "",
            session: dict = Depends(require),
        ):
            c_id = corpus_id.strip()
            if not c_id:
                raise HTTPException(status_code=400, detail="corpus_id is required")
            allowed = self._agent_allowed_corpora(agent.strip() or None)
            if allowed is not None and c_id not in set(allowed):
                raise HTTPException(status_code=403, detail="corpus is not allowed for this agent")
            try:
                page = self.memory_service.list_documents(
                    corpus_id=c_id,
                    limit=max(1, min(200, int(limit))),
                    offset=max(0, int(offset)),
                    query=q,
                    tag=tag,
                    sort_by=sort_by,
                    sort_dir=sort_dir,
                )
                return {"ok": True, **page}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.post("/api/memory/search")
        async def api_memory_search(body: MemorySearchRequest, session: dict = Depends(require)):
            query = body.query.strip()
            if not query:
                raise HTTPException(status_code=400, detail="query is required")

            allowed = self._agent_allowed_corpora((body.agent or "").strip() or None)
            effective_corpora = self._apply_corpus_acl(body.corpora, allowed)
            try:
                hits = self.memory_service.search_docs(
                    query=query,
                    corpora=effective_corpora,
                    filters=body.filters,
                    limit=max(1, min(50, int(body.limit))),
                )
                return {"ok": True, "items": hits, "corpora": effective_corpora}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.post("/api/memory/ingest")
        async def api_memory_ingest(body: MemoryIngestRequest, session: dict = Depends(require)):
            c_id = body.corpus_id.strip()
            if not c_id:
                raise HTTPException(status_code=400, detail="corpus_id is required")
            try:
                result = self.memory_service.ingest_document(
                    source=body.source,
                    corpus_id=c_id,
                    title=body.title,
                    tags=body.tags,
                    requested_by=session.get("login", "webui"),
                )
                log(
                    "webui",
                    "info",
                    f"Memory ingest queued by {session.get('login', '?')} corpus={c_id} job={result.get('job_id', '')}",
                )
                return {"ok": True, **result}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.post("/api/memory/ingest/run")
        async def api_memory_ingest_run(body: MemoryRunIngestRequest, session: dict = Depends(require)):
            try:
                requested_limit = body.limit if body.limit is not None else int(
                    self.config.get("memory", {}).get("rag", {}).get("ingest", {}).get("max_jobs_per_tick", 3)
                )
                limit = max(1, min(100, int(requested_limit)))
                result = self.memory_service.run_ingest_batch(limit=limit)
                log(
                    "webui",
                    "notice",
                    (
                        f"Memory ingest batch triggered by {session.get('login', '?')} "
                        f"limit={limit} processed={result.get('processed', 0)} failed={result.get('failed', 0)}"
                    ),
                )
                return {"ok": True, "limit": limit, **result}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.get("/api/memory/document/{doc_id}")
        async def api_memory_document(doc_id: str, mode: str = "source", version: int | None = None, session: dict = Depends(require)):
            clean_id = doc_id.strip()
            if not clean_id:
                raise HTTPException(status_code=400, detail="doc_id is required")
            view = mode.strip().lower()
            if view not in ("source", "text", "summary"):
                raise HTTPException(status_code=400, detail="mode must be source|text|summary")
            try:
                data = self.memory_service.get_document(clean_id, version=version, mode=view)
                return {"ok": True, "item": data}
            except ValueError as e:
                raise HTTPException(status_code=404, detail=str(e))
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.get("/api/memory/agent/{agent_id}/namespace/{namespace}")
        async def api_memory_agent_namespace(
            agent_id: str,
            namespace: str,
            limit: int = 100,
            profile_id: str = "",
            envid: str = "",
            session: dict = Depends(require),
        ):
            clean_agent = agent_id.strip()
            clean_ns = namespace.strip()
            if not clean_agent:
                raise HTTPException(status_code=400, detail="agent_id is required")
            if not clean_ns:
                raise HTTPException(status_code=400, detail="namespace is required")
            try:
                items = self.memory_service.get_agent_namespace_items(
                    agent_id=clean_agent,
                    namespace=clean_ns,
                    limit=max(1, min(200, int(limit))),
                    profile_id=profile_id.strip() or None,
                    envid=envid.strip() or None,
                )
                return {"ok": True, "items": items}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.delete("/api/memory/document/{doc_id}")
        async def api_memory_document_delete(doc_id: str, session: dict = Depends(require)):
            clean_id = doc_id.strip()
            if not clean_id:
                raise HTTPException(status_code=400, detail="doc_id is required")
            try:
                deleted = self.memory_service.delete_document(clean_id)
                if deleted:
                    log("webui", "warning", f"Memory document deleted by {session.get('login', '?')}: {clean_id}")
                return {"ok": True, "deleted": bool(deleted)}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        # ── Memoryd API ─────────────────────────────────────────────────────

        @app.get("/api/memoryd/envids")
        async def api_memoryd_envids(session: dict = Depends(require)):
            try:
                svc = self._memoryd_service_for_envid(None)
                rows = svc.list_envids()
                enriched: list[dict[str, Any]] = []
                for row in rows:
                    envid = str(row.get("envid") or "").strip()
                    effective = build_effective_config(self.config, envid or None)
                    enabled = self._memoryd_enabled_types(effective)
                    auto_writable = self._memoryd_auto_writable_types(effective)
                    context_types = self._resolve_agent_memoryd_types(
                        effective,
                        agent_id="memory_executor",
                        key="context_types",
                        default_types=enabled,
                        allowed_types=enabled,
                    )
                    update_types = self._resolve_agent_memoryd_types(
                        effective,
                        agent_id="memory_executor",
                        key="update_types",
                        default_types=auto_writable,
                        allowed_types=auto_writable,
                    )
                    enriched_row = dict(row)
                    enriched_row["memory_executor_context_types"] = context_types
                    enriched_row["memory_executor_update_types"] = update_types
                    enriched.append(enriched_row)
                return {"ok": True, "items": enriched}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.get("/api/memoryd/muids")
        async def api_memoryd_muids(envid: str = "", session: dict = Depends(require)):
            try:
                svc = self._memoryd_service_for_envid(envid.strip() or None)
                items = svc.list_muids(limit=500)
                effective = build_effective_config(self.config, envid.strip() or None)
                memoryd_cfg = effective.get("memoryd", {}) if isinstance(effective, dict) else {}
                return {
                    "ok": True,
                    "envid": envid.strip() or None,
                    "default_muid": str(memoryd_cfg.get("muid") or "default"),
                    "items": items,
                }
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.get("/api/memoryd/records")
        async def api_memoryd_records(
            muid: str,
            types: str = "",
            envid: str = "",
            limit: int = 100,
            offset: int = 0,
            session: dict = Depends(require),
        ):
            clean_muid = muid.strip()
            if not clean_muid:
                raise HTTPException(status_code=400, detail="muid is required")
            raw_types = [item.strip() for item in types.split(",") if item.strip()]
            try:
                svc = self._memoryd_service_for_envid(envid.strip() or None)
                page = svc.list_records(clean_muid, types=raw_types or None, offset=max(0, int(offset)), limit=max(1, min(200, int(limit))))
                return {"ok": True, **page}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.post("/api/memoryd/tasks/enqueue")
        async def api_memoryd_tasks_enqueue(body: MemorydEnqueueRequest, session: dict = Depends(require)):
            try:
                envid = str(body.source_context.get("envid") or "").strip() or None
                svc = self._memoryd_service_for_envid(envid)
                result = svc.enqueue_update(
                    source_context=body.source_context,
                    final_response=body.final_response,
                    muid=body.muid,
                    caller_tag=body.caller_tag,
                    request_text=body.request_text,
                    provider=body.provider,
                    model=body.model,
                    temperature=body.temperature,
                    top_p=body.top_p,
                    repetition_penalty=body.repetition_penalty,
                    repeat_last_n=body.repeat_last_n,
                    max_tokens=body.max_tokens,
                    num_predict=body.num_predict,
                    seed=body.seed,
                    presence_penalty=body.presence_penalty,
                    frequency_penalty=body.frequency_penalty,
                    top_k=body.top_k,
                    min_p=body.min_p,
                    tools=body.tools,
                    context_types=body.context_types,
                    types=body.types,
                )
                return {"ok": True, **result}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.post("/api/memoryd/tasks/run")
        async def api_memoryd_tasks_run(body: MemorydRunRequest | None = None, session: dict = Depends(require)):
            try:
                limit = int(body.limit) if body and body.limit is not None else None
                svc = self._memoryd_service_for_envid(None)
                result = svc.run_tick(limit=limit)
                return {"ok": True, **result}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.get("/api/memoryd/tasks")
        async def api_memoryd_tasks(
            envid: str = "",
            limit: int = 200,
            offset: int = 0,
            session: dict = Depends(require),
        ):
            try:
                svc = self._memoryd_service_for_envid(envid.strip() or None)
                page = svc.list_active_tasks(
                    envid=envid.strip() or None,
                    limit=max(1, min(500, int(limit))),
                    offset=max(0, int(offset)),
                )
                return {"ok": True, **page}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.post("/api/memoryd/records/upsert")
        async def api_memoryd_record_upsert(body: MemorydUpsertRecordRequest, session: dict = Depends(require)):
            try:
                svc = self._memoryd_service_for_envid(str(body.payload.get("envid") or "").strip() or None)
                result = svc.upsert_record(body.payload)
                return {"ok": True, "item": result}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.delete("/api/memoryd/records/{record_id}")
        async def api_memoryd_record_delete(record_id: str, session: dict = Depends(require)):
            clean_id = record_id.strip()
            if not clean_id:
                raise HTTPException(status_code=400, detail="record_id is required")
            try:
                svc = self._memoryd_service_for_envid(None)
                svc.store.delete_record_by_id(clean_id)
                return {"ok": True, "deleted": True}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.post("/api/memoryd/config/reload")
        async def api_memoryd_config_reload(session: dict = Depends(require)):
            """Re-read config.json5 from disk and refresh memoryd envid list."""
            try:
                from core.config import load_config as _load_config
                fresh = _load_config(self.root_dir)
                self.config = fresh
                svc = self._memoryd_service_for_envid(None)
                envids = svc.list_envids()
                return {"ok": True, "items": envids}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        # ── MemoryExecutor API ─────────────────────────────────────────────

        @app.get("/api/memory-executor/templates")
        async def api_memory_executor_templates(session: dict = Depends(require)):
            if not self._memory_executor_available():
                raise HTTPException(status_code=404, detail="memory_executor is not available")
            try:
                templates_dir = Path(self.root_dir) / "agent_files"
                if not templates_dir.is_dir():
                    return {"ok": True, "items": []}
                items: list[dict[str, Any]] = []
                for path in sorted(templates_dir.glob("*.md")):
                    try:
                        text = path.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        text = ""
                    items.append(
                        {
                            "name": path.name,
                            "size": path.stat().st_size,
                            "text": text,
                        }
                    )
                return {"ok": True, "items": items}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.get("/api/memory-executor/tasks")
        async def api_memory_executor_tasks(
            envid: str = "",
            limit: int = 200,
            offset: int = 0,
            session: dict = Depends(require),
        ):
            if not self._memory_executor_available():
                raise HTTPException(status_code=404, detail="memory_executor is not available")
            try:
                store = self._memory_executor_store_for_envid(envid.strip() or None)
                page = store.list_tasks(
                    envid=envid.strip() if envid.strip() else None,
                    limit=max(1, min(500, int(limit))),
                    offset=max(0, int(offset)),
                )
                return {"ok": True, **page}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.post("/api/memory-executor/tasks")
        async def api_memory_executor_create_task(body: MemoryExecutorTaskRequest, session: dict = Depends(require)):
            if not self._memory_executor_available():
                raise HTTPException(status_code=404, detail="memory_executor is not available")
            try:
                envid = str(body.task.get("envid") or "").strip() or None
                store = self._memory_executor_store_for_envid(envid)
                item = store.create_task(body.task)
                return {"ok": True, "item": item}
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.put("/api/memory-executor/tasks/{task_id}")
        async def api_memory_executor_update_task(task_id: str, body: MemoryExecutorTaskRequest, session: dict = Depends(require)):
            clean_id = task_id.strip()
            if not clean_id:
                raise HTTPException(status_code=400, detail="task_id is required")
            if not self._memory_executor_available():
                raise HTTPException(status_code=404, detail="memory_executor is not available")
            try:
                envid = str(body.task.get("envid") or "").strip() or None
                store = self._memory_executor_store_for_envid(envid)
                item = store.update_task(clean_id, body.task)
                if item is None:
                    raise HTTPException(status_code=404, detail="task not found")
                return {"ok": True, "item": item}
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.delete("/api/memory-executor/tasks/{task_id}")
        async def api_memory_executor_delete_task(task_id: str, session: dict = Depends(require)):
            clean_id = task_id.strip()
            if not clean_id:
                raise HTTPException(status_code=400, detail="task_id is required")
            if not self._memory_executor_available():
                raise HTTPException(status_code=404, detail="memory_executor is not available")
            try:
                store = self._memory_executor_store_for_envid(None)
                deleted = store.delete_task(clean_id)
                return {"ok": True, "deleted": bool(deleted)}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.post("/api/memory-executor/tasks/run")
        async def api_memory_executor_run_tasks(body: MemoryExecutorRunRequest | None = None, session: dict = Depends(require)):
            if not self._memory_executor_available():
                raise HTTPException(status_code=404, detail="memory_executor is not available")
            try:
                envid = str(body.envid).strip() if body and body.envid is not None else ""
                stats = await self.orchestrator.run_cron_tick_hooks(
                    runtime={"source": "webui.memory_executor.run", "root_dir": self.root_dir, "envid": envid or None}
                )
                return {"ok": True, **stats}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.post("/api/memory-executor/tasks/{task_id}/run")
        async def api_memory_executor_run_task_now(task_id: str, session: dict = Depends(require)):
            clean_id = task_id.strip()
            if not clean_id:
                raise HTTPException(status_code=400, detail="task_id is required")
            if not self._memory_executor_available():
                raise HTTPException(status_code=404, detail="memory_executor is not available")
            try:
                result = self._memory_executor_run_task_now(clean_id)
                if not bool(result.get("found", True)):
                    raise HTTPException(status_code=404, detail="task not found")
                return {"ok": True, **result}
            except HTTPException:
                raise
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        # WebSocket: stream agent task status/result
        @app.websocket("/ws/agents")
        async def ws_agents(websocket: WebSocket, task_id: str = ""):
            await websocket.accept()
            # Authenticate via session cookie
            token = websocket.cookies.get("aibt_session", "")
            if not self.sessions.get(token):
                await websocket.send_json({"type": "error", "message": "not authenticated"})
                await websocket.close(code=4001)
                return
            if not task_id:
                await websocket.send_json({"type": "error", "message": "task_id required"})
                await websocket.close(code=4002)
                return
            # Poll orchestrator for status
            prev_status = None
            while True:
                task = self.orchestrator.get_task(task_id)
                if not task:
                    await websocket.send_json({"type": "error", "message": "not found"})
                    break
                status = task.get("status")
                if status != prev_status:
                    await websocket.send_json({"type": "status", "status": status, "result": task.get("result")})
                    prev_status = status
                if status in ("done", "error"):
                    break
                await asyncio.sleep(0.5)
            await websocket.close()
        frontend_dir = os.path.join(_ROOT_DIR, "webui", "frontend")
        if os.path.isdir(frontend_dir):
            app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")

        return app

    # ── Server lifecycle ──────────────────────────────────────────────────────

    @staticmethod
    def _adapter_enabled(adapter: Any) -> bool:
        """Return whether adapter should be started in runtime lifecycle."""

        if hasattr(adapter, "enabled"):
            return bool(getattr(adapter, "enabled"))
        return True

    @staticmethod
    def _select_adapter_method(adapter: Any, names: tuple[str, ...]) -> tuple[str, Any] | None:
        """Select first available callable adapter method from ordered names."""

        for method_name in names:
            method = getattr(adapter, method_name, None)
            if callable(method):
                return method_name, method
        return None

    def _start_adapter_task(self, adapter_id: str, adapter: Any) -> None:
        """Start one adapter task using flexible lifecycle method names."""

        selected = self._select_adapter_method(adapter, ("start_polling", "start", "run"))
        if selected is None:
            log("webui", "debug", f"Adapter '{adapter_id}' has no runtime start method")
            return

        method_name, starter = selected
        try:
            if inspect.iscoroutinefunction(starter):
                task = asyncio.create_task(starter(), name=f"aibt-adapter-{adapter_id}")
            else:
                task = asyncio.create_task(asyncio.to_thread(starter), name=f"aibt-adapter-{adapter_id}")
        except Exception as e:
            log("webui", "error", f"Adapter '{adapter_id}' failed to start via {method_name}(): {e}\n{traceback.format_exc()}")
            return
        self._adapter_tasks[adapter_id] = task

    async def _stop_adapter(self, adapter_id: str, adapter: Any) -> None:
        """Stop one adapter using flexible lifecycle method names."""

        selected = self._select_adapter_method(adapter, ("stop", "shutdown", "close"))
        if selected is None:
            return

        method_name, stopper = selected
        try:
            if inspect.iscoroutinefunction(stopper):
                await stopper()
            else:
                await asyncio.to_thread(stopper)
        except Exception as e:
            log("webui", "error", f"Adapter '{adapter_id}' stop failed via {method_name}(): {e}\n{traceback.format_exc()}")

    async def start(self) -> None:
        """Start uvicorn. Blocks until the server exits."""
        try:
            init_stats = await self.orchestrator.run_init_hooks(
                runtime={"source": "webui.start", "root_dir": self.root_dir}
            )
            if init_stats.get("called") or init_stats.get("failed"):
                log(
                    "webui",
                    "info",
                    f"Agent on_init hooks: called={init_stats.get('called', 0)} failed={init_stats.get('failed', 0)}",
                )
        except Exception as e:
            log("webui", "error", f"Failed to run agent on_init hooks: {e}\n{traceback.format_exc()}")

        cfg = uvicorn.Config(self.app, host=self.host, port=self.port, log_level="warning")
        self._server = uvicorn.Server(cfg)
        log("webui", "info", f"Starting WebUI on {self.host}:{self.port}")

        def _adapter_done(adapter_id: str, task: asyncio.Task) -> None:
            try:
                task.result()
            except asyncio.CancelledError:
                log("webui", "notice", f"Adapter task cancelled: {adapter_id}")
            except Exception as e:
                log("webui", "error", f"Adapter task crashed ({adapter_id}): {e}\n{traceback.format_exc()}")

        async def _watch() -> None:
            while self._server and not self._server.started and not self._server.should_exit:
                await asyncio.sleep(0.05)
            if self._server and self._server.started:
                self.started_event.set()
                log("webui", "info", f"WebUI ready at http://{self.host}:{self.port}")
                for adapter_id, adapter in self.adapters.items():
                    if adapter_id == "webui":
                        continue
                    if not self._adapter_enabled(adapter):
                        log("webui", "info", f"Adapter '{adapter_id}' disabled by configuration")
                        continue
                    log("webui", "info", f"Starting adapter task: {adapter_id}")
                    self._start_adapter_task(adapter_id, adapter)
                    task = self._adapter_tasks.get(adapter_id)
                    if task is None:
                        continue
                    task.add_done_callback(lambda t, aid=adapter_id: _adapter_done(aid, t))

        asyncio.create_task(_watch())
        await self._server.serve()

    async def stop(self) -> None:
        """Signal uvicorn to shut down gracefully."""
        for adapter_id, adapter in self.adapters.items():
            await self._stop_adapter(adapter_id, adapter)

        for adapter_id, task in list(self._adapter_tasks.items()):
            if task.done():
                continue
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except asyncio.TimeoutError:
                log("webui", "warning", f"Adapter task did not stop in time; cancelling: {adapter_id}")
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self._adapter_tasks.clear()

        try:
            shutdown_stats = await self.orchestrator.run_shutdown_hooks(
                runtime={"source": "webui.stop", "root_dir": self.root_dir}
            )
            if shutdown_stats.get("called") or shutdown_stats.get("failed"):
                log(
                    "webui",
                    "info",
                    f"Agent on_shutdown hooks: called={shutdown_stats.get('called', 0)} failed={shutdown_stats.get('failed', 0)}",
                )
        except Exception as e:
            log("webui", "error", f"Failed to run agent on_shutdown hooks: {e}\n{traceback.format_exc()}")

        if self._server:
            self._server.should_exit = True
