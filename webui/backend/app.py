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
from memory.api import get_memory_service


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
            }

        @app.get("/api/logs")
        async def api_logs(log_type: str = "all", lines: int = 200, session: dict = Depends(require)):
            return self._read_log(log_type, lines)

        @app.get("/api/logs/types")
        async def api_log_types(session: dict = Depends(require)):
            return {"types": self._list_log_types()}

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

        if self._server:
            self._server.should_exit = True
