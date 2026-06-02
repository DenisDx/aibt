"""Memoryd schemas and normalization helpers.

Provides: request/record normalization utilities for memoryd storage and queueing.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from core.logging_utils import log


DEFAULT_MEMORYD_LIMIT_PER_TYPE = 20
DEFAULT_MEMORYD_TASK_LIMIT = 1
MAX_MEMORYD_MUID_LENGTH = 128


def _preview_data_block(value: Any, head: int = 100, tail: int = 100) -> str:
    """Build bounded preview string: up to 100 chars from start + 100 from end.

    Input: any payload value.
    Output: preview string with max length 200.
    """

    text = str(value or "")
    cap = max(0, int(head)) + max(0, int(tail))
    if len(text) <= cap:
        return text
    return text[: max(0, int(head))] + text[-max(0, int(tail)) :]


def clean_text(value: Any) -> str:
    """Normalize any value into a trimmed lower-case string.

    Input: raw value.
    Output: normalized string.
    """

    return str(value or "").strip().lower()


def normalize_types(types: list[Any] | None, allowed_types: list[str] | None = None) -> list[str]:
    """Normalize memory types for exact-set matching.

    Input: requested types and optional allowlist.
    Output: sorted unique normalized types.
    """

    log("memoryd", "debug", "call memoryd.schemas.normalize_types")
    if types is None:
        return sorted({clean_text(t) for t in (allowed_types or []) if clean_text(t)})

    allowed = {clean_text(t) for t in (allowed_types or []) if clean_text(t)} if allowed_types else None
    out: set[str] = set()
    for raw in types:
        item = clean_text(raw)
        if not item:
            continue
        if allowed is not None and item not in allowed:
            continue
        out.add(item)
    return sorted(out)


def normalize_muid(muid: Any) -> str:
    """Normalize MUID to canonical lowercase text.

    Input: raw MUID.
    Output: canonical MUID string.
    """

    log("memoryd", "debug", "call memoryd.schemas.normalize_muid")
    return clean_text(muid)


def normalize_json_value(value: Any) -> Any:
    """Make value JSON-safe for storage.

    Input: arbitrary value.
    Output: JSON-serializable payload.
    """

    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            out[str(key)] = normalize_json_value(item)
        return out

    if isinstance(value, (list, tuple, set)):
        return [normalize_json_value(item) for item in value]

    # Common model objects (pydantic/langchain/etc.) often expose model_dump().
    if hasattr(value, "model_dump"):
        try:
            return normalize_json_value(value.model_dump())
        except Exception:
            pass

    # Some objects expose dict() but are not plain mappings.
    if hasattr(value, "dict") and callable(getattr(value, "dict", None)):
        try:
            return normalize_json_value(value.dict())
        except Exception:
            pass

    return str(value)


def serialize_json(value: Any) -> str:
    """Serialize value using standard JSON.

    Input: JSON-compatible payload.
    Output: JSON text.
    """

    log("memoryd", "debug", "call memoryd.schemas.serialize_json")
    return json.dumps(normalize_json_value(value), ensure_ascii=False, separators=(",", ":"), default=str)


def deserialize_json(value: Any, default: Any = None) -> Any:
    """Deserialize JSON text or pass-through Python value.

    Input: value and default fallback.
    Output: Python value.
    """

    log("memoryd", "debug", "call memoryd.schemas.deserialize_json")
    if value is None:
        return default
    if isinstance(value, (dict, list, int, float, bool)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception as e:
            preview = _preview_data_block(value)
            log(
                "memoryd",
                "warning",
                f"invalid JSON value format in deserialize_json: {e}; preview_len={len(preview)} raw_len={len(value)} preview={preview}",
            )
            return default if default is not None else value
    return default if default is not None else value


@dataclass(slots=True)
class MemorydTaskMutation:
    """Normalized mutation instruction for worker application.

    Input: parsed mutation dict.
    Output: normalized mutation fields.
    """

    operation: str
    type_name: str | None = None
    record_id: str | None = None
    title: str | None = None
    text: str | None = None
    importance: int = 5
