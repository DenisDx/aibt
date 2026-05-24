"""Transport-level OpenAI-compatible raw request/response logging."""
from __future__ import annotations

import os
from contextvars import ContextVar, Token
from typing import Any

import httpx

from core.logging_utils import log


_CTX: ContextVar[dict[str, Any] | None] = ContextVar("llm_wiretap_ctx", default=None)


def _append_raw_line(path: str, raw: bytes) -> None:
    try:
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(path, "ab") as handle:
            handle.write(raw)
            handle.write(b"\n")
    except Exception as e:
        log("agents", "warning", f"wiretap log write failed: {e}")


def _request_body_bytes(request: httpx.Request) -> bytes:
    try:
        content = request.content
        if isinstance(content, bytes):
            return content
        if isinstance(content, str):
            return content.encode("utf-8")
    except Exception:
        pass

    try:
        data = request.read()
        if isinstance(data, bytes):
            return data
        if isinstance(data, str):
            return data.encode("utf-8")
    except Exception:
        pass

    return b""


def push_llm_log_context(
    *,
    agent_id: str,
    envid: str | None,
    log_path: str,
    payload: dict[str, Any] | None,
) -> Token:
    """Enable raw transport-level logging for current task context."""

    ctx = {
        "log_path": str(log_path or "").strip(),
    }
    return _CTX.set(ctx)


def pop_llm_log_context(token: Token) -> None:
    """Restore previous task logging context."""

    _CTX.reset(token)


async def _on_request(request: httpx.Request) -> None:
    ctx = _CTX.get()
    if not ctx:
        return

    raw = _request_body_bytes(request)
    _append_raw_line(str(ctx.get("log_path") or ""), raw)


async def _on_response(response: httpx.Response) -> None:
    ctx = _CTX.get()
    if not ctx:
        return

    raw = await response.aread()
    _append_raw_line(str(ctx.get("log_path") or ""), raw)


def get_async_http_client() -> httpx.AsyncClient:
    """Create a fresh async HTTP client with request/response wiretap hooks.

    A new instance is created on each call so it is always bound to the
    current event loop (important for in-process service restarts).
    """

    return httpx.AsyncClient(
        headers={"Accept-Encoding": "identity"},
        timeout=httpx.Timeout(90.0),
        event_hooks={
            "request": [_on_request],
            "response": [_on_response],
        },
    )
