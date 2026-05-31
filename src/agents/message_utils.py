"""Shared helpers for building role-aware chat messages."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage


def build_role_aware_messages(query: str, ctx: dict[str, Any], history_limit: int | None = None) -> list[Any]:
    """Build ordinary request messages with user/assistant roles preserved."""

    recent = ctx.get("recent_messages", [])
    items: list[dict[str, Any]] = []
    if isinstance(recent, list):
        items = [x for x in recent[-history_limit:] if isinstance(x, dict)] if history_limit is not None else [x for x in recent if isinstance(x, dict)]

    if not items:
        items = [
            {
                "role": "user",
                "message_id": ctx.get("message_id", ""),
                "user_id": ctx.get("user_id", "unknown"),
                "display_name": ctx.get("display_name", ""),
                "username": ctx.get("username", ""),
                "text": query,
            }
        ]

    out: list[Any] = []
    for item in items:
        text = str(item.get("text", "") or "").strip()
        if not text:
            continue
        role = str(item.get("role", "user") or "user").strip().lower()
        message_id = str(item.get("message_id", "") or "").strip()
        user_id = str(item.get("user_id", "unknown") or "unknown")
        name = str(item.get("display_name", "") or item.get("username", "") or f"user_{user_id}").strip()
        username = str(item.get("username", "") or "").strip().lstrip("@")
        payload = json.dumps(
            {
                "message_id": message_id,
                "user_id": user_id,
                "name": name,
                "username": username,
                "text": text,
            },
            ensure_ascii=False,
        )
        if role == "assistant":
            out.append(AIMessage(content=payload))
        else:
            out.append(HumanMessage(content=payload))
    return out or [HumanMessage(content=query)]


def serialize_role_aware_messages(messages: list[Any]) -> list[dict[str, Any]]:
    """Serialize chat messages to a JSON-friendly form for AS-IS logging."""

    out: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, dict):
            out.append(dict(message))
            continue
        role = str(getattr(message, "type", "") or message.__class__.__name__).strip().lower()
        if role == "ai":
            role = "assistant"
        elif role == "human":
            role = "user"
        content = getattr(message, "content", "")
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")
        out.append(
            {
                "role": role,
                "content": content,
                "name": getattr(message, "name", None),
                "additional_kwargs": getattr(message, "additional_kwargs", {}) or {},
            }
        )
    return out